# ---------------------------------------------------------------------------
# tests/test_onboarding_smoke.py
# v1.0 | 13-Jul-2026 | Initial creation. Onboarding config + the deterministic
#                      scaffolder, run against a synthetic EQKT extract in the
#                      SE16N export format (preamble, spacer column, blank
#                      separator row).
# v2.0 | 13-Jul-2026 | Config source moved from the retired config/objects to
#                      config/schema, which already carried primary_key and
#                      header_anchor. Scaffolder now emits a draft SCHEMA.
# ---------------------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from src.data.schema import load_all_schemas, load_table_schema
from src.data.profiler import profile_table
from tools.onboard_object import detect_header, detect_key_candidates, map_roles, scaffold


REPO_ROOT = Path(__file__).resolve().parents[1]
ROLES_PATH = REPO_ROOT / "config" / "rule_bank" / "field_roles.yaml"
MANIFEST = REPO_ROOT / "config" / "reference" / "manifest.yaml"
SCHEMA_DIR = REPO_ROOT / "config" / "schema"


# ---------------------------------------------------------------------------
# Onboarding config lives in the SCHEMA (not a parallel object pack)
# ---------------------------------------------------------------------------

def test_schema_carries_all_onboarding_fields():
    # ONE file per table is what a steward writes. The schema already had
    # primary_key and header_anchor; v0.3 added file_pattern and uniqueness.
    schemas = load_all_schemas(SCHEMA_DIR)
    assert {"MARA", "MARC"}.issubset(set(schemas))
    mara = schemas["MARA"]
    assert mara.primary_key == ["MATNR"]
    assert mara.header_anchor == "MATNR"
    assert mara.resolve_file("data/raw").name == "MARA_EX_DATA.xlsx"
    assert mara.uniqueness.blocking_key == "MTART"
    assert mara.uniqueness.compare_fields == ["MAKT.MAKTX"]
    # MARC has no uniqueness block: absent config degrades to None, not a crash.
    assert schemas["MARC"].primary_key == ["MATNR", "WERKS"]
    assert schemas["MARC"].uniqueness.blocking_key is None


def test_schema_field_role_is_structural_not_semantic():
    # A naming collision worth guarding: schema FieldSpec.role is STRUCTURAL
    # (key/attribute/flag/temporal/client) and drives parsing and generation.
    # The rule bank's field_role is SEMANTIC (unit_of_measure, ...) and drives
    # template retrieval. Same word, different vocabularies - do not conflate.
    mara = load_table_schema(str(SCHEMA_DIR / "mara.yaml"))
    assert mara.field("MATNR").role == "key"
    assert mara.field("MEINS").role == "attribute"     # NOT unit_of_measure
    assert mara.field("LVORM").role == "flag"


def test_profiler_uses_schema_key_over_fallback():
    # The schema's primary_key wins over the historical hardcoded dict.
    frame = pd.DataFrame({
        "MATNR": ["1", "1", "2"],
        "SPRAS": ["E", "D", "E"],
        "MAKTX": ["Bolt", "Schraube", "Nut"],
    })
    profile = profile_table(frame, "MAKT", primary_key=["MATNR", "SPRAS"])
    assert profile.key_uniqueness is not None
    assert profile.key_uniqueness.primary_key == ["MATNR", "SPRAS"]
    assert profile.key_uniqueness.is_unique is True

    # A table with NO fallback dict entry still gets a key check from its schema.
    equi_frame = pd.DataFrame({"EQUNR": ["10", "11"], "EQART": ["P", "P"]})
    equi_profile = profile_table(equi_frame, "EQUI", primary_key=["EQUNR"])
    assert equi_profile.key_uniqueness.is_unique is True


# ---------------------------------------------------------------------------
# The scaffolder, on a synthetic SE16N-format EQKT extract
# ---------------------------------------------------------------------------

def _write_se16n_xlsx(path: Path) -> None:
    """Reproduce the SE16N export shape: preamble, header row with a leading
    spacer column, one blank separator, then data. EQKT keyed on EQUNR+SPRAS,
    with leading zeros on EQUNR."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    rows = [
        [None],
        [None, "Table:", "EQKT"],
        [None, "Displayed Fields:", "5 of 5"],
        [None],
        [None, "MANDT", "EQUNR", "SPRAS", "EQKTX", "KZLTX"],
        [None],
        [None, "100", "000000000010", "E", "Pump motor", ""],
        [None, "100", "000000000010", "D", "Pumpenmotor", ""],
        [None, "100", "000000000011", "E", "Gearbox", "X"],
        [None, "100", "000000000012", "E", "Compressor", ""],
    ]
    for row in rows:
        sheet.append(row)
    workbook.save(str(path))


def test_scaffold_detects_header_key_and_roles(tmp_path: Path):
    xlsx = tmp_path / "EQKT_EX_DATA.xlsx"
    _write_se16n_xlsx(xlsx)

    result = scaffold(xlsx, "EQKT", roles_path=ROLES_PATH, manifest_path=MANIFEST)

    # Header found at row 5, data rows counted correctly.
    assert result["header_row"] == 5
    assert result["row_count"] == 4

    # Key detection: EQUNR alone is NOT unique (two languages for equipment
    # 10); the composite EQUNR+SPRAS is - arithmetic, no AI needed.
    assert ["EQUNR", "SPRAS"] in result["key_candidates"]
    assert ["EQUNR"] not in result["key_candidates"]
    assert result["draft_schema"]["primary_key"] == result["key_candidates"][0]

    # Role mapping: SPRAS is a known language_key; EQUNR is unknown to the
    # material-centric vocabulary and routes to inference - by design.
    assert result["role_mapped"].get("SPRAS") == "language_key"
    assert "EQUNR" in result["role_unmapped"]

    # Reference readiness reports the language table for the mapped role.
    tables = [entry["reference_table"] for entry in result["reference_readiness"]]
    assert "T002" in tables

    # The draft SCHEMA carries TODO markers - a draft, never a silent config.
    draft = result["draft_schema"]
    assert draft["uniqueness"]["blocking_key"] == "TODO"
    assert "{table}" in draft["file_pattern"]
    assert draft["description"].startswith("TODO")

    # Field blocks are seeded from a real profile: SPRAS is a 2-value code,
    # mandatory (fully populated), with its observed domain proposed.
    spras = draft["fields"]["SPRAS"]
    assert spras["mandatory"] is True
    assert sorted(spras["domain"]) == ["D", "E"]
    assert spras["description"] == "TODO"          # judgement, not scanned

    # KZLTX is sparsely populated -> not proposed as mandatory.
    assert draft["fields"]["KZLTX"]["mandatory"] is False

    # And the draft VALIDATES as a real schema once loaded.
    from src.data.schema import TableSchema
    for name, block in draft["fields"].items():
        block["name"] = name
    TableSchema.model_validate(draft)


def test_detect_header_rejects_non_se16n(tmp_path: Path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["just", "some", "text"])  # lowercase: not SAP field names
    path = tmp_path / "not_an_export.xlsx"
    workbook.save(str(path))
    with pytest.raises(ValueError):
        detect_header(path)


def test_key_detection_ignores_constants_and_gaps():
    frame = pd.DataFrame({
        "MANDT": ["100", "100", "100"],      # excluded by name
        "CONST": ["X", "X", "X"],            # constant: cannot key
        "GAPPY": ["A", None, "B"],           # nullable: cannot key
        "IDCOL": ["1", "2", "3"],            # the honest key
    })
    candidates = detect_key_candidates(frame)
    assert candidates == [["IDCOL"]]
