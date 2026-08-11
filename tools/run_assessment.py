# v0.3 | 10-Aug-2026 | Package 4f. assess() now runs the graph, so this driver
#                      reports what the graph produced: the no-checks guard, the
#                      uniqueness settings, the clusters and the cluster-level
#                      evaluation. Console and dashboard show the same numbers
#                      because they call the same function.
# v0.2 | 27-Jun-2026 | Delegate the pipeline to reporting.assessment.assess
# v0.1 | 27-Jun-2026 | Initial MVP assessment driver

"""End-to-end assessment driver - the console MVP entry point.

Runs an assessment via the shared assess() function and prints a scorecard, plus
a precision/recall evaluation when the dataset carries ground-truth labels. The
Streamlit dashboard uses the same assess() function, so console and dashboard
never disagree.

Since v0.3 assess() runs the assessment graph, so this driver also reports the
uniqueness stage: the settings in force, the clusters found, and the
cluster-level evaluation on a labelled dataset.

Two representative invocations:

    # Proof on labelled synthetic data
    python -m tools.run_assessment --data data/synthetic/degraded

    # Assessment of the real CAL extracts
    python -m tools.run_assessment --data data/raw --format xlsx
"""

from __future__ import annotations

import argparse

from typing import Any, Optional  # v0.3

from src.reporting.assessment import AssessmentResult, assess, no_checks_warning  # v0.3
from src.reporting.scorecard import print_evaluation, print_examples, print_scorecard
# v0.3: two functions share the name print_evaluation and measure different
# things, so the uniqueness one is imported under a name of its own.
from src.reporting.uniqueness_eval import print_evaluation as print_uniqueness_evaluation  # v0.3


def _cluster_size(cluster: Any) -> int:  # v0.3
    """Sort key for the cluster listing. A named function, not a lambda."""
    return cluster.size


def print_uniqueness(result: AssessmentResult) -> None:  # v0.3
    """Print the uniqueness stage: settings, held-back records and clusters."""
    settings: dict[str, Any] = result.uniqueness_settings or {}
    resolved: dict[str, Any] = settings.get("resolved", {})
    summary: dict[str, Any] = settings.get("summary", {})
    bands: dict[str, Any] = resolved.get("bands", {})
    line: str = ""
    reason: str = ""
    held: int = 0
    automatic: int = 0
    cluster: Any = None

    if not summary:
        return
    print("\nUniqueness:")
    print(f"  mode            : {summary.get('mode')} "
          f"({summary.get('mode_reason') or 'all rungs ran'})")
    print(f"  bands in force  : {bands.get('duplicate')} / {bands.get('review_low')} "
          f"(steward {bands.get('steward_duplicate')} / {bands.get('steward_review_low')}, "
          f"shift {bands.get('shift')})")
    print(f"  blocking keys   : {', '.join(resolved.get('blocking_keys', [])) or '(none)'}")
    print(f"  fuzzy metric    : {resolved.get('fuzzy_metric', '-')}")
    print(f"  records compared: {summary.get('records_assessed', '-')} "
          f"({summary.get('held_back_total', 0)} held back)")
    for reason, held in (summary.get("held_back", {}) or {}).items():
        print(f"    held back     : {held} record(s), reason: {reason}")
    print(f"  score spread    : {summary.get('score_spread')}")
    print(f"  candidate pairs : {summary.get('candidate_pairs')} awaiting the adjudicator")
    for line in settings.get("readable", []) or []:
        print(f"  advisory        : {line}")

    if result.clusters:
        for cluster in result.clusters:
            if cluster.resolution.value == "automatic":
                automatic += 1
        print(f"  clusters        : {len(result.clusters)} ({automatic} automatic, "
              f"{len(result.clusters) - automatic} need a steward)")
        print("\n  Largest clusters:")
        for cluster in sorted(result.clusters, key=_cluster_size, reverse=True)[:5]:
            print(f"    {cluster.cluster_id}  {cluster.size} records  "
                  f"block={cluster.blocking_values}  weakest link={cluster.weakest_link}")
            print(f"      keep {cluster.survivor_id} ({cluster.survivor_reason.value}, "
                  f"{cluster.resolution.value})")


def main() -> None:
    """Entry point for module execution."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Run an AgentDQ assessment and print a scorecard."
    )
    parser.add_argument("--data", required=True, help="dataset directory")
    parser.add_argument("--schema", default="config/schema", help="schema YAML directory")
    parser.add_argument("--rules", default="config/rules", help="rule YAML directory")
    parser.add_argument("--tables", default="MARA,MARC,MAKT", help="comma separated tables")
    parser.add_argument("--format", dest="data_format", default="parquet", choices=["parquet", "xlsx"])
    args: argparse.Namespace = parser.parse_args()
    table_list: list[str] = [t.strip() for t in args.tables.split(",") if t.strip()]

    result: AssessmentResult = assess(
        data_dir=args.data,
        schema_dir=args.schema,
        rules_dir=args.rules,
        tables=table_list,
        data_format=args.data_format,
    )

    warning: Optional[str] = no_checks_warning(  # v0.3
        result.rules_loaded, result.rules_run, result.rules_dir
    )
    label: str = result.dataset_label if warning is None else f"{result.dataset_label}  [NO CHECKS RUN]"  # v0.3

    print(f"\nLoaded {result.rules_loaded} rule(s) from {result.rules_dir}.")  # v0.3
    print("\nAgents run:")
    for summary in result.agent_summaries:
        print(f"  {summary.get('agent')}: {summary.get('rules_run', 0)} rules, "
              f"{summary.get('findings', 0)} findings")  # v0.3

    if warning is not None:  # v0.3
        print("\n" + "!" * 60)
        print(warning)
        print("!" * 60)

    print_scorecard(result.scorecard, dataset_label=label)  # v0.3
    print_examples(result.findings)
    print_uniqueness(result)  # v0.3
    if result.evaluation is not None:
        print_evaluation(result.evaluation)
    if result.uniqueness_evaluation is not None:  # v0.3
        print_uniqueness_evaluation(result.uniqueness_evaluation)


if __name__ == "__main__":
    main()
