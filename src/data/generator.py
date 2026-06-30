# v0.1 | 27-Jun-2026 | Initial synthetic clean-baseline generator

"""Synthetic generator for clean baseline material master data.

This is the clean-data factory. It reads the schema (structure, types,
formatting, mandatory flags) and the profiler output (per-field value
frequencies and population rates) and produces MARA, MARC and MAKT records that
mirror the real system without any quality defects. The defect injector is a
separate pass that introduces labelled defects on top of this baseline.

The baseline is deliberately clean in three specific senses, so that any defect
the injector later adds is the only one present and the ground truth stays
unambiguous:

- every mandatory field is populated;
- every categorical value is drawn from the field's permitted domain;
- the composite key is unique and referential integrity holds (every MARC and
  MAKT row points at a material that exists in MARA).

Optional fields are populated at their observed real-world rate, so natural
sparsity is preserved and is not mistaken for a defect.

Cross-field consistency constraints (for example MRP-type-implies-max-stock) are
not yet enforced here; that hook is added when the rules layer lands, at which
point the generator will satisfy those constraints in the baseline too.

Run as a module:

    python -m src.data.generator --schema config/schema --profiles data/profile \\
        --tables MARA,MARC,MAKT --materials 5000 --seed 42 --out data/synthetic/baseline
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.data.schema import TableSchema, load_schemas


GENERATOR_VERSION: str = "0.1"
CLIENT_VALUE: str = "100"
MATNR_WIDTH: int = 18
DEFAULT_MATNR_START: int = 100000

# Default plants-per-material weights, calibrated from the real MARC extract
# (81.9% one plant, 17.4% two, with a short tail). Overridable at call time.
DEFAULT_PLANTS_PER_MATERIAL: dict[int, float] = {1: 0.819, 2: 0.174, 3: 0.005, 6: 0.002}

# Bounds for synthetic creation dates, used for date fields such as ERSDA.
DATE_START: datetime = datetime(2015, 1, 1)
DATE_END: datetime = datetime(2025, 12, 31)

# Small, domain-flavoured vocabulary for plausible material descriptions. This
# keeps MAKTX readable and SAP-like without pulling in a heavy dependency.
DESC_NOUNS: list[str] = [
    "Pump", "Valve", "Motor", "Bearing", "Gasket", "Flange", "Bolt", "Sensor",
    "Filter", "Coupling", "Gear", "Seal", "Bracket", "Housing", "Shaft",
    "Impeller", "Cylinder", "Actuator", "Compressor", "Turbine",
]
DESC_QUALIFIERS: list[str] = [
    "Precision", "Heavy Duty", "Stainless", "High Speed", "Industrial",
    "Standard", "Compact", "Reinforced", "Cast Steel", "Modular",
]


class Calibration:
    """Per-field empirical calibration drawn from a table profile.

    Holds, for each field, the population rate and a frequency-weighted list of
    observed values, so the generator can sample realistically rather than
    uniformly.
    """

    def __init__(self, profile: dict[str, Any]) -> None:
        self.populated_pct: dict[str, float] = {}
        self.values: dict[str, list[str]] = {}
        self.weights: dict[str, list[float]] = {}
        field_name: str = ""
        field_profile: dict[str, Any] = {}
        top_values: list[dict[str, Any]] = []
        counts: list[float] = []
        total: float = 0.0

        for field_name, field_profile in profile.get("fields", {}).items():
            self.populated_pct[field_name] = float(field_profile.get("populated_pct", 0.0))
            top_values = field_profile.get("top_values", [])
            if top_values:
                counts = [float(v["count"]) for v in top_values]
                total = sum(counts)
                if total > 0:
                    self.values[field_name] = [str(v["value"]) for v in top_values]
                    self.weights[field_name] = [c / total for c in counts]

    def has_values(self, field_name: str) -> bool:
        """Whether weighted observed values exist for a field."""
        return field_name in self.values

    def population_rate(self, field_name: str) -> float:
        """Observed population rate (0..1) for a field, defaulting to 1.0."""
        return self.populated_pct.get(field_name, 100.0) / 100.0


def _load_profile(profiles_dir: Path, table: str) -> dict[str, Any]:
    """Read one profile JSON."""
    return json.loads((profiles_dir / f"{table}_profile.json").read_text(encoding="utf-8"))


def _make_matnr(index: int) -> str:
    """Build a zero-padded 18-character material number."""
    return str(index).zfill(MATNR_WIDTH)


def _make_description(rng: np.random.Generator) -> str:
    """Compose a plausible material description."""
    noun: str = str(rng.choice(DESC_NOUNS))
    qualifier: str = str(rng.choice(DESC_QUALIFIERS))
    size: int = int(rng.integers(10, 999))
    return f"{noun} {qualifier} {size}"


def _sample_categorical(
    rng: np.random.Generator,
    field_name: str,
    schema: TableSchema,
    calibration: Calibration,
) -> Optional[str]:
    """Pick a value for a coded field, weighted by observed frequency.

    Preference order: observed weighted values, then the schema domain
    (uniform), then None when neither is available.
    """
    domain: Optional[list[str]] = None
    chosen: Optional[str] = None

    if calibration.has_values(field_name):
        chosen = str(rng.choice(calibration.values[field_name], p=calibration.weights[field_name]))
    else:
        domain = schema.domain(field_name)
        if domain:
            chosen = str(rng.choice(domain))
    return chosen


def _random_date(rng: np.random.Generator) -> datetime:
    """Pick a random date within the configured window."""
    span_days: int = (DATE_END - DATE_START).days
    offset: int = int(rng.integers(0, span_days + 1))
    return DATE_START + timedelta(days=offset)


def _generate_field_value(
    rng: np.random.Generator,
    field_name: str,
    schema: TableSchema,
    calibration: Calibration,
) -> Optional[str]:
    """Generate a single formatted value for one field of one record.

    Mandatory fields are always populated; optional fields are populated at
    their observed rate. The returned value is already in the SAP string
    representation (comma-decimal quantities, MM/DD/YYYY dates) so synthetic and
    real data share one format.
    """
    spec = schema.field(field_name)
    populate: bool = True
    value: Optional[str] = None
    number: float = 0.0

    if spec is None:
        return None

    # Decide population for optional fields using the observed rate.
    if not spec.mandatory:
        populate = bool(rng.random() < calibration.population_rate(field_name))
    if not populate:
        return None

    if spec.type == "client":
        value = CLIENT_VALUE
    elif spec.type == "lang":
        value = schema.domain(field_name)[0] if schema.domain(field_name) else "E"
    elif spec.type in ("code", "flag"):
        value = _sample_categorical(rng, field_name, schema, calibration)
    elif spec.type == "quantity":
        if calibration.has_values(field_name):
            value = _sample_categorical(rng, field_name, schema, calibration)
        else:
            number = float(rng.integers(0, 1000))
            value = schema.format_quantity(number)
    elif spec.type == "integer":
        if calibration.has_values(field_name):
            value = _sample_categorical(rng, field_name, schema, calibration)
        else:
            value = str(int(rng.integers(0, 60)))
    elif spec.type == "date":
        value = schema.format_date(_random_date(rng))
    elif spec.type == "text":
        value = _make_description(rng)
    else:
        value = None

    return value


def _generate_row(
    rng: np.random.Generator,
    schema: TableSchema,
    calibration: Calibration,
    overrides: dict[str, str],
) -> dict[str, Optional[str]]:
    """Generate one record for a table, applying key overrides last."""
    row: dict[str, Optional[str]] = {}
    field_name: str = ""

    for field_name in schema.fields.keys():
        row[field_name] = _generate_field_value(rng, field_name, schema, calibration)
    row.update(overrides)
    return row


def _choose_plant_count(rng: np.random.Generator, weights: dict[int, float]) -> int:
    """Sample the number of plants a material is extended to."""
    counts: list[int] = list(weights.keys())
    probs: list[float] = list(weights.values())
    total: float = sum(probs)
    normalised: list[float] = [p / total for p in probs]
    return int(rng.choice(counts, p=normalised))


def generate_dataset(
    schema_dir: str,
    profiles_dir: str,
    tables: list[str],
    n_materials: int,
    seed: int,
    out_dir: Optional[str] = None,
    plants_per_material: Optional[dict[int, float]] = None,
    matnr_start: int = DEFAULT_MATNR_START,
) -> dict[str, pd.DataFrame]:
    """Generate clean baseline MARA, MARC and MAKT and optionally persist them."""
    schemas: dict[str, TableSchema] = load_schemas(schema_dir, tables)
    profiles_path: Path = Path(profiles_dir)
    calibrations: dict[str, Calibration] = {}
    rng: np.random.Generator = np.random.default_rng(seed)
    plant_weights: dict[int, float] = plants_per_material or DEFAULT_PLANTS_PER_MATERIAL
    frames: dict[str, pd.DataFrame] = {}
    matnrs: list[str] = []
    index: int = 0
    table: str = ""

    for table in tables:
        calibrations[table] = Calibration(_load_profile(profiles_path, table))

    matnrs = [_make_matnr(matnr_start + index) for index in range(n_materials)]

    # MARA: one row per material.
    if "MARA" in schemas:
        mara_rows: list[dict[str, Optional[str]]] = []
        matnr: str = ""
        for matnr in matnrs:
            mara_rows.append(_generate_row(rng, schemas["MARA"], calibrations["MARA"], {"MATNR": matnr}))
        frames["MARA"] = pd.DataFrame.from_records(mara_rows, columns=list(schemas["MARA"].fields.keys()))

    # MAKT: one row per material in the single available language.
    if "MAKT" in schemas:
        makt_rows: list[dict[str, Optional[str]]] = []
        language: str = schemas["MAKT"].domain("SPRAS")[0] if schemas["MAKT"].domain("SPRAS") else "E"
        description: str = ""
        for matnr in matnrs:
            description = _make_description(rng)
            makt_rows.append(
                _generate_row(
                    rng,
                    schemas["MAKT"],
                    calibrations["MAKT"],
                    {"MATNR": matnr, "SPRAS": language, "MAKTX": description, "MAKTG": description.upper()},
                )
            )
        frames["MAKT"] = pd.DataFrame.from_records(makt_rows, columns=list(schemas["MAKT"].fields.keys()))

    # MARC: one row per material per extended plant.
    if "MARC" in schemas:
        marc_rows: list[dict[str, Optional[str]]] = []
        plant_domain: list[str] = schemas["MARC"].domain("WERKS") or []
        plant_count: int = 0
        chosen_plants: np.ndarray = None
        plant: str = ""
        for matnr in matnrs:
            plant_count = _choose_plant_count(rng, plant_weights)
            plant_count = min(plant_count, len(plant_domain)) if plant_domain else plant_count
            chosen_plants = rng.choice(plant_domain, size=plant_count, replace=False)
            for plant in chosen_plants:
                marc_rows.append(
                    _generate_row(
                        rng,
                        schemas["MARC"],
                        calibrations["MARC"],
                        {"MATNR": matnr, "WERKS": str(plant)},
                    )
                )
        frames["MARC"] = pd.DataFrame.from_records(marc_rows, columns=list(schemas["MARC"].fields.keys()))

    if out_dir is not None:
        _persist(frames, Path(out_dir), seed, n_materials)

    return frames


def _persist(frames: dict[str, pd.DataFrame], out_path: Path, seed: int, n_materials: int) -> None:
    """Write the generated tables to parquet and record a run manifest."""
    out_path.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {}
    row_counts: dict[str, int] = {}
    table: str = ""
    frame: pd.DataFrame = None

    for table, frame in frames.items():
        frame.to_parquet(out_path / f"{table}.parquet", index=False)
        row_counts[table] = int(frame.shape[0])

    manifest = {
        "generator_version": GENERATOR_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seed": seed,
        "n_materials": n_materials,
        "row_counts": row_counts,
    }
    with (out_path / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    print(f"wrote {len(frames)} tables to {out_path}")
    for table in row_counts:
        print(f"  {table}: {row_counts[table]} rows")


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the command line interface."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Generate clean baseline SAP material master data."
    )
    parser.add_argument("--schema", required=True, help="schema YAML directory")
    parser.add_argument("--profiles", required=True, help="profiler output directory")
    parser.add_argument("--tables", default="MARA,MARC,MAKT", help="comma separated tables")
    parser.add_argument("--materials", type=int, default=5000, help="number of materials")
    parser.add_argument("--seed", type=int, default=42, help="random seed for reproducibility")
    parser.add_argument("--out", default=None, help="output directory for parquet files")
    parser.add_argument("--matnr-start", type=int, default=DEFAULT_MATNR_START)
    return parser


def main() -> None:
    """Entry point for module execution."""
    parser: argparse.ArgumentParser = _build_arg_parser()
    args: argparse.Namespace = parser.parse_args()
    table_list: list[str] = [t.strip() for t in args.tables.split(",") if t.strip()]

    generate_dataset(
        schema_dir=args.schema,
        profiles_dir=args.profiles,
        tables=table_list,
        n_materials=args.materials,
        seed=args.seed,
        out_dir=args.out,
        matnr_start=args.matnr_start,
    )


if __name__ == "__main__":
    main()
