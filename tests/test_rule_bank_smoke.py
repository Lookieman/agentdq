# ---------------------------------------------------------------------------
# tests/test_rule_bank_smoke.py
# v1.0 | 05-Jul-2026 | Initial creation. Smoke tests for the deterministic
#                      foundation: field-role vocabulary, rule-bank retrieval
#                      join (recall floor behaviour), and the reference store
#                      (membership, match-rate, as-of metadata, scoped tables).
# v1.1 | 05-Jul-2026 | Add build_rule_bank adapter tests locking the real
#                      RuleSpec shape: reference vs template_fixed sourcing,
#                      fields[]/assertion.value reading, role resolution.
# v1.2 | 05-Jul-2026 | Reference-store tests moved to the xlsx path: in-memory
#                      frames via build_reference_table for logic, plus a real
#                      integration test loading the shipped CAL extracts.
# v1.3 | 09-Jul-2026 | Make the real-manifest test state-robust (partition
#                      invariants, not hard-coded pending list) so it survives
#                      as more extracts are loaded; add real T023/T137 coverage
#                      and a scoped T024D membership test.
# ---------------------------------------------------------------------------
"""These tests use built-in fixtures and touch no LLM and no real CAL extract,
so they run offline and without API keys. They assert the two behaviours that
matter most for the design: retrieval is generous (a dirty field still gets
retrieved), and reference membership distinguishes 'not a member' from 'table
not loaded'."""

from __future__ import annotations

from pathlib import Path

import pytest

import pandas as pd  # v1.2

from src.rules.rule_bank import (
    FieldObservation,
    RetrievalConfig,
    RuleBank,
    load_field_roles,
)
from src.rules.reference_store import (  # v1.2
    ReferenceStore,
    ReferenceTable,
    ReferenceTableMeta,
    build_reference_table,
)
from tools.build_rule_bank import build_templates  #v1.1


REPO_ROOT = Path(__file__).resolve().parents[1]
ROLES_PATH = REPO_ROOT / "config" / "rule_bank" / "field_roles.yaml"
REFERENCE_MANIFEST = REPO_ROOT / "config" / "reference" / "manifest.yaml"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def bank(tmp_path: Path) -> RuleBank:
    # A tiny two-template bank written to a temp dir, plus the real roles file.
    bank_dir = tmp_path / "rule_bank"
    bank_dir.mkdir()
    templates_yaml = """
version: 1
templates:
  - template_id: TPL-APN-0042
    source_rule_id: APN-0042
    rule_spec: {rule_id: APN-0042, table: MARA, field: MEINS, archetype: domain_in}
    provenance: {is_rule_id: APN-0042, dimension: Validity}
    binding: {target_table: MARA, target_field: null, field_role: unit_of_measure}
    applicability:
      population_min: 0.95
      reference_match_min: 0.90
      distinct_count_min: 1
      distinct_count_max: 60
      type_hint: categorical_string
    parameterisation:
      - {name: domain_values, source: reference}
    prior_strength:
      strength: strong
      strength_source: default
      strength_reason: proven_template
  - template_id: TPL-APN-0101
    source_rule_id: APN-0101
    rule_spec: {rule_id: APN-0101, table: MARA, field: MATKL, archetype: not_null}
    provenance: {is_rule_id: APN-0101, dimension: Completeness}
    binding: {target_table: MARA, target_field: MATKL, field_role: material_group}
    applicability:
      population_min: 0.98
    parameterisation: []
    prior_strength:
      strength: strong
      strength_source: default
      strength_reason: proven_template
"""
    (bank_dir / "templates.yaml").write_text(templates_yaml, encoding="utf-8")
    return RuleBank.load(bank_dir, roles_path=ROLES_PATH)


@pytest.fixture()
def scoped_store() -> ReferenceStore:  # v1.2
    # Build the store from in-memory frames (via the pure indexing seam), so the
    # membership/scope/match-rate logic is tested without any file IO.
    t006_meta = ReferenceTableMeta(
        name="T006", value_column="MSEHI", key_columns=["MSEHI"], status="loaded",
        source_system="TESTSYS", extract_date="2026-07-05",
    )
    t006_frame = pd.DataFrame({"MANDT": ["100"] * 5, "MSEHI": ["ST", "KG", "L", "M", "EA"]})
    t006 = build_reference_table(t006_meta, t006_frame)

    t024d_meta = ReferenceTableMeta(
        name="T024D", value_column="DISPO", key_columns=["WERKS", "DISPO"],
        scope_columns=["WERKS"], status="loaded",
    )
    t024d_frame = pd.DataFrame({"WERKS": ["1000", "1000", "2000"], "DISPO": ["100", "101", "200"]})
    t024d = build_reference_table(t024d_meta, t024d_frame)

    t023_meta = ReferenceTableMeta(
        name="T023", value_column="MATKL", key_columns=["MATKL"], status="pending_extract",
    )
    t023 = ReferenceTable(meta=t023_meta)

    return ReferenceStore(tables={"T006": t006, "T024D": t024d, "T023": t023})


# ---------------------------------------------------------------------------
# Field-role vocabulary
# ---------------------------------------------------------------------------

def test_roles_load_and_unknown_present():
    roles = load_field_roles(ROLES_PATH)
    assert "unit_of_measure" in roles
    assert "unknown" in roles, "the escape-hatch role must exist"
    assert roles["unit_of_measure"]["reference_table"] == "T006"


# ---------------------------------------------------------------------------
# Retrieval join
# ---------------------------------------------------------------------------

def test_clean_field_retrieves_unit_template(bank: RuleBank):
    observation = FieldObservation(
        table="MARA",
        field_name="MEINS",
        role_candidates=["unit_of_measure"],
        population=1.0,
        distinct_count=17,
        type_hint="categorical_string",
        reference_match_rate=0.97,
    )
    results = bank.retrieve(observation)
    ids = [candidate.template.template_id for candidate in results]
    assert "TPL-APN-0042" in ids


def test_dirty_field_still_retrieved_above_floor(bank: RuleBank):
    # The core design claim: a field with a 6% typo rate (match 0.94, population
    # 0.84) is still RETRIEVED, because retrieval uses the 0.80 floor, not the
    # template's stricter 0.90/0.95 ideals. Missing this rule is the whole point.
    observation = FieldObservation(
        table="MARA",
        field_name="MEINS",
        role_candidates=["unit_of_measure"],
        population=0.84,
        distinct_count=22,
        type_hint="categorical_string",
        reference_match_rate=0.94,
    )
    results = bank.retrieve(observation)
    ids = [candidate.template.template_id for candidate in results]
    assert "TPL-APN-0042" in ids, "dirty-but-plausible field must survive retrieval"


def test_field_below_floor_is_excluded(bank: RuleBank):
    # A genuinely non-conforming field (match 0.50) should NOT retrieve.
    observation = FieldObservation(
        table="MARA",
        field_name="MEINS",
        role_candidates=["unit_of_measure"],
        population=0.99,
        distinct_count=22,
        type_hint="categorical_string",
        reference_match_rate=0.50,
    )
    results = bank.retrieve(observation)
    ids = [candidate.template.template_id for candidate in results]
    assert "TPL-APN-0042" not in ids


def test_wrong_table_does_not_match(bank: RuleBank):
    observation = FieldObservation(
        table="MARC",
        field_name="MEINS",
        role_candidates=["unit_of_measure"],
        population=1.0,
        reference_match_rate=0.99,
    )
    results = bank.retrieve(observation)
    assert results == []


def test_explicit_field_binding(bank: RuleBank):
    # MATKL template binds by explicit field name, not role.
    observation = FieldObservation(
        table="MARA",
        field_name="MATKL",
        role_candidates=["material_group"],
        population=0.99,
    )
    results = bank.retrieve(observation)
    ids = [candidate.template.template_id for candidate in results]
    assert "TPL-APN-0101" in ids


def test_absent_rate_evidence_does_not_exclude(bank: RuleBank):
    # If reference_match_rate is unknown, the template must not be excluded on
    # that ground - absent evidence is not a failed check.
    observation = FieldObservation(
        table="MARA",
        field_name="MEINS",
        role_candidates=["unit_of_measure"],
        population=0.99,
        reference_match_rate=None,
    )
    results = bank.retrieve(observation)
    ids = [candidate.template.template_id for candidate in results]
    assert "TPL-APN-0042" in ids


def test_highlight_floor_not_used_at_retrieval():
    config = RetrievalConfig()
    assert config.rate_floor == 0.80
    assert config.highlight_floor == 0.95


# ---------------------------------------------------------------------------
# Reference store
# ---------------------------------------------------------------------------

def test_reference_membership(scoped_store: ReferenceStore):
    assert scoped_store.is_member("T006", "kg") is True   # normalisation: case
    assert scoped_store.is_member("T006", " KG ") is True  # normalisation: spaces
    assert scoped_store.is_member("T006", "XYZ") is False


def test_reference_match_rate(scoped_store: ReferenceStore):
    values = ["ST", "KG", "XYZ", "L"]  # 3 of 4 valid
    rate = scoped_store.match_rate("T006", values)
    assert rate == pytest.approx(0.75)


def test_pending_table_returns_none_not_false(scoped_store: ReferenceStore):
    # The critical distinction: a not-yet-extracted table yields None
    # (unknown), never False (which would masquerade as a failed check).
    assert scoped_store.is_member("T023", "ANYTHING") is None
    assert scoped_store.match_rate("T023", ["A", "B"]) is None
    assert "T023" in scoped_store.pending_tables()


def test_scoped_membership(scoped_store: ReferenceStore):
    # T024D: controller 100 is valid in plant 1000 but not plant 2000.
    assert scoped_store.is_member("T024D", "100", scope=("1000",)) is True
    assert scoped_store.is_member("T024D", "100", scope=("2000",)) is False
    assert scoped_store.is_member("T024D", "200", scope=("2000",)) is True


def test_as_of_metadata_present(scoped_store: ReferenceStore):
    meta = scoped_store.get_meta("T006")
    assert meta.source_system == "TESTSYS"
    assert meta.extract_date == "2026-07-05"


def test_real_manifest_loads_and_partitions_cleanly():  # v1.3
    # State-robust: does NOT hard-code which tables are loaded, because that
    # legitimately changes as extracts are added. Asserts the invariants that
    # must always hold whatever the load state.
    store = ReferenceStore.load(REFERENCE_MANIFEST)
    declared = {
        "T006", "T001W", "T134", "T023", "T137",
        "T002", "T024", "T024D", "T438A", "T141",
    }
    loaded = set()
    pending = set()
    name = ""

    assert declared.issubset(set(store.tables)), "all ten tables must be registered"

    loaded = {name for name in store.tables if store.is_loaded(name)}
    pending = set(store.pending_tables())
    # Loaded and pending partition the set: no overlap, and together they cover it.
    assert loaded.isdisjoint(pending)
    assert loaded | pending == set(store.tables)
    # A loaded table answers membership with a bool; a pending one with None.
    for name in loaded:
        assert isinstance(store.is_member(name, "___probe___"), bool)
    for name in pending:
        assert store.is_member(name, "___probe___") is None


def test_real_extracts_load_via_extract_loader():
    # End-to-end through extract_loader on the actual CAL exports: SE16N preamble
    # stripped, internal codes and leading zeros preserved.
    store = ReferenceStore.load(REFERENCE_MANIFEST)
    # T006 holds internal UoM codes such as '%'.
    assert store.is_member("T006", "%") is True
    assert store.is_member("T006", "NOT_A_UNIT") is False
    # T001W plant codes keep their leading zeros ('0001', not '1').
    assert store.is_member("T001W", "0001") is True
    assert store.is_member("T001W", "1") is False
    # T134 material types include the usual suspects.
    assert store.is_member("T134", "HALB") is True
    # T023 material groups and T137 industry sectors (real values).
    assert store.is_member("T023", "A001") is True
    assert store.is_member("T137", "A") is True


def test_real_scoped_membership_t024d():  # v1.3
    # T024D is plant-scoped: an MRP controller is valid FOR a given plant. DISPO
    # '000' exists in plant 1010 but not in plant 0001 - the scope must bite.
    store = ReferenceStore.load(REFERENCE_MANIFEST)
    if not store.is_loaded("T024D"):
        pytest.skip("T024D extract not present in this checkout")
    assert store.is_member("T024D", "000", scope=("1010",)) is True
    assert store.is_member("T024D", "000", scope=("0001",)) is False
    assert store.is_member("T024D", "001", scope=("0001",)) is True


# ---------------------------------------------------------------------------
# build_rule_bank adapter: wrapping the REAL serialised RuleSpec shape  v1.1
# ---------------------------------------------------------------------------

# These mirror is_importer's on-disk output (model_dump_json, exclude_none):
# fields[], assertion{op,value}, provenance{}, and absent keys where None.
_REFERENCE_RULE = {
    "rule_id": "IS_MARA_MEINS_REFERENCE_EXISTS",
    "table": "MARA",
    "dama_dimension": "Validity",
    "archetype": "reference_exists",
    "description": "Base unit of measure must be a valid unit.",
    "fields": ["MEINS"],
    "assertion": {"field": "MEINS", "op": "in", "value": ["ST", "KG", "L", "M", "EA"]},
    "provenance": {
        "source": "information_steward",
        "original_expression": "exists(MEINS in T006)",
        "reference_table": "T006",
    },
}

_INLINE_DOMAIN_RULE = {
    "rule_id": "IS_MARC_BESKZ_DOMAIN_IN",
    "table": "MARC",
    "dama_dimension": "Validity",
    "archetype": "domain_in",
    "description": "Procurement type must be one of E, F, X.",
    "fields": ["BESKZ"],
    "assertion": {"field": "BESKZ", "op": "in", "value": ["E", "F", "X"]},
    "provenance": {"source": "information_steward", "original_expression": "BESKZ IN ('E','F','X')"},
}

_NOT_NULL_RULE = {
    "rule_id": "IS_MARA_MATKL_NOT_NULL",
    "table": "MARA",
    "dama_dimension": "Completeness",
    "archetype": "not_null",
    "description": "Material group must be populated.",
    "fields": ["MATKL"],
    "assertion": {"field": "MATKL", "op": "is_not_null"},
    "provenance": {"source": "information_steward", "original_expression": "MATKL IS NOT NULL"},
}


def test_adapter_reference_rule_sourced_from_provenance():
    # A rule whose provenance names a check table must wrap as `reference`, and
    # gain a reference_match_min applicability signal.
    roles = load_field_roles(ROLES_PATH)
    templates = build_templates([_REFERENCE_RULE], roles)
    template = templates[0]
    assert template["binding"]["field_role"] == "unit_of_measure"
    assert template["parameterisation"][0]["source"] == "reference"
    assert template["applicability"].get("reference_match_min") is not None
    # Domain size drives the distinct-count band (5 values here).
    assert template["applicability"]["distinct_count_max"] >= 5


def test_adapter_inline_domain_is_template_fixed():
    # Inline values with no check table must wrap as `template_fixed` and carry
    # no reference_match_min - there is nothing external to match against.
    roles = load_field_roles(ROLES_PATH)
    templates = build_templates([_INLINE_DOMAIN_RULE], roles)
    template = templates[0]
    assert template["parameterisation"][0]["source"] == "template_fixed"
    assert "reference_match_min" not in template["applicability"]


def test_adapter_reads_fields_and_dimension():
    # fields[0] is the bound field; dama_dimension feeds provenance.dimension;
    # original_expression is read from the nested provenance block.
    roles = load_field_roles(ROLES_PATH)
    templates = build_templates([_REFERENCE_RULE], roles)
    template = templates[0]
    assert template["binding"]["target_field"] == "MEINS"
    assert template["provenance"]["dimension"] == "Validity"
    assert template["provenance"]["original_expression"] == "exists(MEINS in T006)"


def test_adapter_not_null_population_signal_no_params():
    roles = load_field_roles(ROLES_PATH)
    templates = build_templates([_NOT_NULL_RULE], roles)
    template = templates[0]
    assert template["applicability"]["population_min"] == 0.98
    assert template["parameterisation"] == []
    assert template["prior_strength"]["strength"] == "strong"
    assert template["prior_strength"]["strength_reason"] == "proven_template"
