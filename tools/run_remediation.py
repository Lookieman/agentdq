# ---------------------------------------------------------------------------
# tools/run_remediation.py
# v1.0 | 11-Aug-2026 | Package 4h. The batch narrative runner: assess a
#                      dataset, send the grouped plan to the model ONCE, and
#                      write the plan artefact under artefacts/remediation/.
#                      The next assessment of the same dataset and rules
#                      attaches the narrative to the deterministic plan.
# ---------------------------------------------------------------------------
"""Write the model narrative for a remediation plan.

Run from the repository root:

    uv run python -m tools.run_remediation --data data/synthetic/degraded
    uv run python -m tools.run_remediation --data data/raw --format xlsx

One model call covers the whole plan, because grouping turned thousands of
findings into tens of actions. If the response fails validation, no artefact
is written, and the deterministic plan stands on its own.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from src.agents.remediation import group_id_for, save_plan
from src.dspy_modules.lm_config import configure_lm
from src.reporting.assessment import assess

# One call must stay one call. More groups than this and the tail is cut,
# highest ranks kept, and the plan says so.
MAX_GROUPS_IN_CALL: int = 60


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Write the model narrative for the remediation plan of one dataset."
    )
    parser.add_argument("--data", required=True, help="dataset directory")
    parser.add_argument("--schema", default="config/schema", help="schema YAML directory")
    parser.add_argument("--rules", default="config/rules", help="rules directory")
    parser.add_argument("--tables", default="MARA,MARC,MAKT", help="comma separated tables")
    parser.add_argument("--format", default="parquet", choices=["parquet", "xlsx"])
    parser.add_argument("--model", default=None, help="model string; default from AGENTDQ_MODEL or gpt-4o-mini")
    parser.add_argument("--artefacts", default="artefacts", help="artefacts directory")
    args: argparse.Namespace = parser.parse_args()

    model: str = configure_lm(args.model)
    tables: list[str] = [name.strip() for name in args.tables.split(",") if name.strip()]

    print(f"Assessing {args.data} to build the deterministic plan...")
    result = assess(args.data, args.schema, args.rules, tables, args.format)
    fingerprint: str = str(result.uniqueness_settings.get("resolved", {}).get("fingerprint", ""))
    actions = result.actions[:MAX_GROUPS_IN_CALL]
    groups: list[dict[str, Any]] = []
    action: Any = None

    if not actions:
        print("The plan is empty. Nothing to narrate.")
        return
    if len(result.actions) > MAX_GROUPS_IN_CALL:
        print(f"NOTE: {len(result.actions)} actions; the top {MAX_GROUPS_IN_CALL} go to the model.")

    for action in actions:
        groups.append({
            "group_id": group_id_for(action),
            "dimension": action.dimension.value,
            "table": action.table,
            "field": action.field or "",
            "affected_records": action.affected_records,
            "severity": action.severity.value,
            "reason": "/".join(action.finding_refs),
            "sample_issues": action.rationale,
        })

    print(f"{len(groups)} group(s) in one model call; model {model}.")

    import dspy
    from src.dspy_modules.adjudication_signatures import RemediationSynthesis

    program = dspy.ChainOfThought(RemediationSynthesis)
    answer = program(groups_json=json.dumps(groups, ensure_ascii=False))

    narratives: list[dict[str, str]] = []
    entry: Any = None
    known: set[str] = {group["group_id"] for group in groups}
    for entry in answer.narratives:
        if str(entry.group_id) in known:
            narratives.append({
                "group_id": str(entry.group_id),
                "action": str(entry.action),
                "rationale": str(entry.rationale),
            })
    if not narratives:
        print("The response held no usable narrative. No artefact written; the "
              "deterministic plan stands.")
        return

    path = save_plan(args.artefacts, args.data, args.rules, {
        "fingerprint": fingerprint,
        "model": model,
        "dataset": args.data,
        "rules_dir": args.rules,
        "executive_summary": str(answer.executive_summary),
        "narratives": narratives,
    })
    print(f"\nNarrative for {len(narratives)} of {len(groups)} group(s).")
    print(f"Artefact: {path}")
    print("\nRun the assessment again to read the plan with the narrative attached:")
    print(f"  uv run python -m tools.run_assessment --data {args.data} --format {args.format}")


if __name__ == "__main__":
    main()
