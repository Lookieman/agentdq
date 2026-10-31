# ---------------------------------------------------------------------------
# src/agents/remediation.py
# v1.0 | 11-Aug-2026 | Package 4h. Group findings, rank the groups, and turn
#                      each group into one action. Grouping and ranking are
#                      arithmetic and run offline on every assessment. The
#                      model writes the narrative in a batch step, and the plan
#                      artefact attaches it here when the fingerprints match.
# ---------------------------------------------------------------------------
"""Turn thousands of findings into tens of ranked actions.

Volume is the design problem. A degraded run produces thousands of findings,
and one action per finding is a worse report than no report. So the agent
groups by dimension, table, field and rule, and each group becomes ONE action
that carries the number of records it affects.

The split of work follows the project's one rule for models:

    arithmetic (offline, always)      judgement (model, batch step)
    --------------------------------  --------------------------------
    grouping                          the action wording
    the rank                          the rationale
    the affected counts               the cross-dimension summary
    the template action text

The rank is severity weight times affected records, with a lift for a
mandatory field, because a gap in a field the business declared mandatory
outranks the same gap in an optional one. The weights are stated, not
calibrated; calibration is Package 5.

The agent recommends. It never writes to data, and that boundary is permanent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from src.contracts import Dimension, Finding, RemediationAction, Severity
from src.data.schema import TableSchema

# The rank arithmetic. Stated, not calibrated.
SEVERITY_WEIGHT: dict[Severity, int] = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
}
MANDATORY_LIFT: float = 1.5

# One template per dimension: the offline action text. The model can replace
# the wording; it cannot replace the group or the numbers.
ACTION_TEMPLATES: dict[Dimension, str] = {
    Dimension.COMPLETENESS: "Populate {field} on the {count} affected {table} records.",
    Dimension.VALIDITY: "Correct the {count} {table} records whose {field} value fails its checks.",
    Dimension.CONSISTENCY: "Align {field} on the {count} {table} records that break the cross-field checks.",
    Dimension.UNIQUENESS: "Review the duplicate groups, keep each recommended survivor, and "
                          "block the other members after a check for open transactions.",
}

PLAN_DIRNAME: str = "remediation"


def _severity_rank(severity: Severity) -> int:
    return SEVERITY_WEIGHT.get(severity, 1)


def _worst_severity(findings: list[Finding]) -> Severity:
    worst: Severity = Severity.LOW
    finding: Finding = None

    for finding in findings:
        if _severity_rank(finding.severity) > _severity_rank(worst):
            worst = finding.severity
    return worst


def _action_priority(entry: dict[str, Any]) -> float:
    """Sort key for the plan. A named function, not a lambda."""
    return float(entry["priority"])


def group_findings(findings: list[Finding]) -> dict[tuple, list[Finding]]:
    """Group by dimension, table, field and rule. Thousands become tens."""
    groups: dict[tuple, list[Finding]] = {}
    finding: Finding = None
    key: tuple = ()

    for finding in findings:
        key = (finding.dimension, finding.table, finding.field or "", finding.rule_id or "")
        groups.setdefault(key, []).append(finding)
    return groups


def build_actions(
    findings: list[Finding],
    schemas: dict[str, TableSchema],
    uniqueness_summary: Optional[dict[str, Any]] = None,
) -> list[RemediationAction]:
    """The deterministic plan: grouped, ranked, template-worded.

    The uniqueness summary contributes actions the findings cannot see: the
    held-back records, an oversized block, and a fuzzy-only run. A held-back
    record is work, because it was never even checked for duplicates.
    """
    groups: dict[tuple, list[Finding]] = group_findings(findings)
    entries: list[dict[str, Any]] = []
    key: tuple = ()
    members: list[Finding] = []
    dimension: Dimension = None
    table: str = ""
    field: str = ""
    rule_id: str = ""
    affected: int = 0
    severity: Severity = Severity.MEDIUM
    mandatory: bool = False
    priority: float = 0.0
    entry: dict[str, Any] = {}
    actions: list[RemediationAction] = []
    rank: int = 0

    for key, members in groups.items():
        dimension, table, field, rule_id = key
        affected = len({finding.record_id for finding in members})
        severity = _worst_severity(members)
        mandatory = bool(
            table in schemas and field in schemas[table].fields
            and schemas[table].fields[field].mandatory
        )
        priority = _severity_rank(severity) * affected * (MANDATORY_LIFT if mandatory else 1.0)
        entries.append({
            "priority": priority,
            "action": RemediationAction(
                rank=0,
                dimension=dimension,
                table=table,
                field=field or None,
                action=ACTION_TEMPLATES.get(dimension, "Review the {count} affected records.").format(
                    field=field or "the field", count=affected, table=table,
                ),
                rationale=(f"{affected} records; worst severity {severity.value}"
                           + ("; the field is mandatory" if mandatory else "")
                           + (f"; rule {rule_id}" if rule_id else "")),
                affected_records=affected,
                finding_refs=[rule_id] if rule_id else [],
                severity=severity,
            ),
        })

    entries.extend(_uniqueness_entries(uniqueness_summary or {}))
    entries.sort(key=_action_priority, reverse=True)
    for rank, entry in enumerate(entries, start=1):
        entry["action"].rank = rank
        actions.append(entry["action"])
    return actions


def _uniqueness_entries(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Actions from the matcher's own accounting, priced like the rest."""
    entries: list[dict[str, Any]] = []
    held: dict[str, int] = summary.get("held_back", {}) or {}
    oversized: list[dict[str, Any]] = summary.get("oversized_blocks", []) or []
    reason_text: dict[str, str] = {
        "validity_failed": ("Fix the descriptions that failed a validity check. The same records "
                            "were held out of duplicate matching, so one fix serves two dimensions."),
        "no_description": "Add a description to the records that have none, so they can be matched.",
        "no_blocking_key": "Populate the blocking key fields on the records that miss them.",
        "block_too_large": ("Add a blocking key or raise max_block_pairs: these records sat in a "
                            "block too large to compare, so they were never checked."),
    }
    reason: str = ""
    count: int = 0
    entry: dict[str, Any] = {}

    for reason, count in held.items():
        if count <= 0:
            continue
        entries.append({
            "priority": SEVERITY_WEIGHT[Severity.HIGH] * count,
            "action": RemediationAction(
                rank=0,
                dimension=Dimension.UNIQUENESS,
                table="MARA",
                field=None,
                action=reason_text.get(reason, f"Review the records held back for reason {reason}."),
                rationale=(f"{count} records were held out of duplicate matching "
                           f"(reason code {reason}). A held-back record is not a clean record; "
                           f"it was never compared."),
                affected_records=count,
                finding_refs=[reason],
                severity=Severity.HIGH,
            ),
        })
    if str(summary.get("mode", "")) == "fuzzy_only":
        entries.append({
            "priority": SEVERITY_WEIGHT[Severity.MEDIUM] * max(1, int(summary.get("records_assessed", 1))),
            "action": RemediationAction(
                rank=0,
                dimension=Dimension.UNIQUENESS,
                table="MARA",
                field=None,
                action="Build the semantic vectors with tools/build_embeddings.py, then run again.",
                rationale=(f"The run compared letter by letter only "
                           f"({summary.get('mode_reason') or 'no vector file'}), so duplicates "
                           f"worded differently were missed."),
                affected_records=int(summary.get("records_assessed", 0)),
                finding_refs=["fuzzy_only"],
                severity=Severity.MEDIUM,
            ),
        })
    for entry in oversized:
        pass  # oversized blocks already arrive through the block_too_large hold-back count
    return entries


# ---------------------------------------------------------------------------
# The plan artefact - the model's narrative, attached when it matches
# ---------------------------------------------------------------------------

def plan_path(artefacts_dir: str | Path, dataset_label: str, rules_dir: str) -> Path:
    """One plan file per dataset-and-rules pair, named so a person can read it."""
    safe: str = f"{Path(dataset_label).name}__{Path(rules_dir).name}"
    return Path(artefacts_dir) / PLAN_DIRNAME / f"plan_{safe}.json"


def load_plan(artefacts_dir: str | Path, dataset_label: str, rules_dir: str) -> dict[str, Any]:
    path: Path = plan_path(artefacts_dir, dataset_label, rules_dir)

    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}


def save_plan(
    artefacts_dir: str | Path,
    dataset_label: str,
    rules_dir: str,
    plan: dict[str, Any],
) -> Path:
    path: Path = plan_path(artefacts_dir, dataset_label, rules_dir)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)
    return path


def group_id_for(action: RemediationAction) -> str:
    """The stable id the narrative is keyed by."""
    return f"{action.dimension.value}|{action.table}|{action.field or ''}|{'/'.join(action.finding_refs)}"


def attach_narrative(
    actions: list[RemediationAction],
    plan: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    """Replace template wording with the model's, when the plan matches.

    A plan built under different settings or rules is STALE: nothing is
    replaced, and the caller says so. The numbers are never replaced at all;
    the model owns the words, the arithmetic owns the figures.
    """
    narratives: dict[str, dict[str, str]] = {}
    entry: dict[str, Any] = {}
    action: RemediationAction = None
    attached: int = 0
    stale: bool = False

    if not plan:
        return {"attached": 0, "stale": False, "executive_summary": ""}
    if plan.get("fingerprint") and plan.get("fingerprint") != fingerprint:
        return {"attached": 0, "stale": True, "executive_summary": ""}
    for entry in plan.get("narratives", []):
        narratives[str(entry.get("group_id", ""))] = entry
    for action in actions:
        entry = narratives.get(group_id_for(action))
        if not entry:
            continue
        if entry.get("action"):
            action.action = str(entry["action"])
        if entry.get("rationale"):
            action.rationale = str(entry["rationale"])
        attached += 1
    return {
        "attached": attached,
        "stale": stale,
        "executive_summary": str(plan.get("executive_summary", "")),
    }
