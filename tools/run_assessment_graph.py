# ---------------------------------------------------------------------------
# tools/run_assessment_graph.py
# v1.0 | 19-Jul-2026 | Initial creation. CLI runner for the assessment graph:
#                      loads frames/schemas/approved-rules, runs the parallel
#                      dimension fan-out through LangGraph, prints the scorecard.
#                      The LangGraph counterpart to tools/run_assessment.py.
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
from typing import Any, Callable

import pandas as pd

from src.agents.completeness import CompletenessAgent
from src.agents.consistency import ConsistencyAgent
from src.agents.validity import ValidityAgent
from src.data.schema import TableSchema, load_schemas
from src.orchestrator import build_assessment_graph
from src.reporting.assessment import load_frames
from src.reporting.scorecard import compute_scorecard, print_scorecard
from src.rules.rule_loader import load_rules


GRAPH_DIMENSIONS: list[str] = ["Completeness", "Validity", "Consistency"]


def _rule_loader(rules_dir: str) -> Callable[[dict[str, Any]], list[Any]]:
    """Return a load_rules callable the load_approved node can call with state.
    Reads approved rules from a directory in the importer/rule_loader shape."""
    def loader(state: dict[str, Any]) -> list[Any]:
        return load_rules(rules_dir)
    return loader


def _scorecard_fn() -> Callable[[list[Any], dict[str, Any]], Any]:
    """Return a compute callable bound to the graph's three dimensions."""
    def compute(findings: list[Any], frames: dict[str, pd.DataFrame]) -> Any:
        return compute_scorecard(findings, frames, GRAPH_DIMENSIONS)
    return compute


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

    graph: Any = build_assessment_graph(
        CompletenessAgent(), ValidityAgent(), ConsistencyAgent(),
        load_rules=_rule_loader(args.rules), compute_scorecard=_scorecard_fn(),
    )
    initial: dict[str, Any] = {
        "tables": args.tables, "frames": frames, "schemas": schemas,
        "dataset_label": args.data,
    }
    final: dict[str, Any] = graph.invoke(initial)

    print_scorecard(final["report"]["scorecard"], args.data)
    advisories: list[str] = final.get("upstream_advisories", {}).get("uniqueness", [])
    if advisories:
        print("\nCross-agent advisories (to the Uniqueness stage, Package 4):")
        for message in advisories:
            print(f"  - {message}")


if __name__ == "__main__":
    main()
