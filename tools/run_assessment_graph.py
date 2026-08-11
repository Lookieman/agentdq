# ---------------------------------------------------------------------------
# tools/run_assessment_graph.py
# v1.0 | 19-Jul-2026 | Initial creation. CLI runner for the assessment graph:
#                      loads frames/schemas/approved-rules, runs the parallel
#                      dimension fan-out through LangGraph, prints the scorecard.
#                      The LangGraph counterpart to tools/run_assessment.py.
# v1.1 | 20-Jul-2026 | Add a no-checks guard: when zero rules execute, a 100%
#                      score means "checked nothing", not "clean data". Warn
#                      loudly, label the scorecard, and hint at --rules
#                      config/rules. Guard logic is a testable pure helper.
# v1.5 | 10-Aug-2026 | Package 4f. Two corrections. The scorecard now covers the
#                      SAME dimensions the linear path covers, so Uniqueness is
#                      scored rather than run and then ignored; and the
#                      per-dimension denominator reaches it. The no-checks guard
#                      moved to src/reporting/assessment.py so the dashboard can
#                      use it too, and is re-exported here for existing callers.
# v1.4 | 04-Aug-2026 | Package 4e. Reads the ground-truth labels and the decoys
#                      the injector wrote, and prints the uniqueness evaluation:
#                      twin recall, decoy error rate, unlabelled joins, and the
#                      score spread by strategy that Package 5 needs.
# v1.3 | 04-Aug-2026 | Package 4d. Passes data_dir so the agent can read its
#                      vectors, and prints the uniqueness result: the mode, the
#                      spread of scores, the clusters and the candidate pairs.
# v1.2 | 04-Aug-2026 | Package 4b. Advisories print as readable lines rather
#                      than raw dictionaries, and the resolved uniqueness
#                      settings (bands before and after, blocking keys, the
#                      settings code, held-back records) print with them.
# ---------------------------------------------------------------------------
"""Run the assessment graph over a dataset and print the scorecard.

Mirrors the linear tools/run_assessment.py, but orchestrated: the three
dimension agents fan out in parallel and cross-agent advisories are routed to
the (stubbed) uniqueness stage. Approved rules are read from the repository's
export directory by default - the loop the design intends (agent suggests ->
human approves -> executor runs approved rules).

Run:
    python -m tools.run_assessment_graph --data data/synthetic/degraded \\
        --rules data/approved --schema-dir config/schema
"""

from __future__ import annotations

import argparse
from pathlib import Path  # v1.4
from typing import Any, Callable, Optional

import pandas as pd

from src.agents.completeness import CompletenessAgent
from src.agents.consistency import ConsistencyAgent
from src.agents.uniqueness_settings import describe_advisory  # v1.2
from src.agents.validity import ValidityAgent
from src.data.schema import TableSchema, load_schemas
from src.orchestrator import build_assessment_graph
from src.reporting.assessment import (  # v1.5
    SCORED_DIMENSIONS,
    load_frames,
    no_checks_warning,
)
from src.reporting.scorecard import compute_scorecard, print_scorecard
from src.reporting.uniqueness_eval import evaluate_uniqueness, print_evaluation  # v1.4
from src.rules.rule_loader import load_rules

# v1.5: ONE dimension list, imported rather than declared. The local list left
# Uniqueness out, so the agent ran, produced clusters, and its score never
# reached the screen. Two lists in two files is how that happens.
GRAPH_DIMENSIONS: list[str] = SCORED_DIMENSIONS  # v1.5


def _preloaded_loader(rules: list[Any]) -> Callable[[dict[str, Any]], list[Any]]:  # v1.1
    """Return a load-rules callable that yields an already-loaded rule list, so
    the CLI can report the count once and reuse it inside the graph."""
    def loader(state: dict[str, Any]) -> list[Any]:
        return rules
    return loader


def _scorecard_fn() -> Callable[[list[Any], dict[str, Any], dict[str, Any]], Any]:  # v1.5
    """Return a compute callable bound to the scored dimensions.

    The third argument carries the denominators a dimension states for itself,
    so the Uniqueness score is taken over the records it really compared.
    """
    def compute(
        findings: list[Any],
        frames: dict[str, pd.DataFrame],
        totals: dict[str, Any],
    ) -> Any:
        return compute_scorecard(findings, frames, GRAPH_DIMENSIONS, totals)  # v1.5
    return compute


def _cluster_size(cluster: Any) -> int:  # v1.3
    """Sort key for the cluster listing. A named function, not a lambda."""
    return cluster.size


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Run the AgentDQ assessment graph and print a scorecard."
    )
    parser.add_argument("--data", required=True, help="dataset directory")
    parser.add_argument("--rules", default="data/approved", help="approved-rules directory")
    parser.add_argument("--schema-dir", default="config/schema")
    parser.add_argument("--tables", nargs="+", default=["MARA", "MARC", "MAKT"])
    parser.add_argument("--format", default="xlsx", choices=["xlsx", "parquet"])
    args: argparse.Namespace = parser.parse_args()

    frames: dict[str, pd.DataFrame] = load_frames(args.data, args.tables, args.format)
    schemas: dict[str, TableSchema] = load_schemas(args.schema_dir, args.tables)
    approved: list[Any] = load_rules(args.rules)  # v1.1
    executable: int = sum(1 for r in approved if getattr(r, "executable", True))  # v1.1
    print(f"Loaded {len(approved)} rule(s) from {args.rules} ({executable} executable).")  # v1.1

    graph: Any = build_assessment_graph(
        CompletenessAgent(), ValidityAgent(), ConsistencyAgent(),
        load_rules=_preloaded_loader(approved), compute_scorecard=_scorecard_fn(),  # v1.1
    )
    initial: dict[str, Any] = {
        "tables": args.tables, "frames": frames, "schemas": schemas,
        "dataset_label": args.data,
        "data_dir": args.data,  # v1.3 - where the Uniqueness agent reads its vectors
    }
    final: dict[str, Any] = graph.invoke(initial)

    rules_run: int = sum(  # v1.1
        r.get("rules_run", 0) for r in final["agent_results"] if "rules_run" in r
    )
    warning: Optional[str] = no_checks_warning(len(approved), rules_run, args.rules)  # v1.1
    label: str = args.data if warning is None else f"{args.data}  [NO CHECKS RUN]"  # v1.1

    if warning is not None:  # v1.1
        print("\n" + "!" * 60)
        print(warning)
        print("!" * 60)

    print_scorecard(final["report"]["scorecard"], label)  # v1.1

    if warning is not None:  # v1.1
        print("\nReminder: the score above is not meaningful - 0 checks ran.")
    advisories: list[dict[str, Any]] = final.get("upstream_advisories", {}).get("uniqueness", [])  # v1.2
    settings: dict[str, Any] = final.get("uniqueness_settings", {})  # v1.2
    resolved: dict[str, Any] = settings.get("resolved", {})  # v1.2
    bands: dict[str, Any] = resolved.get("bands", {})  # v1.2
    advisory: dict[str, Any] = {}  # v1.2
    table_name: str = ""  # v1.2
    held: int = 0  # v1.2

    if advisories:
        print("\nCross-agent advisories (to the Uniqueness stage, Package 4):")
        for advisory in advisories:
            print(f"  - {describe_advisory(advisory)}")  # v1.2
    if bands:  # v1.2
        print("\nUniqueness settings after the advisories:")
        print(f"  steward bands   : {bands.get('steward_duplicate')} / {bands.get('steward_review_low')}")
        print(f"  advisory shift  : {bands.get('shift')}")
        print(f"  bands in force  : {bands.get('duplicate')} / {bands.get('review_low')}")
        print(f"  blocking keys   : {', '.join(resolved.get('blocking_keys', [])) or '(none)'}")
        print(f"  settings code   : {resolved.get('fingerprint', '')}")
        for table_name, held in settings.get("excluded_counts", {}).items():
            print(f"  held back       : {held} record(s), reason: {table_name}")

    clusters: list[Any] = final.get("clusters", [])  # v1.3
    summary: dict[str, Any] = settings.get("summary", {})  # v1.3
    automatic: int = 0  # v1.3
    cluster: Any = None  # v1.3

    if summary:
        print("\nUniqueness:")
        print(f"  mode            : {summary.get('mode')} ({summary.get('mode_reason') or 'all rungs ran'})")
        print(f"  score spread    : {summary.get('score_spread')}")
        print(f"  candidate pairs : {summary.get('candidate_pairs')} awaiting the adjudicator (Package 4g)")
    if clusters:
        for cluster in clusters:
            if cluster.resolution.value == "automatic":
                automatic += 1
        print(f"  clusters        : {len(clusters)} ({automatic} automatic, "
              f"{len(clusters) - automatic} need a steward)")
        print("\n  Largest clusters:")
        for cluster in sorted(clusters, key=_cluster_size, reverse=True)[:5]:
            print(f"    {cluster.cluster_id}  {cluster.size} records  "
                  f"block={cluster.blocking_values}  weakest link={cluster.weakest_link}")
            print(f"      keep {cluster.survivor_id} ({cluster.survivor_reason.value}, "
                  f"{cluster.resolution.value})")

    # v1.4: read the ground-truth labels and decoys next to the data. When they
    # are not there (a directory with no injected labels), we say so and stop
    # rather than pretending numbers can be produced.
    labels_path: Path = Path(args.data) / "ground_truth.parquet"
    decoys_path: Path = Path(args.data) / "decoys.json"
    if clusters and labels_path.exists():
        evaluation = evaluate_uniqueness(
            clusters=clusters,
            findings=final.get("findings", []),
            labels_path=labels_path,
            decoys_path=decoys_path,
            score_spread=summary.get("score_spread", {}),
            frames=frames,
            blocking_keys=schemas["MARA"].uniqueness.blocking_keys,
        )
        print_evaluation(evaluation)
    elif clusters:
        print("\nNo ground-truth labels beside the dataset, so no uniqueness evaluation.")


if __name__ == "__main__":
    main()
