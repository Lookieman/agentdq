# v0.1 | 27-Jun-2026 | Initial MVP assessment driver
# v0.2 | 27-Jun-2026 | Delegate the pipeline to reporting.assessment.assess

"""End-to-end assessment driver - the console MVP entry point.

Runs an assessment via the shared assess() function and prints a scorecard, plus
a precision/recall evaluation when the dataset carries ground-truth labels. The
Streamlit dashboard uses the same assess() function, so console and dashboard
never disagree.

Two representative invocations:

    # Proof on labelled synthetic data
    python -m tools.run_assessment --data data/synthetic/degraded

    # Assessment of the real CAL extracts
    python -m tools.run_assessment --data data/raw --format xlsx
"""

from __future__ import annotations

import argparse

from src.reporting.assessment import AssessmentResult, assess
from src.reporting.scorecard import print_evaluation, print_examples, print_scorecard


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

    print("\nAgents run:")
    for summary in result.agent_summaries:
        print(f"  {summary['agent']}: {summary['rules_run']} rules, {summary['findings']} findings")

    print_scorecard(result.scorecard, dataset_label=result.dataset_label)
    print_examples(result.findings)
    if result.evaluation is not None:
        print_evaluation(result.evaluation)


if __name__ == "__main__":
    main()
