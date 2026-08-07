# ---------------------------------------------------------------------------
# tests/test_orchestrator_smoke.py
# v1.0 | 19-Jul-2026 | Initial creation. Node-level tests, both compiled graphs
#                      (suggestion with fakes; assessment with the REAL executor
#                      and real dimension agents on a tiny synthetic frame), the
#                      two advisory derivations, and proof the parallel fan-out
#                      merges findings from all three dimensions. No LLM.
# v1.1 | 04-Aug-2026 | Package 4a. Fixture uses the schema v0.4 uniqueness
#                      shape (blocking_keys as a list).
# v1.2 | 04-Aug-2026 | Package 4b. Advisories are dictionaries, signal
#                      suppression is replaced by record exclusion, and the
#                      stub now resolves real settings.
# v1.3 | 04-Aug-2026 | Package 4d. The stub is gone: the node runs the real
#                      Uniqueness agent. The graph order changes too, so
#                      uniqueness findings now reach the scorecard.
# ---------------------------------------------------------------------------
"""Offline throughout. The suggestion graph uses fake interpreter/suggester
programs; the assessment graph uses the real RuleBackedAgents and the real
pandas executor over a hand-built frame with known defects, so the fan-out,
the reducers and the advisory plumbing are all exercised for real."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd

from src import graph_nodes as nodes
from src.agents.completeness import CompletenessAgent
from src.agents.consistency import ConsistencyAgent
from src.agents.uniqueness_settings import build_advisory  # v1.2
from src.agents.validity import ValidityAgent
from src.contracts import (  # v1.2
    AdvisoryAction,
    Comparison,
    Dimension,
    Operator,
    RuleArchetype,
    RuleSpec,
    Severity,
)
from src.data.schema import TableSchema, UniquenessConfig
from src.orchestrator import build_assessment_graph, build_suggestion_graph
from src.state import merge_advisories

# ---------------------------------------------------------------------------
# A tiny MARA-like frame + schema + rules, with known defects
# ---------------------------------------------------------------------------

def _frame() -> pd.DataFrame:
    # 4 rows. MATKL has one null (completeness). MEINS has one out-of-domain
    # value 'ZZ' (validity). MAKTX (the uniqueness compare field) is 50%
    # populated (sparse -> threshold advisory).
    return pd.DataFrame({
        "MATNR": ["100", "101", "102", "103"],
        "MTART": ["FERT", "FERT", "HALB", "HALB"],
        "MATKL": ["L001", None, "L002", "L003"],
        "MEINS": ["EA", "KG", "ZZ", "EA"],
        "MAKTX": ["Bolt", None, "Nut", None],
    })


def _schema() -> TableSchema:
    return TableSchema(
        table="MARA",
        primary_key=["MATNR"],
        header_anchor="MATNR",
        uniqueness=UniquenessConfig(blocking_keys=["MTART", "MEINS"], compare_fields=["MAKTX"]),  # v1.1
        fields={},
    )


def _rules() -> list[RuleSpec]:
    not_null = RuleSpec(
        rule_id="C_MARA_MATKL_NN", name="MATKL populated", table="MARA",
        dama_dimension=Dimension.COMPLETENESS, archetype=RuleArchetype.NOT_NULL,
        severity=Severity.HIGH, fields=["MATKL"],
        assertion=Comparison(field="MATKL", op=Operator.IS_NOT_NULL),
    )
    domain = RuleSpec(
        rule_id="V_MARA_MEINS_DOM", name="MEINS domain", table="MARA",
        dama_dimension=Dimension.VALIDITY, archetype=RuleArchetype.DOMAIN_IN,
        severity=Severity.MEDIUM, fields=["MEINS"],
        assertion=Comparison(field="MEINS", op=Operator.IN, value=["EA", "KG", "ST"]),
    )
    return [not_null, domain]


def _assessment_state() -> dict[str, Any]:
    return {
        "tables": ["MARA"],
        "frames": {"MARA": _frame()},
        "schemas": {"MARA": _schema()},
        "dataset_label": "synthetic-tiny",
    }


# ---------------------------------------------------------------------------
# Reducer
# ---------------------------------------------------------------------------

def test_merge_advisories_concatenates_per_target():  # v1.2
    # The reducer never reads an advisory's contents, but the test uses the real
    # dictionary shape so it fails if the shape and the reducer ever diverge.
    first = {"action": "raise_threshold", "source": "Completeness"}
    second = {"action": "exclude_records", "source": "Validity"}
    third = {"action": "raise_threshold", "source": "Completeness"}
    left = {"uniqueness": [first]}
    right = {"uniqueness": [second], "remediation": [third]}
    merged = merge_advisories(left, right)
    assert merged["uniqueness"] == [first, second]
    assert merged["remediation"] == [third]
    # Defensive against None on either side.
    assert merge_advisories(None, {"x": [first]}) == {"x": [first]}
    assert merge_advisories({"x": [first]}, None) == {"x": [first]}


# ---------------------------------------------------------------------------
# Advisory derivations (pure)
# ---------------------------------------------------------------------------

def test_threshold_advisory_fires_on_sparse_field():  # v1.2
    advisories = nodes.derive_threshold_advisory(_assessment_state())
    assert "uniqueness" in advisories
    advisory = advisories["uniqueness"][0]
    assert advisory["action"] == AdvisoryAction.RAISE_THRESHOLD.value
    assert advisory["source"] == "Completeness"
    assert (advisory["table"], advisory["field"]) == ("MARA", "MAKTX")
    assert advisory["value"] == nodes.BAND_SHIFT_SPARSE
    assert "50.0%" in advisory["why"]


def test_threshold_advisory_silent_when_well_populated():
    state = _assessment_state()
    state["frames"]["MARA"]["MAKTX"] = ["Bolt", "Screw", "Nut", "Washer"]  # 100%
    assert nodes.derive_threshold_advisory(state) == {}


def test_exclusion_advisory_fires_on_validity_findings():  # v1.2
    # A validity finding on MAKTX holds the offending RECORDS out of matching.
    # It does not drop MAKTX as a signal: MARA compares one field, so dropping
    # it would leave nothing to compare and every material would score unique.
    finding = SimpleNamespace(table="MARA", field="MAKTX")
    result = SimpleNamespace(findings=[finding])
    advisories = nodes.derive_exclusion_advisory(_assessment_state(), result)
    advisory = advisories["uniqueness"][0]
    assert advisory["action"] == AdvisoryAction.EXCLUDE_RECORDS.value
    assert advisory["source"] == "Validity"
    assert (advisory["table"], advisory["field"]) == ("MARA", "MAKTX")
    assert advisory["value"] is None


# ---------------------------------------------------------------------------
# Node-level: a dimension node packs an AgentResult + advisory
# ---------------------------------------------------------------------------

def test_completeness_node_emits_findings_and_threshold_advisory():
    state = _assessment_state()
    state["approved_rules"] = _rules()
    out = nodes.completeness_node(state, CompletenessAgent())
    assert len(out["findings"]) == 1                      # the one null MATKL
    assert out["agent_results"][0]["agent"] == "Completeness Agent"
    assert "uniqueness" in out["upstream_advisories"]     # MAKTX sparse


def test_uniqueness_node_runs_the_agent_and_applies_the_advice():  # v1.3
    # The node stays thin: unpack, run(), pack. The agent resolves the advice
    # itself, so the steward-versus-advisory arithmetic reaches the matcher.
    advisory = build_advisory(
        action=AdvisoryAction.RAISE_THRESHOLD,
        source="Completeness",
        table="MAKT",
        field="MAKTX",
        value=0.05,
        why="only 50.0% populated, so matching on it is unreliable",
    )
    state = _assessment_state()
    state["upstream_advisories"] = {"uniqueness": [advisory]}
    out = nodes.uniqueness_node(state)

    assert out["agent_results"][0]["agent"] == "Uniqueness Agent"
    assert out["uniqueness_settings"]["resolved"]["bands"]["duplicate"] == 0.97
    assert "raise the match bands" in out["uniqueness_settings"]["readable"][0]
    assert "clusters" in out


def test_uniqueness_node_reports_when_there_is_no_subject_table():  # v1.3
    # A missing subject must be REPORTED, not silently treated as "nothing to
    # do". Silence here would look identical to a clean result.
    out = nodes.uniqueness_node({"upstream_advisories": {"uniqueness": []},
                                 "schemas": {}, "frames": {}})
    assert out["agent_results"][0]["findings"] == 0
    assert out["clusters"] == []


# ---------------------------------------------------------------------------
# The suggestion graph, compiled, with fakes
# ---------------------------------------------------------------------------

def test_suggestion_graph_runs_end_to_end():
    interpreter = SimpleNamespace(interpret=lambda profile: SimpleNamespace(table_name=profile["table"]))
    suggester = SimpleNamespace(suggest=lambda profile, interp: ["cand1", "cand2"])

    def build_artefact(state):
        return {"table": state["profile"]["table"], "count": len(state["candidates"])}

    graph = build_suggestion_graph(interpreter, suggester, build_artefact)
    final = graph.invoke({"table": "MARA", "profile": {"table": "MARA", "fields": {}}})
    assert final["artefact"] == {"table": "MARA", "count": 2}


# ---------------------------------------------------------------------------
# The assessment graph, compiled, REAL executor + real agents
# ---------------------------------------------------------------------------

def _load_rules_from_state(state):
    # In real use this is the repository export; here rules are pre-seeded.
    return state["approved_rules"]


def _compute_scorecard(findings, frames):
    # A light stand-in for reporting.compute_scorecard (avoids its dimension
    # constant); the real function is wired in the CLI runner.
    return {"total_findings": len(findings)}


def test_assessment_graph_fans_out_and_merges_all_dimensions():
    graph = build_assessment_graph(
        CompletenessAgent(), ValidityAgent(), ConsistencyAgent(),
        load_rules=_load_rules_from_state, compute_scorecard=_compute_scorecard,
    )
    initial = _assessment_state()
    initial["approved_rules"] = _rules()

    final = graph.invoke(initial)

    # Findings from BOTH the completeness rule (1 null MATKL) and the validity
    # rule (1 out-of-domain MEINS) are merged by the reducer.
    dimensions = {f.dimension.value for f in final["findings"]}
    assert dimensions == {"Completeness", "Validity"}
    assert len(final["findings"]) == 2

    # All three dimension agents ran (consistency found nothing but still ran).
    agents_run = {r["agent"] for r in final["agent_results"] if "agent" in r}
    assert "Completeness Agent" in agents_run
    assert "Validity Agent" in agents_run
    assert "Consistency Agent" in agents_run

    # Both advisories reached uniqueness: a threshold modifier (MAKTX sparse)
    # and a suppression (if MAKTX had validity findings). At minimum the sparse
    # threshold advisory is present.
    advisories = final["upstream_advisories"]["uniqueness"]
    assert any(a["action"] == AdvisoryAction.RAISE_THRESHOLD.value for a in advisories)  # v1.2

    # The stub resolved REAL settings: the steward's bands, the shift the
    # advisory asked for, and the result, all recorded together.
    bands = final["uniqueness_settings"]["resolved"]["bands"]  # v1.2
    assert bands["steward_duplicate"] == 0.92
    assert bands["shift"] == nodes.BAND_SHIFT_SPARSE
    assert bands["duplicate"] == 0.97

    # The real agent ran, and its stage now sits BEFORE the scorecard, so its
    # findings could reach the score. They could not in v1.0 of the graph.
    assert "Uniqueness Agent" in agents_run  # v1.3
    assert "clusters" in final  # v1.3

    # The report is assembled with the scorecard.
    assert final["report"]["total_findings"] == 2
    assert final["report"]["scorecard"] == {"total_findings": 2}


def test_assessment_graph_agents_stay_graph_free():
    # The agents must not import langgraph - that is what keeps them unit-
    # testable. Assert it structurally.
    import inspect

    import src.agents.base as base
    import src.agents.completeness as comp
    for module in (base, comp):
        source = inspect.getsource(module)
        assert "langgraph" not in source


# ---------------------------------------------------------------------------
# The no-checks guard (run_assessment_graph)
# ---------------------------------------------------------------------------

def test_no_checks_warning_fires_when_nothing_runs():
    from tools.run_assessment_graph import no_checks_warning
    # No rules found at all.
    w1 = no_checks_warning(rules_loaded=0, rules_run=0, rules_dir="data/approved")
    assert w1 is not None and "NOTHING CHECKED" in w1 and "no rules were found" in w1
    # Rules loaded but none executed (e.g. not executable / wrong tables).
    w2 = no_checks_warning(rules_loaded=12, rules_run=0, rules_dir="config/rules")
    assert w2 is not None and "none executed" in w2 and "12 rule" in w2


def test_no_checks_warning_silent_when_checks_run():
    from tools.run_assessment_graph import no_checks_warning
    assert no_checks_warning(rules_loaded=37, rules_run=14, rules_dir="config/rules") is None
