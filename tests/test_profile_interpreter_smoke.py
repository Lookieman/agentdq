# ---------------------------------------------------------------------------
# tests/test_profile_interpreter_smoke.py
# v1.0 | 09-Jul-2026 | Initial creation. Tests the Profiling Agent's
#                      deterministic scaffolding (role validation, evidence
#                      citations, the unknown escape hatch) and the full
#                      interpret() pipeline via an injected fake program, so no
#                      LLM or API key is needed. Also exercises the bridge from
#                      characterisations to rule-bank FieldObservations.
# v1.1 | 09-Jul-2026 | Fixtures moved to the real profiler shape; add a
#                      reconciliation test that runs src/data/profiler.py
#                      end-to-end and feeds its output through the seam.
# ---------------------------------------------------------------------------
"""These tests never call an LLM. The full pipeline is exercised by injecting a
fake program that returns a canned prediction (including a deliberately invalid
role), which lets us assert the grounding controls are code guarantees: the bad
role is dropped, the field falls back to 'unknown', and evidence citations come
from the profile rather than the model."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents.profile_interpreter import (
    ProfileInterpreter,
    build_evidence_refs,
    field_signals_from_profile,
    format_allowed_roles,
    normalise_characterisation,
    validate_role_candidates,
)
from src.rules.rule_bank import RuleBank


REPO_ROOT = Path(__file__).resolve().parents[1]
ROLES_PATH = REPO_ROOT / "config" / "rule_bank" / "field_roles.yaml"


# ---------------------------------------------------------------------------
# Fixtures: a small profile and an injected fake program
# ---------------------------------------------------------------------------

def _sample_profile() -> dict:
    # Mirrors src/data/profiler.py TableProfile.model_dump(): populated_pct on a
    # 0..100 scale, min_length/max_length, top_values as {value,count}, and the
    # profiler's own type_hint vocabulary (categorical / free_text / ...).
    return {
        "table": "MARA",
        "row_count": 2798,
        "field_count": 2,
        "key_uniqueness": None,
        "fields": {
            "MEINS": {
                "name": "MEINS",
                "type_hint": "categorical",
                "populated_count": 2798,
                "populated_pct": 100.0,
                "distinct_count": 17,
                "min_length": 1,
                "max_length": 3,
                "top_values": [{"value": "ST", "count": 1200}, {"value": "KG", "count": 800}],
                "inferred_domain": ["ST", "KG", "L", "M", "EA"],
                "sample_values": ["ST", "KG", "L", "M", "EA"],
            },
            "ZZORPHAN": {
                "name": "ZZORPHAN",
                "type_hint": "free_text",
                "populated_count": 1175,
                "populated_pct": 42.0,
                "distinct_count": 900,
                "min_length": 3,
                "max_length": 40,
                "top_values": [{"value": "free text a", "count": 5}],
                "inferred_domain": None,
                "sample_values": ["free text a", "free text b"],
            },
        },
    }


class _FakeProgram:
    """Stands in for a DSPy program: callable, returns a fixed prediction. No
    lambda (project style), and no DSPy needed at runtime."""

    def __init__(self, prediction):
        self.prediction = prediction
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.prediction


def _fake_prediction() -> SimpleNamespace:
    # MEINS gets a valid role plus an INVALID one (must be dropped). ZZORPHAN
    # gets only an invalid role (must fall back to 'unknown').
    characterisations = [
        {
            "field_name": "MEINS",
            "semantic_type_hypothesis": "a unit-of-measure code",
            "field_role_candidates": ["unit_of_measure", "totally_made_up_role"],
            "domain_candidacy": "closed",
            "anomaly_notes": "",
        },
        {
            "field_name": "ZZORPHAN",
            "semantic_type_hypothesis": "free text of some kind",
            "field_role_candidates": ["not_a_real_role"],
            "domain_candidacy": "open",
            "anomaly_notes": "populated on under half the rows",
        },
    ]
    return SimpleNamespace(
        field_characterisations=characterisations,
        health_summary="Base unit looks healthy at 100% populated; ZZORPHAN is sparse.",
        concerns=["ZZORPHAN populated on only 42% of rows"],
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_allowed_roles_render_includes_unknown():
    roles = format_allowed_roles(_load_roles())
    assert "unit_of_measure:" in roles
    assert "unknown:" in roles


def _load_roles():
    from src.rules.rule_bank import load_field_roles
    return load_field_roles(ROLES_PATH)


def test_field_signals_extracted():
    profile = _sample_profile()
    signals = field_signals_from_profile(profile, "MEINS")
    # population converted from populated_pct (100.0) to a 0..1 fraction.
    assert signals["population"] == 1.0
    assert signals["distinct_count"] == 17
    # type_hint translated from the profiler's 'categorical' to the bank vocab.
    assert signals["type_hint"] == "categorical_string"
    # length keys mapped from min_length/max_length.
    assert signals["max_value_length"] == 3
    assert signals["min_value_length"] == 1
    # null_count derived from row_count - populated_count.
    assert signals["null_count"] == 0
    # dominant-value share derived from top_values (1200 / 2798).
    assert signals["top_value_share"] == pytest.approx(1200 / 2798)


def test_sparse_field_population_scale():
    # The scale bug guard: 42.0 percent must become 0.42, not stay 42.
    profile = _sample_profile()
    signals = field_signals_from_profile(profile, "ZZORPHAN")
    assert signals["population"] == pytest.approx(0.42)
    assert signals["type_hint"] == "free_text"
    assert signals["null_count"] == 2798 - 1175


def test_field_signals_missing_field_degrades():
    profile = _sample_profile()
    signals = field_signals_from_profile(profile, "NOSUCHFIELD")
    assert signals["population"] is None
    assert signals["distinct_count"] is None


def test_evidence_refs_cite_raw_profile_values():
    # Evidence must cite the RAW profiler numbers (populated_pct on 0..100, the
    # raw type_hint), not the derived matching signals.
    profile = _sample_profile()
    refs = build_evidence_refs("MARA", "MEINS", profile)
    assert "profile.mara.meins.populated_pct=100.0" in refs
    assert "profile.mara.meins.distinct_count=17" in refs
    assert "profile.mara.meins.type_hint=categorical" in refs  # raw, not translated
    assert "profile.mara.meins.inferred_domain_size=5" in refs
    assert "profile.mara.meins.top_value=ST" in refs
    assert "profile.mara.meins.top_value_count=1200" in refs


def test_role_validation_drops_invalid():
    allowed = {"unit_of_measure", "material_group", "unknown"}
    validated, coerced = validate_role_candidates(
        ["unit_of_measure", "totally_made_up_role"], allowed
    )
    assert validated == ["unit_of_measure"]
    assert coerced is False


def test_role_validation_empty_falls_back_to_unknown():
    allowed = {"unit_of_measure", "unknown"}
    validated, coerced = validate_role_candidates(["not_a_real_role"], allowed)
    assert validated == ["unknown"]
    assert coerced is True


def test_normalise_attaches_evidence_and_validates():
    allowed = {"unit_of_measure", "unknown"}
    raw = {
        "field_name": "MEINS",
        "semantic_type_hypothesis": "unit",
        "field_role_candidates": ["unit_of_measure", "bogus"],
        "domain_candidacy": "closed",
        "anomaly_notes": "",
    }
    refs = ["profile.mara.meins.population=1.0"]
    char = normalise_characterisation(raw, allowed, refs)
    assert char.field_role_candidates == ["unit_of_measure"]
    assert char.evidence_refs == refs
    assert char.role_coerced_to_unknown is False


# ---------------------------------------------------------------------------
# Full pipeline via injected fake program (no LLM)
# ---------------------------------------------------------------------------

def test_interpret_end_to_end_enforces_grounding():
    profile = _sample_profile()
    program = _FakeProgram(_fake_prediction())
    interpreter = ProfileInterpreter(roles_path=ROLES_PATH, program=program)
    result = interpreter.interpret(profile)

    # Two characterisations, in profile order.
    names = [char.field_name for char in result.field_characterisations]
    assert names == ["MEINS", "ZZORPHAN"]

    meins = result.field_characterisations[0]
    orphan = result.field_characterisations[1]

    # Invalid role dropped; valid one kept.
    assert meins.field_role_candidates == ["unit_of_measure"]
    assert meins.role_coerced_to_unknown is False
    # Evidence citations attached from the real profile, not the model.
    assert any("meins.distinct_count=17" in ref for ref in meins.evidence_refs)

    # ZZORPHAN had only an invalid role -> unknown escape hatch fires.
    assert orphan.field_role_candidates == ["unknown"]
    assert orphan.role_coerced_to_unknown is True

    # Readout passes through.
    assert "healthy" in result.health_summary
    assert result.concerns and "42%" in result.concerns[0]

    # The program was actually called with the assembled inputs.
    assert program.calls and "table_profile" in program.calls[0]
    assert program.calls[0]["table_name"] == "MARA"


def test_interpret_input_carries_allowed_roles_and_profile():
    profile = _sample_profile()
    program = _FakeProgram(_fake_prediction())
    interpreter = ProfileInterpreter(roles_path=ROLES_PATH, program=program)
    interpreter.interpret(profile)
    call = program.calls[0]
    # The model is handed the controlled vocabulary and the profile as evidence.
    assert "unit_of_measure:" in call["allowed_roles"]
    assert "MEINS" in call["table_profile"]


# ---------------------------------------------------------------------------
# The socket: characterisations -> FieldObservations -> bank retrieval
# ---------------------------------------------------------------------------

@pytest.fixture()
def unit_bank(tmp_path: Path) -> RuleBank:
    bank_dir = tmp_path / "rule_bank"
    bank_dir.mkdir()
    templates_yaml = """
version: 1
templates:
  - template_id: TPL-IS_MARA_MEINS_REFERENCE_EXISTS
    source_rule_id: IS_MARA_MEINS_REFERENCE_EXISTS
    rule_spec: {rule_id: IS_MARA_MEINS_REFERENCE_EXISTS, table: MARA, fields: [MEINS]}
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


def test_characterisations_bridge_to_bank_retrieval(unit_bank: RuleBank):
    # The whole front half wired: profile -> interpret -> observations ->
    # retrieve returns the unit-of-measure template for MEINS.
    profile = _sample_profile()
    program = _FakeProgram(_fake_prediction())
    interpreter = ProfileInterpreter(roles_path=ROLES_PATH, program=program)
    interpretation = interpreter.interpret(profile)
    observations = interpreter.field_observations(profile, interpretation)

    by_name = {obs.field_name: obs for obs in observations}
    assert by_name["MEINS"].role_candidates == ["unit_of_measure"]
    assert by_name["MEINS"].distinct_count == 17

    results = unit_bank.retrieve(by_name["MEINS"])
    ids = [candidate.template.template_id for candidate in results]
    assert "TPL-IS_MARA_MEINS_REFERENCE_EXISTS" in ids

    # The orphan field (unknown role) retrieves nothing - correctly routed away
    # from the bank towards inference.
    orphan_results = unit_bank.retrieve(by_name["ZZORPHAN"])
    assert orphan_results == []


# ---------------------------------------------------------------------------
# Reconciliation against the real profiler (src/data/profiler.py)
# ---------------------------------------------------------------------------

def test_reconciles_with_real_profiler_output():
    # Run the actual profiler on a small frame and feed its genuine model_dump()
    # through the seam - proves the key names and scales line up for real.
    import pandas as pd
    from src.data.profiler import profile_table

    frame = pd.DataFrame(
        {
            "MATNR": ["100000001", "100000002", "100000003", "100000004"],
            "MEINS": ["ST", "KG", "ST", "KG"],
            "MAKTX": ["Bolt M8", "Nut M8", "Washer", None],
        }
    )
    table_profile = profile_table(frame, "MARA")
    profile = table_profile.model_dump()

    # MEINS: fully populated, low cardinality -> population 1.0, categorical_string.
    meins = field_signals_from_profile(profile, "MEINS")
    assert meins["population"] == 1.0
    assert meins["distinct_count"] == 2
    assert meins["type_hint"] == "categorical_string"

    # MATNR: profiler classifies a unique full column as 'key' -> 'identifier'.
    matnr = field_signals_from_profile(profile, "MATNR")
    assert matnr["type_hint"] == "identifier"

    # MAKTX: one null of four -> population 0.75, null_count 1 (derived).
    maktx = field_signals_from_profile(profile, "MAKTX")
    assert maktx["population"] == pytest.approx(0.75)
    assert maktx["null_count"] == 1

    # Evidence cites the raw profiler numbers (populated_pct on 0..100).
    refs = build_evidence_refs("MARA", "MEINS", profile)
    assert any("populated_pct=100.0" in ref for ref in refs)
