# v0.1 | 27-Jun-2026 | Initial reusable assessment (shared by CLI and dashboard)
# v0.2 | 26-Jul-2026 | Add the Consistency dimension: ConsistencyAgent joins the
# v0.3 | 04-Aug-2026 | Package 4d. ONE dimension list became TWO. It fed both
#                      the scorecard and the ground-truth evaluation, so adding
#                      Uniqueness to it would have put a meaningless 50% figure
#                      beside the real ones.
#                      agent loop and Consistency joins ASSESSED_DIMENSIONS, so
#                      the linear assess() (and the dashboard over it) covers the
#                      same three deterministic dimensions the graph does.

"""One place that runs an assessment end to end.

Loads a dataset (generated parquet or real SE16N xlsx), runs the dimension
agents, computes the scorecard and, when ground-truth labels are present,
evaluates the findings. Both the command-line tool and the Streamlit dashboard
call assess() so the two can never drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pandas as pd
from pydantic import BaseModel, Field

from src.agents.completeness import CompletenessAgent
from src.agents.consistency import ConsistencyAgent  # v0.2
from src.agents.validity import ValidityAgent
from src.contracts import DefectLabel, Finding
from src.data.extract_loader import load_sap_table
from src.data.schema import TableSchema, load_schemas
from src.reporting.scorecard import (
    DimensionEval,
    Scorecard,
    compute_scorecard,
    evaluate_against_labels,
)
from src.rules.rule_loader import load_rules

# Two lists, because they answer two different questions.
#
# SCORED_DIMENSIONS is what appears on the scorecard.
#
# LABEL_EVALUATED_DIMENSIONS is what is measured against the injected ground
# truth, record by record. Uniqueness is deliberately absent. The injector
# labels a twin but holds NO opinion on which of the two records deserves to
# survive, and a twin is a full copy of its source, so the two diverge at random
# once field defects are injected. Survivorship therefore keeps the source about
# half the time and the twin about half the time, and a record-level score would
# read near 50% precision and 50% recall even with perfect detection. Cluster-
# level measurement arrives in Package 4e, with the decoy pairs that make it
# mean something.
SCORED_DIMENSIONS: list[str] = [  # v0.3
    "Completeness", "Validity", "Consistency", "Uniqueness",
]
LABEL_EVALUATED_DIMENSIONS: list[str] = [  # v0.3
    "Completeness", "Validity", "Consistency",
]
# Kept so existing callers do not break. It means "scored".
ASSESSED_DIMENSIONS: list[str] = SCORED_DIMENSIONS  # v0.3


class AssessmentResult(BaseModel):
    """Everything a caller needs to render or print an assessment."""

    dataset_label: str
    data_format: str
    tables: dict[str, int] = Field(default_factory=dict)
    total_records: int = 0
    agent_summaries: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    scorecard: Scorecard
    has_ground_truth: bool = False
    evaluation: Optional[dict[str, DimensionEval]] = None


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


def assess(
    data_dir: str,
    schema_dir: str,
    rules_dir: str,
    tables: list[str],
    data_format: str,
) -> AssessmentResult:
    """Run the agents over a dataset and return a structured result."""
    frames: dict[str, pd.DataFrame] = load_frames(data_dir, tables, data_format)
    schemas: dict[str, TableSchema] = load_schemas(schema_dir, tables)
    rules = load_rules(rules_dir)
    findings: list[Finding] = []
    agent_summaries: list[dict[str, Any]] = []
    result = None
    labels_path: Path = Path(data_dir) / "ground_truth.parquet"
    evaluation: Optional[dict[str, DimensionEval]] = None
    has_gt: bool = labels_path.exists()

    for agent in (CompletenessAgent(), ValidityAgent(), ConsistencyAgent()):  # v0.2
        result = agent.run(frames, schemas, rules)
        agent_summaries.append({
            "agent": result.agent,
            "rules_run": result.rules_run,
            "findings": len(result.findings),
        })
        findings.extend(result.findings)

    scorecard: Scorecard = compute_scorecard(findings, frames, SCORED_DIMENSIONS)  # v0.3

    if has_gt:
        evaluation = evaluate_against_labels(  # v0.3
            findings, load_labels(labels_path), LABEL_EVALUATED_DIMENSIONS
        )

    return AssessmentResult(
        dataset_label=data_dir,
        data_format=data_format,
        tables={table: int(frame.shape[0]) for table, frame in frames.items()},
        total_records=sum(int(frame.shape[0]) for frame in frames.values()),
        agent_summaries=agent_summaries,
        findings=findings,
        scorecard=scorecard,
        has_ground_truth=has_gt,
        evaluation=evaluation,
    )
