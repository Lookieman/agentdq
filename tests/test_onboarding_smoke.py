# ---------------------------------------------------------------------------
# tests/test_onboarding_smoke.py
# v1.0 | 13-Jul-2026 | Initial creation. Object packs (load, resolve, profiler
#                      v0.4 pack-wins-dict-falls-back) and the deterministic
#                      onboarding scaffolder run against a synthetic EQKT
#                      extract built in the SE16N export format (preamble,
#                      spacer column, blank separator row).
# ---------------------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from src.data.object_packs import load_object_pack, load_object_packs
from src.data.profiler import profile_table
from tools.onboard_object import detect_header, detect_key_candidates, map_roles, scaffold


REPO_ROOT = Path(__file__).resolve().parents[1]
ROLES_PATH = REPO_ROOT / "config" / "rule_bank" / "field_roles.yaml"
MANIFEST = REPO_ROOT / "config" / "reference" / "manifest.yaml"
PACKS_DIR = REPO_ROOT / "config" / "objects"


# ---------------------------------------------------------------------------
# Object packs
# ---------------------------------------------------------------------------

def test_packs_load_and_resolve():
    packs = load_object_packs(PACKS_DIR)
    assert {"MARA", "MARC", "MARD", "MAKT"}.issubset(set(packs))
    assert packs["MAKT"].primary_key == ["MATNR", "SPRAS"]
    assert packs["MARA"].uniqueness["blocking_key"] == "MTART"
    assert packs["MARC"].resolve_file("data/raw").name == "MARC_EX_DATA.xlsx"


def test_pack_requires_table_and_anchor(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("table: EQUI\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_object_pack(bad)


def test_profiler_uses_pack_key_over_fallback():
    # MAKT's fallback dict key is MATNR+SPRAS; give profile_table an explicit
    # override (as profile_files does from a pack) and confirm it wins.
    frame = pd.DataFrame({
        "MATNR": ["1", "1", "2"],
        "SPRAS": ["E", "D", "E"],
        "MAKTX": ["Bolt", "Schraube", "Nut"],
    })
    profile = profile_table(frame, "MAKT", primary_key=["MATNR", "SPRAS"])
    assert profile.key_uniqueness is not None
    assert profile.key_uniqueness.primary_key == ["MATNR", "SPRAS"]
    assert profile.key_uniqueness.is_unique is True

    # And a pack for a table with NO fallback entry still gets a key check.
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
    assert result["draft_pack"]["primary_key"] == result["key_candidates"][0]

    # Role mapping: SPRAS is a known language_key; EQUNR is unknown to the
    # material-centric vocabulary and routes to inference - by design.
    assert result["role_mapped"].get("SPRAS") == "language_key"
    assert "EQUNR" in result["role_unmapped"]

    # Reference readiness reports the language table for the mapped role.
    tables = [entry["reference_table"] for entry in result["reference_readiness"]]
    assert "T002" in tables

    # The draft pack carries TODO markers - a draft, never a silent config.
    assert result["draft_pack"]["uniqueness"]["blocking_key"] == "TODO"
    assert "{table}" in result["draft_pack"]["file_pattern"]


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
