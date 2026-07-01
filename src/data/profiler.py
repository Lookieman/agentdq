# v0.1 | 27-Jun-2026 | Initial CAL data profiler
# v0.2 | 27-Jun-2026 | Add composite primary keys and a key-uniqueness check;
#                      switch loader call to header_anchor
# v0.3 | 27-Jun-2026 | Follow rename of data loader to extract_loader

"""Profiler for SAP master data extracts.

Reads the loaded extracts and produces, per field: population rate, distinct
count, the most frequent values, an inferred domain for low-cardinality fields,
value-length range, and a coarse type hint. At table level it now also checks
the composite business key for uniqueness - one row per material per plant in
MARC, per plant and storage location in MARD, and so on. A duplicated composite
key is a genuine Uniqueness defect, so this check doubles as a first live data
quality signal on the real extracts.

The output calibrates the synthetic generator so generated data mirrors the
real system.

Run as a module:

    python -m src.data.profiler --input data/raw --tables MARA,MARC,MAKT \\
        --pattern "{table}_EX_DATA.xlsx" --out data/profile
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

import pandas as pd
from pydantic import BaseModel, Field

from src.data.extract_loader import load_sap_table  # v0.3


# Field used only to locate the header row in the SE16N preamble (see loader).
# It is not the business key; MATNR appears in every material table's header.
TABLE_HEADER_ANCHOR: dict[str, str] = {  # v0.2
    "MARA": "MATNR",
    "MARC": "MATNR",
    "MARD": "MATNR",
    "MAKT": "MATNR",
}

# The composite business key per table, excluding the client field MANDT (which
# is constant within a single-client extract). A duplicated key here is a real
# Uniqueness defect. This will migrate into the schema YAMLs once the schema
# layer is built, which then becomes the single source of truth.
TABLE_PRIMARY_KEY: dict[str, list[str]] = {  # v0.2
    "MARA": ["MATNR"],
    "MARC": ["MATNR", "WERKS"],
    "MARD": ["MATNR", "WERKS", "LGORT"],
    "MAKT": ["MATNR", "SPRAS"],
}

DEFAULT_TOP_N: int = 20
DEFAULT_DOMAIN_THRESHOLD: int = 50
MAX_DUPLICATE_EXAMPLES: int = 5  # v0.2


class ValueCount(BaseModel):
    """A single value and how often it occurs in a field."""

    value: str
    count: int


class FieldProfile(BaseModel):
    """Profile of one field within a table."""

    name: str
    type_hint: str
    populated_count: int
    populated_pct: float
    distinct_count: int
    min_length: int
    max_length: int
    top_values: list[ValueCount] = Field(default_factory=list)
    inferred_domain: Optional[list[str]] = None
    sample_values: list[str] = Field(default_factory=list)


class KeyUniqueness(BaseModel):  # v0.2
    """Result of checking the composite business key for uniqueness."""

    primary_key: list[str]
    key_fields_present: list[str]
    key_fields_missing: list[str]
    distinct_key_count: int
    duplicate_row_count: int
    is_unique: bool
    assessable: bool
    duplicate_examples: list[str] = Field(default_factory=list)


class TableProfile(BaseModel):
    """Profile of a whole table."""

    table: str
    row_count: int
    field_count: int
    key_uniqueness: Optional[KeyUniqueness] = None  # v0.2
    fields: dict[str, FieldProfile] = Field(default_factory=dict)


def _classify_field(
    populated_pct: float,
    distinct_count: int,
    row_count: int,
    populated_values: pd.Series,
    domain_threshold: int,
) -> str:
    """Assign a coarse type hint used by the generator to pick a strategy."""
    type_hint: str = "free_text"
    all_digits: bool = False

    if row_count > 0 and distinct_count == row_count and populated_pct == 100.0:
        type_hint = "key"
    elif distinct_count <= 1:
        type_hint = "constant"
    elif distinct_count <= domain_threshold:
        type_hint = "categorical"
    else:
        all_digits = bool(populated_values.str.fullmatch(r"\d+").all())
        type_hint = "numeric_text" if all_digits else "free_text"

    return type_hint


def check_key_uniqueness(frame: pd.DataFrame, primary_key: list[str]) -> KeyUniqueness:  # v0.2
    """Check whether the composite business key uniquely identifies each row.

    If any key field is absent from the extract the key cannot be fully
    assessed; that is reported rather than silently passing. Duplicate examples
    are formatted as readable 'FIELD=value' combinations for the summary.
    """
    present: list[str] = [k for k in primary_key if k in frame.columns]
    missing: list[str] = [k for k in primary_key if k not in frame.columns]
    assessable: bool = len(missing) == 0 and len(present) > 0
    distinct_key_count: int = 0
    duplicate_row_count: int = 0
    is_unique: bool = False
    examples: list[str] = []
    duplicated_mask: pd.Series = None
    duplicate_keys: pd.DataFrame = None
    row = None
    parts: list[str] = []

    if not present:
        return KeyUniqueness(
            primary_key=primary_key,
            key_fields_present=present,
            key_fields_missing=missing,
            distinct_key_count=0,
            duplicate_row_count=0,
            is_unique=False,
            assessable=False,
            duplicate_examples=[],
        )

    distinct_key_count = int(frame[present].drop_duplicates().shape[0])
    duplicated_mask = frame.duplicated(subset=present, keep=False)
    duplicate_row_count = int(duplicated_mask.sum())
    is_unique = assessable and duplicate_row_count == 0

    if duplicate_row_count > 0:
        duplicate_keys = (
            frame.loc[duplicated_mask, present].drop_duplicates().head(MAX_DUPLICATE_EXAMPLES)
        )
        for _, row in duplicate_keys.iterrows():
            parts = [f"{field}={row[field]}" for field in present]
            examples.append(", ".join(parts))

    return KeyUniqueness(
        primary_key=primary_key,
        key_fields_present=present,
        key_fields_missing=missing,
        distinct_key_count=distinct_key_count,
        duplicate_row_count=duplicate_row_count,
        is_unique=is_unique,
        assessable=assessable,
        duplicate_examples=examples,
    )


def profile_field(
    series: pd.Series,
    row_count: int,
    top_n: int,
    domain_threshold: int,
) -> FieldProfile:
    """Profile a single column."""
    populated: pd.Series = series.dropna()
    populated_count: int = int(populated.shape[0])
    populated_pct: float = round(100.0 * populated_count / row_count, 2) if row_count else 0.0
    distinct_count: int = int(populated.nunique())
    lengths: pd.Series = populated.str.len() if populated_count else pd.Series(dtype="int64")
    min_length: int = int(lengths.min()) if populated_count else 0
    max_length: int = int(lengths.max()) if populated_count else 0

    value_counts: pd.Series = populated.value_counts().head(top_n)
    top_values: list[ValueCount] = [
        ValueCount(value=str(value), count=int(count))
        for value, count in value_counts.items()
    ]

    type_hint: str = _classify_field(
        populated_pct, distinct_count, row_count, populated, domain_threshold
    )

    inferred_domain: Optional[list[str]] = None
    if type_hint in ("categorical", "constant") and distinct_count <= domain_threshold:
        inferred_domain = sorted(populated.unique().tolist())

    sample_values: list[str] = [str(v) for v in populated.head(5).tolist()]

    return FieldProfile(
        name=str(series.name),
        type_hint=type_hint,
        populated_count=populated_count,
        populated_pct=populated_pct,
        distinct_count=distinct_count,
        min_length=min_length,
        max_length=max_length,
        top_values=top_values,
        inferred_domain=inferred_domain,
        sample_values=sample_values,
    )


def profile_table(
    frame: pd.DataFrame,
    table_name: str,
    top_n: int = DEFAULT_TOP_N,
    domain_threshold: int = DEFAULT_DOMAIN_THRESHOLD,
) -> TableProfile:
    """Profile every field in a loaded table and check its composite key."""
    row_count: int = int(frame.shape[0])
    fields: dict[str, FieldProfile] = {}
    column_name: str = ""
    primary_key: list[str] = TABLE_PRIMARY_KEY.get(table_name, [])  # v0.2
    key_uniqueness: Optional[KeyUniqueness] = None  # v0.2

    for column_name in frame.columns:
        fields[column_name] = profile_field(
            frame[column_name], row_count, top_n, domain_threshold
        )

    if primary_key:  # v0.2
        key_uniqueness = check_key_uniqueness(frame, primary_key)  # v0.2

    return TableProfile(
        table=table_name,
        row_count=row_count,
        field_count=len(frame.columns),
        key_uniqueness=key_uniqueness,  # v0.2
        fields=fields,
    )


def _print_key_summary(profile: TableProfile) -> None:  # v0.2
    """Print the composite-key uniqueness verdict for one table."""
    ku: Optional[KeyUniqueness] = profile.key_uniqueness
    key_str: str = ""
    verdict: str = ""

    if ku is None:
        return

    key_str = " + ".join(ku.primary_key)
    if not ku.assessable:
        verdict = f"NOT ASSESSABLE (missing {', '.join(ku.key_fields_missing)})"
    elif ku.is_unique:
        verdict = f"UNIQUE ({ku.distinct_key_count} distinct keys)"
    else:
        verdict = (
            f"DUPLICATES FOUND ({ku.duplicate_row_count} rows share a key; "
            f"{ku.distinct_key_count} distinct keys)"
        )
    print(f"  key [{key_str}]: {verdict}")
    if ku.duplicate_examples:
        for example in ku.duplicate_examples:
            print(f"    duplicate: {example}")


def _print_summary(profile: TableProfile) -> None:
    """Print a compact, Latin-1 safe summary table for one profiled table."""
    header: str = ""
    line: str = ""
    field_profile: FieldProfile = None

    print(f"\n=== {profile.table} | {profile.row_count} rows | {profile.field_count} fields ===")
    _print_key_summary(profile)  # v0.2
    header = f"{'FIELD':<22}{'POP%':>7}{'DISTINCT':>10}  {'TYPE':<13}{'SAMPLE'}"
    print(header)
    print("-" * len(header))
    for field_profile in profile.fields.values():
        sample: str = field_profile.sample_values[0] if field_profile.sample_values else ""
        if len(sample) > 24:
            sample = sample[:21] + "..."
        line = (
            f"{field_profile.name:<22}"
            f"{field_profile.populated_pct:>7}"
            f"{field_profile.distinct_count:>10}  "
            f"{field_profile.type_hint:<13}"
            f"{sample}"
        )
        print(line)


def profile_files(
    input_dir: str,
    tables: list[str],
    pattern: str,
    out_dir: Optional[str],
    top_n: int = DEFAULT_TOP_N,
    domain_threshold: int = DEFAULT_DOMAIN_THRESHOLD,
) -> dict[str, TableProfile]:
    """Load, profile, and optionally persist a set of table extracts."""
    base: Path = Path(input_dir)
    out_path: Optional[Path] = Path(out_dir) if out_dir else None
    profiles: dict[str, TableProfile] = {}
    table: str = ""

    if out_path is not None:
        out_path.mkdir(parents=True, exist_ok=True)

    for table in tables:
        file_path: Path = base / pattern.format(table=table)
        anchor: str = TABLE_HEADER_ANCHOR.get(table, "MATNR")  # v0.2
        frame: pd.DataFrame = load_sap_table(str(file_path), header_anchor=anchor)  # v0.2
        profile: TableProfile = profile_table(frame, table, top_n, domain_threshold)
        profiles[table] = profile
        _print_summary(profile)

        if out_path is not None:
            target: Path = out_path / f"{table}_profile.json"
            with target.open("w", encoding="utf-8") as handle:
                handle.write(profile.model_dump_json(indent=2))
            print(f"  written: {target}")

    return profiles


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the command line interface."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Profile SAP master data extracts to calibrate the generator."
    )
    parser.add_argument("--input", required=True, help="directory holding the extracts")
    parser.add_argument(
        "--tables",
        required=True,
        help="comma separated table names, for example MARA,MARC,MAKT",
    )
    parser.add_argument(
        "--pattern",
        default="{table}_EX_DATA.xlsx",
        help="filename pattern, with {table} as the placeholder",
    )
    parser.add_argument("--out", default=None, help="directory to write profile JSON into")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--domain-threshold", type=int, default=DEFAULT_DOMAIN_THRESHOLD)
    return parser


def main() -> None:
    """Entry point for module execution."""
    parser: argparse.ArgumentParser = _build_arg_parser()
    args: argparse.Namespace = parser.parse_args()
    table_list: list[str] = [t.strip() for t in args.tables.split(",") if t.strip()]

    profile_files(
        input_dir=args.input,
        tables=table_list,
        pattern=args.pattern,
        out_dir=args.out,
        top_n=args.top_n,
        domain_threshold=args.domain_threshold,
    )


if __name__ == "__main__":
    main()
