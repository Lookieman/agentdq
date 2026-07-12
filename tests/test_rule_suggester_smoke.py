# ---------------------------------------------------------------------------
# tests/test_rule_suggester_smoke.py
# v1.0 | 09-Jul-2026 | Initial creation. Tests the Rule Suggestion Agent: the
#                      decomposable confidence arithmetic, reference-domain
#                      instantiation, inferred RuleSpec assembly (validated
#                      against the real contracts), and both engines end-to-end
#                      via injected fake programs (no LLM / API key).
# ---------------------------------------------------------------------------
"""No LLM is called. The adjudicator and inferrer are injected fakes returning
canned decisions, so the full routing and both engines are exercised offline.
The inferred and instantiated rule IR is checked to round-trip through the real
RuleSpec contract - a suggestion that would not execute is dropped, by design."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.contracts import RuleSpec
from src.agents.profile_interpreter import FieldCharacterisation, TableInterpretation
from src.agents.rule_suggester import (
    CandidateSuggestion,
    RuleSuggester,
    assemble_inferred_rulespec,
    compute_confidence,
    coverage_score,
    field_values_from_profile,
    instantiate_bank_rulespec,
    reference_evidence,
    strength_prior_score,
)
from src.rules.reference_store import ReferenceStore, ReferenceTableMeta, build_reference_table
from src.rules.rule_bank import RuleBank


REPO_ROOT = Path(__file__).resolve().parents[1]
ROLES_PATH = REPO_ROOT / "config" / "rule_bank" / "field_roles.yaml"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _profile() -> dict:
    return {
        "table": "MARA",
        "row_count": 2798,
        "fields": {
            "MEINS": {
                "name": "MEINS", "type_hint": "categorical",
                "populated_count": 2798, "populated_pct": 100.0, "distinct_count": 5,
                "min_length": 1, "max_length": 3,
                "top_values": [{"value": "ST", "count": 1500}],
                "inferred_domain": ["ST", "KG", "L", "M", "EA"],
                "sample_values": ["ST", "KG"],
            },
            "ZZSTATUS": {
                "name": "ZZSTATUS", "type_hint": "categorical",
                "populated_count": 2798, "populated_pct": 100.0, "distinct_count": 3,
                "min_length": 1, "max_length": 1,
                "top_values": [{"value": "A", "count": 2700}],
                "inferred_domain": ["A", "B", "C"],
                "sample_values": ["A", "B"],
            },
        },
    }


def _interpretation() -> TableInterpretation:
    meins = FieldCharacterisation(
        field_name="MEINS", semantic_type_hypothesis="unit of measure",
        field_role_candidates=["unit_of_measure"], domain_candidacy="closed",
        anomaly_notes="", evidence_refs=[], role_coerced_to_unknown=False,
    )
    zz = FieldCharacterisation(
        field_name="ZZSTATUS", semantic_type_hypothesis="a status code",
        field_role_candidates=["unknown"], domain_candidacy="closed",
        anomaly_notes="", evidence_refs=[], role_coerced_to_unknown=True,
    )
    return TableInterpretation(
        table_name="MARA", field_characterisations=[meins, zz],
        health_summary="", concerns=[],
    )


def _reference_store() -> ReferenceStore:
    meta = ReferenceTableMeta(
        name="T006", value_column="MSEHI", key_columns=["MSEHI"], status="loaded",
        source_system="TESTSYS", extract_date="2026-07-09",
    )
    frame = pd.DataFrame({"MSEHI": ["ST", "KG", "L", "M", "EA"]})
    t006 = build_reference_table(meta, frame)
    return ReferenceStore(tables={"T006": t006})


@pytest.fixture()
def bank(tmp_path: Path) -> RuleBank:
    bank_dir = tmp_path / "rule_bank"
    bank_dir.mkdir()
    templates_yaml = """
version: 1
templates:
  - template_id: TPL-IS_MARA_MEINS_REFERENCE_EXISTS
    source_rule_id: IS_MARA_MEINS_REFERENCE_EXISTS
    rule_spec:
      rule_id: IS_MARA_MEINS_REFERENCE_EXISTS
      name: MEINS valid unit
      table: MARA
      dama_dimension: Validity
      archetype: reference_exists
      severity: Medium
      description: Base unit must be a valid unit.
      fields: [MEINS]
      assertion: {node: cmp, field: MEINS, op: in, value: [ST, KG]}
      executable: true
      provenance: {source: information_steward, reference_table: T006}
    provenance: {is_rule_id: IS_MARA_MEINS_REFERENCE_EXISTS, dimension: Validity}
    binding: {target_table: MARA, target_field: null, field_role: unit_of_measure}
    applicability:
      population_min: 0.95
      distinct_count_min: 1
      distinct_count_max: 60
      type_hint: categorical_string
    parameterisation:
      - {name: domain_values, source: reference}
    prior_strength:
      strength: strong
      strength_source: default
      strength_reason: proven_template
"""
    (bank_dir / "templates.yaml").write_text(templates_yaml, encoding="utf-8")
    return RuleBank.load(bank_dir, roles_path=ROLES_PATH)


class _FakeProgram:
    def __init__(self, prediction):
        self.prediction = prediction
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.prediction


def _accepting_adjudicator() -> _FakeProgram:
    return _FakeProgram(SimpleNamespace(
        accepted=True,
        rationale="Values match T006 cleanly; the few misses look like typos.",
        evidence_citations=["match_rate=1.0"],
    ))


def _rejecting_adjudicator() -> _FakeProgram:
    return _FakeProgram(SimpleNamespace(
        accepted=False, rationale="Value space looks like a different domain.",
        evidence_citations=[],
    ))


def _inferrer(should_suggest=True, risk="medium") -> _FakeProgram:
    return _FakeProgram(SimpleNamespace(
        should_suggest=should_suggest,
        proposed_archetype="domain_in",
        proposed_domain_values=["A", "B", "C"],
        rationale="Three recurring codes across a fully populated field.",
        evidence_citations=["distinct_count=3"],
        description_risk=risk,
    ))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_strength_prior_score():
    assert strength_prior_score("strong") == 1.0
    assert strength_prior_score("moderate") == 0.6
    assert strength_prior_score("weak") == 0.3
    assert strength_prior_score(None) == 0.3


def test_coverage_bands():
    assert coverage_score(2798) == 1.0
    assert coverage_score(300) == 0.8
    assert coverage_score(60) == 0.6
    assert coverage_score(20) == 0.4
    assert coverage_score(3) == 0.2


def test_confidence_is_decomposable():
    breakdown = compute_confidence(1.0, 1.0, 1.0, weights={"prior": 0.4, "support": 0.4, "coverage": 0.2})
    assert breakdown.confidence == 1.0
    assert breakdown.s_prior == 1.0 and breakdown.s_support == 1.0 and breakdown.s_coverage == 1.0
    # A weak inference on a tiny sample lands low even with clean support.
    weak = compute_confidence(0.3, 0.6, 0.2)
    assert weak.confidence == pytest.approx(0.4 * 0.3 + 0.4 * 0.6 + 0.2 * 0.2)


def test_field_values_prefers_inferred_domain():
    values = field_values_from_profile(_profile(), "MEINS")
    assert values == ["ST", "KG", "L", "M", "EA"]


def test_reference_evidence_match_and_nonmatch():
    store = _reference_store()
    rate, non_matching = reference_evidence(store, "T006", ["ST", "KG", "ZZZ"])
    assert rate == pytest.approx(2 / 3)
    assert non_matching == ["ZZZ"]
    # Not-loaded table -> None, not a false failure.
    assert reference_evidence(store, "T999", ["X"]) == (None, [])


# ---------------------------------------------------------------------------
# Inferred RuleSpec assembly (validated against real contracts)
# ---------------------------------------------------------------------------

def test_assemble_inferred_domain_in_round_trips():
    spec = assemble_inferred_rulespec("MARA", "ZZSTATUS", "domain_in", ["A", "B", "C"], "desc")
    assert spec is not None
    assert spec["dama_dimension"] == "Validity"
    assert spec["assertion"]["op"] == "in"
    assert spec["assertion"]["value"] == ["A", "B", "C"]
    assert spec["rule_id"] == "INF_MARA_ZZSTATUS_DOMAIN_IN"
    RuleSpec.model_validate(spec)  # must validate


def test_assemble_inferred_not_null_maps_to_completeness():
    spec = assemble_inferred_rulespec("MARA", "MATKL", "not_null", [], "must be populated")
    assert spec is not None
    assert spec["dama_dimension"] == "Completeness"
    assert spec["assertion"]["op"] == "is_not_null"
    RuleSpec.model_validate(spec)


def test_assemble_rejects_unsupported_and_empty_domain():
    assert assemble_inferred_rulespec("MARA", "X", "cross_field", [], "d") is None
    assert assemble_inferred_rulespec("MARA", "X", "domain_in", [], "d") is None


def test_instantiate_reference_rule_uses_live_domain(bank: RuleBank):
    template = bank.templates[0]
    live_values = ["EA", "KG", "L", "M", "ST"]
    spec = instantiate_bank_rulespec(template, live_values)
    # The template's synthetic [ST, KG] is replaced by the live reference domain.
    assert spec["assertion"]["value"] == live_values
    RuleSpec.model_validate(spec)


# ---------------------------------------------------------------------------
# Engine 1: bank match, end-to-end
# ---------------------------------------------------------------------------

def test_bank_match_accepted_produces_instantiated_candidate(bank: RuleBank):
    store = _reference_store()
    adjudicator = _accepting_adjudicator()
    # An inferrer that would fire if routing were wrong - it must NOT be called
    # for MEINS (which has a template).
    inferrer = _inferrer()
    suggester = RuleSuggester(bank=bank, reference_store=store, adjudicator=adjudicator, inferrer=inferrer)

    candidates = suggester.suggest(_profile(), _interpretation())
    bank_candidates = [c for c in candidates if c.origin == "bank_match"]
    assert len(bank_candidates) == 1

    candidate = bank_candidates[0]
    assert candidate.parameter_source == "reference"
    assert candidate.template_ref == "TPL-IS_MARA_MEINS_REFERENCE_EXISTS"
    assert candidate.description_risk == "low"
    # Instantiated with the LIVE T006 domain, not the template's synthetic pair.
    assert candidate.rule_spec["assertion"]["value"] == ["EA", "KG", "L", "M", "ST"]
    RuleSpec.model_validate(candidate.rule_spec)
    # Confidence: strong prior, full match, full coverage -> 1.0, and decomposable.
    assert candidate.confidence.confidence == 1.0
    assert candidate.confidence.s_prior == 1.0
    assert candidate.confidence.s_support == 1.0


def test_bank_match_rejected_yields_no_candidate(bank: RuleBank):
    store = _reference_store()
    suggester = RuleSuggester(
        bank=bank, reference_store=store,
        adjudicator=_rejecting_adjudicator(), inferrer=_inferrer(),
    )
    candidates = suggester.suggest(_profile(), _interpretation())
    assert [c for c in candidates if c.origin == "bank_match"] == []


# ---------------------------------------------------------------------------
# Engine 2: inference, end-to-end
# ---------------------------------------------------------------------------

def test_inference_produces_valid_candidate(bank: RuleBank):
    store = _reference_store()
    suggester = RuleSuggester(
        bank=bank, reference_store=store,
        adjudicator=_accepting_adjudicator(), inferrer=_inferrer(risk="medium"),
    )
    candidates = suggester.suggest(_profile(), _interpretation())
    inferred = [c for c in candidates if c.origin == "inferred"]
    assert len(inferred) == 1

    candidate = inferred[0]
    assert candidate.parameter_source == "data_derived"
    assert candidate.description_risk == "medium"
    assert candidate.rule_spec["assertion"]["value"] == ["A", "B", "C"]
    RuleSpec.model_validate(candidate.rule_spec)
    # Inference: weak prior (0.3), medium risk support (0.6), full coverage (1.0).
    expected = 0.4 * 0.3 + 0.4 * 0.6 + 0.2 * 1.0
    assert candidate.confidence.confidence == pytest.approx(round(expected, 4))
    assert candidate.confidence.s_prior == 0.3


def test_inference_declined_yields_nothing(bank: RuleBank):
    store = _reference_store()
    suggester = RuleSuggester(
        bank=bank, reference_store=store,
        adjudicator=_accepting_adjudicator(),
        inferrer=_inferrer(should_suggest=False),
    )
    candidates = suggester.suggest(_profile(), _interpretation())
    assert [c for c in candidates if c.origin == "inferred"] == []


def test_routing_sends_meins_to_bank_and_zzstatus_to_inference(bank: RuleBank):
    store = _reference_store()
    adjudicator = _accepting_adjudicator()
    inferrer = _inferrer()
    suggester = RuleSuggester(bank=bank, reference_store=store, adjudicator=adjudicator, inferrer=inferrer)
    suggester.suggest(_profile(), _interpretation())
    # MEINS (has template) went to the adjudicator; ZZSTATUS (unknown role) to the inferrer.
    adjudicated_fields = [call["field_summary"] for call in adjudicator.calls]
    inferred_fields = [call["field_summary"] for call in inferrer.calls]
    assert any("field=MEINS" in s for s in adjudicated_fields)
    assert any("field=ZZSTATUS" in s for s in inferred_fields)
    assert not any("field=ZZSTATUS" in s for s in adjudicated_fields)
