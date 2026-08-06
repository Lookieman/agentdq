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
from typing import Any, Callable, Optional

import pandas as pd

from src.agents.completeness import CompletenessAgent
from src.agents.consistency import ConsistencyAgent
from src.agents.uniqueness_settings import describe_advisory  # v1.2
from src.agents.validity import ValidityAgent
from src.data.schema import TableSchema, load_schemas
from src.orchestrator import build_assessment_graph
from src.reporting.assessment import load_frames
from src.reporting.scorecard import compute_scorecard, print_scorecard
from src.rules.rule_loader import load_rules

GRAPH_DIMENSIONS: list[str] = ["Completeness", "Validity", "Consistency"]


def no_checks_warning(rules_loaded: int, rules_run: int, rules_dir: str) -> Optional[str]:  # v1.1
    """Return a warning when the run executed no checks, else None.

    A scorecard of 100% is produced by 100 * (1 - affected/total) with zero
    findings - which is exactly what happens when NO rules run. That reads as
    'perfect data' but means 'checked nothing', so it must be flagged. The two
    causes read differently: no rules were found at all, or rules were found but
    none executed (not executable, or none for the assessed tables)."""
    hint: str = "point --rules at a directory of rules (e.g. --rules config/rules)"
    detail: str = ""

    if rules_run > 0:
        return None
    if rules_loaded == 0:
        detail = f"no rules were found in {rules_dir}"
    else:
        detail = (f"{rules_loaded} rule(s) loaded from {rules_dir}, but none executed "
                  f"(none executable, or none for the assessed tables)")
    return (
        "WARNING: 0 checks ran, so the scores below reflect NOTHING CHECKED, "
        "not clean data.\n"
        f"         {detail}.\n"
        f"         To assess against real rules, {hint}."
    )


def _preloaded_loader(rules: list[Any]) -> Callable[[dict[str, Any]], list[Any]]:  # v1.1
    """Return a load-rules callable that yields an already-loaded rule list, so
    the CLI can report the count once and reuse it inside the graph."""
    def loader(state: dict[str, Any]) -> list[Any]:
        return rules
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
            print(f"  held back       : {held} record(s) in {table_name}, description failed validity")


if __name__ == "__main__":
    main()
