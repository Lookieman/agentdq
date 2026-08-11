# v0.4 | 10-Aug-2026 | Package 4f. assess() no longer loops the agents itself.
#                      It builds the assessment graph and invokes it, so the
#                      dashboard, the console driver and the graph runner all
#                      execute ONE path. Before this the dashboard ran three
#                      dimensions and the graph ran four, and the two agreed
#                      only by coincidence. AssessmentResult gains the clusters,
#                      the resolved uniqueness settings and the uniqueness
#                      evaluation, and the no-checks guard moves here from the
#                      graph runner so every caller can use it.
# v0.3 | 04-Aug-2026 | Package 4d. ONE dimension list became TWO. It fed both
#                      the scorecard and the ground-truth evaluation, so adding
#                      Uniqueness to it would have put a meaningless 50% figure
#                      beside the real ones.
# v0.2 | 26-Jul-2026 | Add the Consistency dimension: ConsistencyAgent joins the
#                      agent loop and Consistency joins ASSESSED_DIMENSIONS, so
#                      the linear assess() (and the dashboard over it) covers the
#                      same three deterministic dimensions the graph does.
# v0.1 | 27-Jun-2026 | Initial reusable assessment (shared by CLI and dashboard)

"""One place that runs an assessment end to end.

Loads a dataset (generated parquet or real SE16N xlsx), runs the assessment
graph over it, and returns a structured result. The command-line tools and the
Streamlit dashboard all call assess(), so no two surfaces can report different
numbers for the same dataset.

What the graph does that a plain loop cannot: the three deterministic dimensions
fan out in parallel, each may send advice to the uniqueness stage, and the
uniqueness stage reads that advice before it matches anything. A loop has no
place to put that.

Two dimension lists, because they answer two different questions.

SCORED_DIMENSIONS is what appears on the scorecard.

LABEL_EVALUATED_DIMENSIONS is what is measured against the injected ground
truth, record by record. Uniqueness is deliberately absent. The injector labels
a twin but holds NO opinion on which of the two records deserves to survive, and
a twin is a full copy of its source, so the two diverge at random once field
defects are injected. Survivorship therefore keeps the source about half the
time and the twin about half the time, and a record-level score would read near
50% precision and 50% recall even with perfect detection. Uniqueness is measured
instead by src/reporting/uniqueness_eval.py, on clusters, with decoy pairs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional  # v0.4

import pandas as pd
from pydantic import BaseModel, Field

from src.agents.completeness import CompletenessAgent
from src.agents.consistency import ConsistencyAgent  # v0.2
from src.agents.validity import ValidityAgent
from src.contracts import DefectLabel, DuplicateCluster, Finding  # v0.4
from src.data.extract_loader import load_sap_table
from src.data.schema import TableSchema, load_schemas
from src.orchestrator import build_assessment_graph  # v0.4
from src.reporting.scorecard import (
    DimensionEval,
    Scorecard,
    compute_scorecard,
    evaluate_against_labels,
)
from src.reporting.uniqueness_eval import UniquenessEvaluation, evaluate_uniqueness  # v0.4
from src.rules.rule_loader import load_rules

# See the module docstring for why there are two lists and not one.
SCORED_DIMENSIONS: list[str] = [  # v0.3
    "Completeness", "Validity", "Consistency", "Uniqueness",
]
LABEL_EVALUATED_DIMENSIONS: list[str] = [  # v0.3
    "Completeness", "Validity", "Consistency",
]
# Kept so existing callers do not break. It means "scored".
ASSESSED_DIMENSIONS: list[str] = SCORED_DIMENSIONS  # v0.3


class AssessmentResult(BaseModel):
    """Everything a caller needs to render or print an assessment.

    The fields added in v0.4 are what Package 4 produced and no surface could
    see: the clusters themselves, the settings the matcher really used, and the
    cluster-level evaluation. rules_dir and the two rule counts travel with them
    so a reader always knows which rules ran, and the no-checks guard can fire.
    """

    dataset_label: str
    data_format: str
    tables: dict[str, int] = Field(default_factory=dict)
    total_records: int = 0
    agent_summaries: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    scorecard: Scorecard
    has_ground_truth: bool = False
    evaluation: Optional[dict[str, DimensionEval]] = None
    clusters: list[DuplicateCluster] = Field(default_factory=list)  # v0.4
    uniqueness_settings: dict[str, Any] = Field(default_factory=dict)  # v0.4
    uniqueness_evaluation: Optional[UniquenessEvaluation] = None  # v0.4
    rules_dir: str = ""  # v0.4
    rules_loaded: int = 0  # v0.4
    rules_run: int = 0  # v0.4


def load_frames(data_dir: str, tables: list[str], data_format: str) -> dict[str, pd.DataFrame]:
    """Load the assessed tables from parquet or SE16N xlsx."""
    base: Path = Path(data_dir)
    frames: dict[str, pd.DataFrame] = {}
    table: str = ""

    for table in tables:
        if data_format == "xlsx":
            frames[table] = load_sap_table(str(base / f"{table}_EX_DATA.xlsx"), header_anchor="MATNR")
        else:
            frames[table] = pd.read_parquet(base / f"{table}.parquet")
    return frames


def load_labels(path: Path) -> list[DefectLabel]:
    """Reconstruct ground-truth labels from a parquet file."""
    frame: pd.DataFrame = pd.read_parquet(path)
    labels: list[DefectLabel] = []
    row = None
    field_value = None
    rule_value = None

    for row in frame.itertuples():
        field_value = row.field if pd.notna(row.field) else None
        rule_value = row.rule_id if pd.notna(row.rule_id) else None
        labels.append(DefectLabel(
            defect_id=str(row.defect_id),
            table=str(row.table),
            record_key=str(row.record_key),
            dimension=str(row.dimension),
            field=field_value,
            rule_id=rule_value,
            detail={},
        ))
    return labels


def no_checks_warning(rules_loaded: int, rules_run: int, rules_dir: str) -> Optional[str]:  # v0.4
    """Return a warning when the run executed no checks, else None.

    A scorecard of 100% is produced by 100 * (1 - affected/total) with zero
    findings, which is exactly what happens when NO rules run. That reads as
    'perfect data' and means 'checked nothing', so it must be flagged. The two
    causes read differently: no rules were found at all, or rules were found but
    none executed.

    Moved here from tools/run_assessment_graph.py in v0.4, because the dashboard
    needs the same guard and an app must not import from the CLI layer.
    """
    hint: str = "point the rules directory at a directory of rules (for example config/rules)"
    detail: str = ""

    if rules_run > 0:
        return None
    if rules_loaded == 0:
        detail = f"no rules were found in {rules_dir}"
    else:
        detail = (f"{rules_loaded} rule(s) loaded from {rules_dir}, but none executed "
                  f"(none executable, or none for the assessed tables)")
    return (
        "WARNING: 0 checks ran, so the scores reflect NOTHING CHECKED, "
        "not clean data.\n"
        f"         {detail}.\n"
        f"         To assess against real rules, {hint}."
    )


def _preloaded_loader(rules: list[Any]) -> Callable[[dict[str, Any]], list[Any]]:  # v0.4
    """Return a load-rules callable that yields an already-loaded rule list.

    The rules are read once, outside the graph, so the count can be reported and
    the same list reused inside it.
    """
    def loader(state: dict[str, Any]) -> list[Any]:
        return rules
    return loader


def _scorecard_fn(
    dimensions: list[str],
) -> Callable[[list[Any], dict[str, Any], dict[str, Any]], Scorecard]:  # v0.4
    """Return a compute callable bound to a dimension list.

    The third argument holds the denominators a dimension states for itself.
    Uniqueness is assessed on one table and holds records back, so it must not
    be divided by every row of every loaded table.
    """
    def compute(
        findings: list[Any],
        frames: dict[str, pd.DataFrame],
        totals: dict[str, Any],
    ) -> Scorecard:
        return compute_scorecard(findings, frames, dimensions, totals)
    return compute


def _count_rules_run(agent_results: list[dict[str, Any]]) -> int:  # v0.4
    """How many rules actually executed across the rule-backed agents."""
    total: int = 0
    entry: dict[str, Any] = {}

    for entry in agent_results or []:
        total += int(entry.get("rules_run", 0))
    return total


def assess(
    data_dir: str,
    schema_dir: str,
    rules_dir: str,
    tables: list[str],
    data_format: str,
) -> AssessmentResult:
    """Run the assessment graph over a dataset and return a structured result.

    The graph fans the three deterministic dimensions out in parallel, routes
    their advice to the uniqueness stage, matches duplicates, and scores the
    result. Every surface calls this function, so console and screen cannot
    disagree.
    """
    frames: dict[str, pd.DataFrame] = load_frames(data_dir, tables, data_format)
    schemas: dict[str, TableSchema] = load_schemas(schema_dir, tables)
    rules: list[Any] = load_rules(rules_dir)
    graph: Any = None
    initial: dict[str, Any] = {}
    final: dict[str, Any] = {}
    findings: list[Finding] = []
    agent_summaries: list[dict[str, Any]] = []
    clusters: list[DuplicateCluster] = []
    labels_path: Path = Path(data_dir) / "ground_truth.parquet"
    decoys_path: Path = Path(data_dir) / "decoys.json"
    evaluation: Optional[dict[str, DimensionEval]] = None
    uniqueness_evaluation: Optional[UniquenessEvaluation] = None
    settings: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    blocking_keys: list[str] = []
    subject: str = ""
    has_gt: bool = labels_path.exists()

    graph = build_assessment_graph(  # v0.4
        CompletenessAgent(), ValidityAgent(), ConsistencyAgent(),
        load_rules=_preloaded_loader(rules),
        compute_scorecard=_scorecard_fn(SCORED_DIMENSIONS),
    )
    initial = {
        "tables": tables,
        "frames": frames,
        "schemas": schemas,
        "dataset_label": data_dir,
        "data_dir": data_dir,  # where the Uniqueness agent reads its vectors
    }
    final = graph.invoke(initial)

    findings = list(final.get("findings", []))
    agent_summaries = list(final.get("agent_results", []))
    clusters = list(final.get("clusters", []))
    settings = dict(final.get("uniqueness_settings", {}))
    summary = dict(settings.get("summary", {}))

    if has_gt:
        evaluation = evaluate_against_labels(  # v0.3
            findings, load_labels(labels_path), LABEL_EVALUATED_DIMENSIONS
        )
    if clusters and has_gt:  # v0.4
        for subject in schemas:
            if schemas[subject].uniqueness and schemas[subject].uniqueness.blocking_keys:
                blocking_keys = list(schemas[subject].uniqueness.blocking_keys)
                break
        uniqueness_evaluation = evaluate_uniqueness(
            clusters=clusters,
            findings=findings,
            labels_path=labels_path,
            decoys_path=decoys_path,
            score_spread=summary.get("score_spread", {}),
            frames=frames,
            blocking_keys=blocking_keys,
        )

    return AssessmentResult(
        dataset_label=data_dir,
        data_format=data_format,
        tables={table: int(frame.shape[0]) for table, frame in frames.items()},
        total_records=sum(int(frame.shape[0]) for frame in frames.values()),
        agent_summaries=agent_summaries,
        findings=findings,
        scorecard=final["report"]["scorecard"],
        has_ground_truth=has_gt,
        evaluation=evaluation,
        clusters=clusters,  # v0.4
        uniqueness_settings=settings,  # v0.4
        uniqueness_evaluation=uniqueness_evaluation,  # v0.4
        rules_dir=rules_dir,  # v0.4
        rules_loaded=len(rules),  # v0.4
        rules_run=_count_rules_run(agent_summaries),  # v0.4
    )
