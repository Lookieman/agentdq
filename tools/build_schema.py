# v0.1 | 27-Jun-2026 | Schema scaffolder: merge SAP overlay with profiler output

# v0.2 | 27-Jun-2026 | Omit empty domains (no values found != nothing allowed)
# v0.3 | 04-Aug-2026 | Package 4a. TABLE_META now carries file_pattern and,
#                      for MARA, the uniqueness block. Both were hand-added
#                      to mara.yaml and were erased on every rebuild.
"""One-off scaffolder that writes the table schema YAMLs.

It merges two sources:

- a curated overlay carrying the SAP knowledge that cannot be inferred from
  data alone (field descriptions, semantic type, baseline mandatory status);
- the profiler output, which supplies the observed domain, population rate and
  value lengths straight from the real extracts.

The semantic type in the overlay is authoritative: it overrides the profiler's
coarse type hint. This matters for the quantity fields (MABST, MINBE, BSTFE),
which the profiler sees as categorical because their comma-formatted values look
like discrete codes, and for date fields whose sentinel values look the same.

Run once, then treat config/schema/*.yaml as the editable source of truth:

    python -m tools.build_schema --profiles data/profile --out config/schema
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

import yaml


# Per-table formatting, confirmed from the extracts: European decimals with a
# comma decimal separator and a dot thousands separator, dates as MM/DD/YYYY,
# and SAP's empty-date sentinel rendered as 00/00/0000.
TABLE_FORMATTING: dict[str, dict[str, Any]] = {
    "MARA": {
        "decimal_separator": ",",
        "thousands_separator": ".",
        "date_format": "%m/%d/%Y",
        "null_date_sentinels": ["00/00/0000"],
    },
    "MARC": {
        "decimal_separator": ",",
        "thousands_separator": ".",
        "date_format": "%m/%d/%Y",
        "null_date_sentinels": ["00/00/0000"],
    },
    "MAKT": {
        "decimal_separator": ",",
        "thousands_separator": ".",
        "date_format": "%m/%d/%Y",
        "null_date_sentinels": ["00/00/0000"],
    },
}

TABLE_META: dict[str, dict[str, Any]] = {
    "MARA": {
        "table": "MARA",
        "description": "General Material Data",
        "source_system": "SAP S/4HANA (Cloud Appliance Library)",
        "primary_key": ["MATNR"],
        "header_anchor": "MATNR",
        "file_pattern": "{table}_EX_DATA.xlsx",  # v0.3
        # v0.3: uniqueness lives here so a rebuild does not silently drop it.
        # Blocking keys must agree EXACTLY before two records are compared;
        # MTART and MEINS mean a bolt is never proposed as a duplicate of a
        # coil, nor an each-priced item of a kilo-priced one. The bands and
        # weights are stated defaults, not calibrated numbers.
        "uniqueness": {  # v0.3
            "scope": None,
            "blocking_keys": ["MTART", "MEINS"],
            "compare_fields": [{"field": "MAKT.MAKTX", "weight": 1.0}],
            "methods": {
                "fuzzy": {"metric": "jaro_winkler", "weight": 0.5},
                "semantic": {"model": "all-MiniLM-L6-v2", "weight": 0.5},
            },
            "bands": {"duplicate": 0.92, "review_low": 0.8},
        },
    },
    "MARC": {
        "table": "MARC",
        "description": "Plant Data for Material",
        "source_system": "SAP S/4HANA (Cloud Appliance Library)",
        "primary_key": ["MATNR", "WERKS"],
        "header_anchor": "MATNR",
        "file_pattern": "{table}_EX_DATA.xlsx",  # v0.3
    },
    "MAKT": {
        "table": "MAKT",
        "description": "Material Descriptions",
        "source_system": "SAP S/4HANA (Cloud Appliance Library)",
        "primary_key": ["MATNR", "SPRAS"],
        "header_anchor": "MATNR",
        "file_pattern": "{table}_EX_DATA.xlsx",  # v0.3
    },
}

# Curated overlay: field -> (description, role, semantic_type, mandatory).
# semantic_type drives downstream behaviour and overrides the profiler hint.
# Types: key, client, lang, code, text, quantity, integer, date, flag.
OVERLAY: dict[str, dict[str, tuple]] = {
    "MARA": {
        "MANDT":  ("Client", "client", "client", True),
        "MATNR":  ("Material Number", "key", "key", True),
        "ERSDA":  ("Created On", "temporal", "date", False),
        "LAEDA":  ("Last Changed On", "temporal", "date", False),
        "ERNAM":  ("Created By", "temporal", "text", False),
        "AENAM":  ("Last Changed By", "temporal", "text", False),
        "LVORM":  ("Deletion Flag (client level)", "flag", "flag", False),
        "MTART":  ("Material Type", "attribute", "code", True),
        "MBRSH":  ("Industry Sector", "attribute", "code", True),
        "MATKL":  ("Material Group", "attribute", "code", False),
        "BISMT":  ("Old Material Number", "attribute", "text", False),
        "MEINS":  ("Base Unit of Measure", "attribute", "code", True),
        "BSTME":  ("Order Unit", "attribute", "code", False),
        "MAGRV":  ("Material Group: Packaging Materials", "attribute", "code", False),
        "RAUBE":  ("Storage Conditions", "attribute", "code", False),
        "TRAGR":  ("Transportation Group", "attribute", "code", False),
        "SPART":  ("Division", "attribute", "code", False),
        "BRGEW":  ("Gross Weight", "attribute", "quantity", False),
        "NTGEW":  ("Net Weight", "attribute", "quantity", False),
        "GEWEI":  ("Weight Unit", "attribute", "code", False),
        "VOLUM":  ("Volume", "attribute", "quantity", False),
        "VOLEH":  ("Volume Unit", "attribute", "code", False),
        "WRKST":  ("Basic Material", "attribute", "text", False),
    },
    "MARC": {
        "MANDT":  ("Client", "client", "client", True),
        "MATNR":  ("Material Number", "key", "key", True),
        "WERKS":  ("Plant", "key", "code", True),
        "PSTAT":  ("Maintenance Status", "attribute", "text", False),
        "LVORM":  ("Deletion Flag (plant level)", "flag", "flag", False),
        "MMSTA":  ("Plant-Specific Material Status", "attribute", "code", False),
        "MMSTD":  ("Date From Which Plant Status Valid", "temporal", "date", False),
        "MAABC":  ("ABC Indicator", "attribute", "code", False),
        "EKGRP":  ("Purchasing Group", "attribute", "code", False),
        "DISMM":  ("MRP Type", "attribute", "code", False),
        "DISPO":  ("MRP Controller", "attribute", "code", False),
        "DISLS":  ("Lot Sizing Procedure", "attribute", "code", False),
        "BESKZ":  ("Procurement Type", "attribute", "code", False),
        "SOBSL":  ("Special Procurement Type", "attribute", "code", False),
        "MINBE":  ("Reorder Point", "attribute", "quantity", False),
        "EISBE":  ("Safety Stock", "attribute", "quantity", False),
        "BSTMI":  ("Minimum Lot Size", "attribute", "quantity", False),
        "BSTMA":  ("Maximum Lot Size", "attribute", "quantity", False),
        "BSTFE":  ("Fixed Lot Size", "attribute", "quantity", False),
        "MABST":  ("Maximum Stock Level", "attribute", "quantity", False),
        "MTVFP":  ("Availability Check Group", "attribute", "code", False),
        "PERKZ":  ("Period Indicator", "attribute", "code", False),
        "PRCTR":  ("Profit Centre", "attribute", "code", False),
        "LADGR":  ("Loading Group", "attribute", "code", False),
        "STAWN":  ("Commodity Code / Import Code", "attribute", "code", False),
        "PLIFZ":  ("Planned Delivery Time (days)", "attribute", "integer", False),
        "WEBAZ":  ("Goods Receipt Processing Time (days)", "attribute", "integer", False),
        "FHORI":  ("Scheduling Margin Key", "attribute", "code", False),
        "XCHPF":  ("Batch Management Requirement Indicator", "flag", "flag", False),
    },
    "MAKT": {
        "MANDT":  ("Client", "client", "client", True),
        "MATNR":  ("Material Number", "key", "key", True),
        "SPRAS":  ("Language Key", "key", "lang", True),
        "MAKTX":  ("Material Description (short)", "description", "text", True),
        "MAKTG":  ("Material Description in Upper Case", "description", "text", False),
    },
}

# Semantic types that legitimately carry an enumerated domain.
DOMAIN_TYPES: frozenset[str] = frozenset({"code", "lang", "flag"})


def _load_profile(profiles_dir: Path, table: str) -> dict[str, Any]:
    """Read one profile JSON produced by the profiler."""
    path: Path = profiles_dir / f"{table}_profile.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _build_field_spec(
    field_name: str,
    overlay_entry: tuple,
    field_profile: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Merge one overlay entry with its profiled facts into a spec dict."""
    description: str = overlay_entry[0]
    role: str = overlay_entry[1]
    semantic_type: str = overlay_entry[2]
    mandatory: bool = overlay_entry[3]
    spec: dict[str, Any] = {}
    domain: Optional[list[str]] = None
    populated_pct: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None

    spec["description"] = description
    spec["role"] = role
    spec["type"] = semantic_type
    spec["mandatory"] = mandatory

    if semantic_type == "quantity":
        spec["decimal"] = True

    if field_profile is not None:
        populated_pct = field_profile.get("populated_pct")
        min_length = field_profile.get("min_length")
        max_length = field_profile.get("max_length")
        if semantic_type in DOMAIN_TYPES:
            domain = field_profile.get("inferred_domain")

    if domain:
        spec["domain"] = domain

    if min_length is not None and max_length is not None:
        spec["length"] = {"min": min_length, "max": max_length}

    if populated_pct is not None:
        spec["observed_population_pct"] = populated_pct

    return spec


def build_table_schema(profiles_dir: Path, table: str) -> dict[str, Any]:
    """Assemble the full schema dictionary for one table."""
    profile: dict[str, Any] = _load_profile(profiles_dir, table)
    profile_fields: dict[str, Any] = profile.get("fields", {})
    overlay_fields: dict[str, tuple] = OVERLAY[table]
    schema: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    field_name: str = ""

    schema.update(TABLE_META[table])
    schema["formatting"] = TABLE_FORMATTING[table]

    for field_name, overlay_entry in overlay_fields.items():
        field_profile = profile_fields.get(field_name)
        fields[field_name] = _build_field_spec(field_name, overlay_entry, field_profile)

    schema["fields"] = fields
    return schema


def write_schema(schema: dict[str, Any], out_dir: Path, table: str) -> Path:
    """Serialise a schema dictionary to YAML, preserving key order."""
    target: Path = out_dir / f"{table.lower()}.yaml"
    with target.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(schema, handle, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return target


def main() -> None:
    """Entry point for module execution."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Scaffold table schema YAMLs from profiler output and the SAP overlay."
    )
    parser.add_argument("--profiles", required=True, help="directory holding *_profile.json")
    parser.add_argument("--out", required=True, help="directory to write schema YAMLs into")
    args: argparse.Namespace = parser.parse_args()

    profiles_dir: Path = Path(args.profiles)
    out_dir: Path = Path(args.out)
    table: str = ""

    out_dir.mkdir(parents=True, exist_ok=True)
    for table in OVERLAY.keys():
        schema: dict[str, Any] = build_table_schema(profiles_dir, table)
        target: Path = write_schema(schema, out_dir, table)
        print(f"wrote {target} ({len(schema['fields'])} fields)")


if __name__ == "__main__":
    main()
