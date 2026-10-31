# ---------------------------------------------------------------------------
# tests/test_remediation.py
# v1.0 | 11-Aug-2026 | Package 4h. Grouping, the rank arithmetic, the actions
#                      that come from the matcher's own accounting, and the
#                      narrative artefact: attached when it matches, refused
#                      when it is stale, absent when there is none.
# ---------------------------------------------------------------------------
"""The plan is deterministic; the model only ever owns the words."""

from __future__ import annotations

from src.agents.remediation import (
    MANDATORY_LIFT,
    attach_narrative,
    build_actions,
    group_findings,
    group_id_for,
    load_plan,
    save_plan,
)
from src.contracts import Dimension, Finding, Severity
from src.data.schema import load_schemas


def _finding(record: str, field: str = "MEINS", dimension=Dimension.COMPLETENESS,
             severity=Severity.HIGH, rule: str = "R1") -> Finding:
    return Finding(dimension=dimension, table="MARA", record_id=record,
                   field=field, rule_id=rule, issue="missing", severity=severity)


def _schemas():
    return load_schemas("config/schema", ["MARA"])


# ---------------------------------------------------------------------------
# Grouping and ranking
# ---------------------------------------------------------------------------

def test_thousands_of_findings_become_one_action_per_group():
    findings = [_finding(f"MATNR={i}") for i in range(300)]
    findings.extend(_finding(f"MATNR={i}", field="MATKL", rule="R2") for i in range(40))
    groups = group_findings(findings)
    actions = build_actions(findings, _schemas())
    assert len(groups) == 2
    assert len(actions) == 2
    assert actions[0].affected_records == 300


def test_the_rank_is_severity_times_records():
    # 10 Critical records outrank 15 Low ones: 4x10=40 against 1x15=15.
    findings = [_finding(f"MATNR=C{i}", field="MTART", severity=Severity.CRITICAL,
                         rule="RC") for i in range(10)]
    findings.extend(_finding(f"MATNR=L{i}", field="MATKL", severity=Severity.LOW,
                             rule="RL") for i in range(15))
    actions = build_actions(findings, _schemas())
    assert actions[0].field == "MTART"
    assert actions[0].rank == 1
    assert actions[1].field == "MATKL"


def test_a_mandatory_field_lifts_its_group():
    # Same severity, same count: the mandatory field must sit first, because
    # the business declared it must be filled. MEINS is mandatory in the MARA
    # schema; MATKL is not.
    schemas = _schemas()
    assert schemas["MARA"].fields["MEINS"].mandatory
    assert not schemas["MARA"].fields["MATKL"].mandatory
    findings = [_finding(f"MATNR=A{i}", field="MATKL", rule="RA") for i in range(10)]
    findings.extend(_finding(f"MATNR=B{i}", field="MEINS", rule="RB") for i in range(10))
    actions = build_actions(findings, schemas)
    assert actions[0].field == "MEINS"
    assert MANDATORY_LIFT > 1.0


def test_records_are_counted_once_per_group():
    findings = [_finding("MATNR=X"), _finding("MATNR=X")]
    actions = build_actions(findings, _schemas())
    assert actions[0].affected_records == 1


# ---------------------------------------------------------------------------
# Actions from the matcher's own accounting
# ---------------------------------------------------------------------------

def test_held_back_records_become_work():
    # A held-back record is not a clean record; it was never compared. The
    # validity reason names the cross-dimension link out loud.
    actions = build_actions([], _schemas(), uniqueness_summary={
        "held_back": {"validity_failed": 143, "no_description": 7},
        "mode": "full",
    })
    by_ref = {action.finding_refs[0]: action for action in actions}
    assert by_ref["validity_failed"].affected_records == 143
    assert "two dimensions" in by_ref["validity_failed"].action
    assert by_ref["no_description"].affected_records == 7


def test_a_fuzzy_only_run_asks_for_its_vectors():
    actions = build_actions([], _schemas(), uniqueness_summary={
        "held_back": {}, "mode": "fuzzy_only", "mode_reason": "no vector file",
        "records_assessed": 1200,
    })
    assert any("build_embeddings" in action.action for action in actions)


# ---------------------------------------------------------------------------
# The narrative artefact
# ---------------------------------------------------------------------------

def _one_action():
    return build_actions([_finding(f"MATNR={i}") for i in range(5)], _schemas())


def test_the_narrative_attaches_when_the_fingerprint_matches(tmp_path):
    actions = _one_action()
    plan = {
        "fingerprint": "fp-1",
        "executive_summary": "One field drives most of the gap.",
        "narratives": [{
            "group_id": group_id_for(actions[0]),
            "action": "Backfill MEINS from the purchasing records.",
            "rationale": "Five records; the field is mandatory.",
        }],
    }
    save_plan(tmp_path, "data/x", "config/rules", plan)
    loaded = load_plan(tmp_path, "data/x", "config/rules")
    outcome = attach_narrative(actions, loaded, fingerprint="fp-1")
    assert outcome["attached"] == 1
    assert outcome["executive_summary"].startswith("One field")
    assert actions[0].action == "Backfill MEINS from the purchasing records."


def test_a_stale_plan_is_refused_and_named_stale():
    # The same rule as the verdicts: made under other settings, it applies
    # nothing, and the caller says so on screen.
    actions = _one_action()
    before = actions[0].action
    outcome = attach_narrative(actions, {"fingerprint": "fp-OTHER",
                                         "narratives": [], "executive_summary": "x"},
                               fingerprint="fp-1")
    assert outcome["stale"] is True
    assert outcome["attached"] == 0
    assert actions[0].action == before


def test_no_plan_means_the_template_plan_stands(tmp_path):
    actions = _one_action()
    before = actions[0].action
    outcome = attach_narrative(actions, load_plan(tmp_path, "data/x", "config/rules"),
                               fingerprint="fp-1")
    assert outcome == {"attached": 0, "stale": False, "executive_summary": ""}
    assert actions[0].action == before


def test_the_model_never_owns_the_numbers():
    # A narrative can change the words. The affected count is arithmetic and
    # must survive any plan.
    actions = _one_action()
    outcome = attach_narrative(actions, {
        "fingerprint": "fp-1",
        "narratives": [{"group_id": group_id_for(actions[0]),
                        "action": "Do the thing.", "rationale": "Because."}],
        "executive_summary": "s",
    }, fingerprint="fp-1")
    assert outcome["attached"] == 1
    assert actions[0].affected_records == 5
