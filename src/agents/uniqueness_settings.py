# ---------------------------------------------------------------------------
# src/agents/uniqueness_settings.py
# v1.2 | 10-Aug-2026 | Package 4f fix. The block pair ceiling joins the resolved
#                      settings, so a steward can see on screen what it is and
#                      why a block went uncompared.
# v1.1 | 10-Aug-2026 | Package 4f. resolve_settings also reports the fuzzy metric
#                      and the semantic model by name. Both were already in
#                      force and neither was visible, so a steward could not see
#                      on screen which comparison method actually ran.
# v1.0 | 04-Aug-2026 | Package 4b. Turns a steward's uniqueness settings plus the
#                      advisories from upstream agents into the settings the
#                      matcher will actually use. Pure: no data, no pandas, no
#                      language model, so every rule here is testable on its own.
# ---------------------------------------------------------------------------
"""Where a steward's settings meet the advice of other agents.

Two agents can send advice to the uniqueness stage, and the two work on
different things:

    RAISE_THRESHOLD  changes the SETTINGS. A compare field is thinly populated
                     across the whole table, so the match bands go up and every
                     pair must show stronger evidence.

    EXCLUDE_RECORDS  changes the DATA. Specific records hold a description that
                     failed a validity check, so they are held out of
                     deduplication altogether.

The second one prevents a severe failure. If twenty materials are all described
"TEST", their texts normalise to the same thing and score a perfect match
against each other. Without exclusion the agent would build one large cluster
of genuinely different materials and, because the match looks perfect,
recommend merging them without asking anybody.

An advisory is a signal, not a payload. It names the table and field that went
bad; it does not carry a list of record keys, which could run to thousands. The
keys are read from the findings the agents already produced.
"""

from __future__ import annotations

from typing import Any, Optional

from src.contracts import AdvisoryAction, Dimension
from src.data.schema import UniquenessConfig

# The six keys every advisory carries. Checked on the way in, so a producer that
# forgets one is caught here rather than three stages downstream.
ADVISORY_KEYS: tuple[str, ...] = ("action", "source", "table", "field", "value", "why")


def build_advisory(
    action: AdvisoryAction,
    source: str,
    table: str,
    field: str,
    why: str,
    value: Optional[float] = None,
) -> dict[str, Any]:
    """Make one advisory with all six keys present.

    Every producer goes through here, so no advisory can be missing a key or
    spell one differently. value is None for actions that carry no number.
    """
    return {
        "action": action.value,
        "source": source,
        "table": table,
        "field": field,
        "value": value,
        "why": why,
    }


def describe_advisory(advisory: dict[str, Any]) -> str:
    """One readable line for a screen or a console.

    The dictionary is what code reads; this is what a person reads.
    """
    action: str = str(advisory.get("action", "?"))
    source: str = str(advisory.get("source", "?"))
    target: str = f"{advisory.get('table', '?')}.{advisory.get('field', '?')}"
    value: Any = advisory.get("value")
    why: str = str(advisory.get("why", ""))
    head: str = ""

    if action == AdvisoryAction.RAISE_THRESHOLD.value:
        head = f"{source}: raise the match bands by {value} because of {target}"
    elif action == AdvisoryAction.EXCLUDE_RECORDS.value:
        head = f"{source}: hold back records failing validity on {target}"
    else:
        head = f"{source}: {action} on {target}"
    if why:
        return f"{head} ({why})"
    return head


def _check_advisory(advisory: dict[str, Any]) -> str:
    """Validate one advisory and return its action.

    An unknown action is an ERROR, not something to skip. A silently dropped
    advisory is the worst outcome available here: the upstream agent would
    believe its advice was taken, the report would list the advisory as
    delivered, and nothing would ever show the gap.
    """
    missing: list[str] = []
    key: str = ""
    action: str = ""

    for key in ADVISORY_KEYS:
        if key not in advisory:
            missing.append(key)
    if missing:
        raise ValueError(
            f"advisory is missing {', '.join(missing)}; build it with "
            f"build_advisory() so every key is present: {advisory}"
        )
    action = str(advisory["action"])
    if action not in {member.value for member in AdvisoryAction}:
        raise ValueError(
            f"unknown advisory action '{action}'; known actions are "
            f"{', '.join(sorted(member.value for member in AdvisoryAction))}"
        )
    return action


def resolve_settings(
    config: UniquenessConfig,
    advisories: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Work out the settings the matcher will really use.

    The steward's numbers come first and an advisory adjusts them. Every number
    involved is returned, so a screen can show what was set, what was asked for,
    and what resulted, and nobody concludes their setting was ignored.

    When two advisories both want to raise the bands, the LARGEST shift wins
    rather than the sum of them. Both are saying the same thing - the text
    signals are weak - so adding them would count one problem twice.
    """
    incoming: list[dict[str, Any]] = list(advisories or [])
    shift: float = 0.0
    applied: list[dict[str, Any]] = []
    exclusion_targets: list[dict[str, str]] = []
    advisory: dict[str, Any] = {}
    action: str = ""
    value: Any = None

    for advisory in incoming:
        action = _check_advisory(advisory)
        if action == AdvisoryAction.RAISE_THRESHOLD.value:
            value = advisory.get("value")
            if value is None:
                raise ValueError(
                    f"a {action} advisory must carry a value: {advisory}"
                )
            shift = max(shift, float(value))
            applied.append(advisory)
        elif action == AdvisoryAction.EXCLUDE_RECORDS.value:
            exclusion_targets.append({
                "table": str(advisory["table"]),
                "field": str(advisory["field"]),
            })
            applied.append(advisory)

    return {
        "bands": config.effective_bands(shift),
        "compare_fields": [
            {"field": entry.field, "weight": entry.weight}
            for entry in config.compare_fields
        ],
        "compare_weights": config.normalised_compare_weights(),
        "method_weights": config.normalised_method_weights(),
        "fuzzy_metric": config.methods.fuzzy.metric,  # v1.1
        "semantic_model": config.methods.semantic.model,  # v1.1
        "max_block_pairs": config.max_block_pairs,  # v1.2
        "blocking_keys": list(config.blocking_keys),
        "exclusion_targets": exclusion_targets,
        "advisories_applied": applied,
        "fingerprint": config.fingerprint(),
    }


def excluded_record_keys(
    settings: dict[str, Any],
    findings: Optional[list[Any]] = None,
) -> dict[str, set[str]]:
    """Collect the records to hold out of deduplication, table by table.

    The advisory says WHICH signal went bad; the findings say WHICH records it
    went bad on. A record with a validity finding on a compare field has a
    description that cannot be trusted as evidence of identity, so it takes no
    part in matching.

    The rule is deliberately simple: any validity finding on a compare field
    excludes that record. A finer rule - excluding only certain kinds of
    violation - would need a judgement we have no basis for yet.
    """
    targets: list[dict[str, str]] = list(settings.get("exclusion_targets", []))
    excluded: dict[str, set[str]] = {}
    wanted: set[tuple[str, str]] = set()
    target: dict[str, str] = {}
    finding: Any = None
    dimension: Any = None
    pair: tuple[str, str] = ("", "")

    if not targets or not findings:
        return excluded
    for target in targets:
        wanted.add((target["table"], target["field"]))
    for finding in findings:
        dimension = getattr(finding, "dimension", None)
        if dimension != Dimension.VALIDITY:
            continue
        pair = (getattr(finding, "table", ""), getattr(finding, "field", "") or "")
        if pair not in wanted:
            continue
        excluded.setdefault(pair[0], set()).add(str(getattr(finding, "record_id", "")))
    return excluded


def summarise_settings(settings: dict[str, Any], excluded: dict[str, set[str]]) -> str:
    """A short plain-language account of what the advisories changed.

    Used by the stub today and by a read-only panel later.
    """
    bands: dict[str, Any] = settings.get("bands", {})
    applied: list[dict[str, Any]] = settings.get("advisories_applied", [])
    held_back: int = sum(len(keys) for keys in excluded.values())
    parts: list[str] = []

    if not applied:
        return (
            f"no advisories; bands stay at {bands.get('duplicate')} / "
            f"{bands.get('review_low')}"
        )
    if bands.get("shift", 0.0):
        parts.append(
            f"bands {bands.get('steward_duplicate')} / {bands.get('steward_review_low')} "
            f"raised by {bands.get('shift')} to {bands.get('duplicate')} / "
            f"{bands.get('review_low')}"
        )
    if settings.get("exclusion_targets"):
        parts.append(f"{held_back} record(s) held back from deduplication")
    return "; ".join(parts) if parts else "advisories received, no change resulted"
