# ---------------------------------------------------------------------------
# src/rules/reference_store.py
# v1.0 | 05-Jul-2026 | Initial creation. Loads reference tables (values + a
#                      thin "as-of" metadata wrapper) from a manifest, and
#                      answers membership and match-rate queries. Supports
#                      plant-scoped tables (e.g. T024D keyed by plant).
# v1.1 | 05-Jul-2026 | Coerce extract_date to an ISO string on load; PyYAML
#                      parses unquoted dates as datetime.date, which made the
#                      metadata type depend on manifest quoting.
# v1.2 | 05-Jul-2026 | Load reference tables through src/data/extract_loader
#                      (SE16N/SE12 xlsx) instead of CSV, so preamble, spacer
#                      column and leading zeros are handled the same way as the
#                      master data. Indexing split into a pure build_reference_
#                      table() seam; added missing_asof() readiness check.
# v1.3 | 09-Jul-2026 | Add values() accessor so a bank-matched reference rule
#                      can be instantiated with the live domain.
# ---------------------------------------------------------------------------
"""Reference store: the trusted check-table layer.

Reference *tables* (T006, T023, ...) are extracted from the SAP appliance via
the same SE16N/SE12 route as the master data and dropped as xlsx alongside a
manifest that carries their "as-of" metadata (source system, extract date, key
columns, status). They are loaded through src/data/extract_loader.load_sap_table
so the SE16N preamble, the spacer column and (critically) leading zeros are
handled identically to the master data - a plant scope key of '0001' must match
the same '0001' seen in MARC. Small fixed domains (e.g. BESKZ = E/F/X) do NOT
belong here; they are template_fixed and live in the rule bank.

A stale reference list masquerading as truth is itself a data quality defect,
so every table carries when-and-whence metadata, and `status` lets the store
report which tables are still pending extract rather than silently returning
empty membership.

Manifest shape (config/reference/manifest.yaml):

    version: 1
    tables:
      - name: T006
        description: Units of measure
        serves_role: unit_of_measure
        source_system: null            # e.g. S4CAL-100
        extract_date: null             # ISO date; null while pending
        key_columns: [MSEHI]           # business key column(s)
        value_column: MSEHI            # the column checked for membership
        scope_columns: []              # e.g. [WERKS] for a plant-scoped table
        header_anchor: MSEHI           # field used to locate the SE16N header row
        sheet: Data                    # worksheet name in the export
        status: pending_extract        # pending_extract | loaded
        file: T006.xlsx                # xlsx under config/reference/

This module is deterministic; no LLM, no LangGraph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd  # v1.2
import yaml

from src.data.extract_loader import load_sap_table  # v1.2


VALID_STATUSES = ("pending_extract", "loaded")


@dataclass
class ReferenceTableMeta:
    """The 'as-of' wrapper. A reference value set is only trustworthy relative
    to when and whence it was extracted."""

    name: str
    description: Optional[str] = None
    serves_role: Optional[str] = None
    source_system: Optional[str] = None
    extract_date: Optional[str] = None
    key_columns: list[str] = field(default_factory=list)
    value_column: Optional[str] = None
    scope_columns: list[str] = field(default_factory=list)
    header_anchor: Optional[str] = None  # v1.2 field used to locate the header row
    sheet: str = "Data"                  # v1.2 worksheet name in the SE16N export
    status: str = "pending_extract"
    file: Optional[str] = None

    @property
    def is_scoped(self) -> bool:
        return len(self.scope_columns) > 0

    @property
    def resolved_value_column(self) -> Optional[str]:  # v1.2
        """The column checked for membership: value_column, or the first key."""
        if self.value_column:
            return self.value_column
        return self.key_columns[0] if self.key_columns else None

    @property
    def resolved_anchor(self) -> Optional[str]:  # v1.2
        """The header anchor for extract_loader: explicit, else the value column."""
        if self.header_anchor:
            return self.header_anchor
        return self.resolved_value_column


@dataclass
class ReferenceTable:
    """A loaded reference table: its metadata plus the value set.

    For an unscoped table, `values` holds the permitted values (upper-cased,
    stripped). For a scoped table, `scoped_values` maps a scope-key tuple to a
    set of permitted values within that scope."""

    meta: ReferenceTableMeta
    values: set[str] = field(default_factory=set)
    scoped_values: dict[tuple[str, ...], set[str]] = field(default_factory=dict)


def _normalise(value: Any) -> str:
    """Uniform comparison form: string, stripped, upper-cased."""
    text = "" if value is None else str(value)
    return text.strip().upper()


def _coerce_iso_date(value: Any) -> Optional[str]:  # v1.1
    """Return an ISO date string, whatever PyYAML gave us (str, date, or None).
    Unquoted YAML dates deserialise to datetime.date; normalise them here so the
    metadata type is stable regardless of manifest quoting."""
    if value is None:  # v1.1
        return None  # v1.1
    isoformat = getattr(value, "isoformat", None)  # v1.1
    if callable(isoformat):  # v1.1
        return isoformat()  # v1.1
    return str(value)  # v1.1


def _meta_from_dict(data: dict[str, Any]) -> ReferenceTableMeta:
    source = data or {}
    return ReferenceTableMeta(
        name=source["name"],
        description=source.get("description"),
        serves_role=source.get("serves_role"),
        source_system=source.get("source_system"),
        extract_date=_coerce_iso_date(source.get("extract_date")),  # v1.1
        key_columns=source.get("key_columns", []) or [],
        value_column=source.get("value_column"),
        scope_columns=source.get("scope_columns", []) or [],
        header_anchor=source.get("header_anchor"),  # v1.2
        sheet=source.get("sheet", "Data"),          # v1.2
        status=source.get("status", "pending_extract"),
        file=source.get("file"),
    )


def build_reference_table(meta: ReferenceTableMeta, frame: pd.DataFrame) -> ReferenceTable:  # v1.2
    """Index a loaded DataFrame into a ReferenceTable's value set(s).

    Pure and file-format-agnostic: the caller supplies the frame (from
    extract_loader in production, or hand-built in tests). Values are normalised
    for comparison but leading zeros are preserved (strip removes only
    whitespace), so '0001' stays '0001'."""
    table = ReferenceTable(meta=meta)
    value_column: Optional[str] = meta.resolved_value_column
    value_list: list[Any] = []
    scope_lists: list[list[Any]] = []
    position: int = 0
    raw_value: Any = None
    normalised_value: str = ""
    scope_key: tuple[str, ...] = ()
    bucket: set[str] = set()

    if value_column is None or value_column not in frame.columns:
        raise ValueError(
            f"reference table {meta.name}: value column "
            f"{value_column!r} not found in extract (columns: {list(frame.columns)})"
        )

    value_list = frame[value_column].tolist()

    if meta.is_scoped:
        for scope_column in meta.scope_columns:
            if scope_column not in frame.columns:
                raise ValueError(
                    f"reference table {meta.name}: scope column "
                    f"{scope_column!r} not found in extract"
                )
            scope_lists.append(frame[scope_column].tolist())
        for position, raw_value in enumerate(value_list):
            normalised_value = _normalise(raw_value)
            if normalised_value == "":
                continue
            scope_key = tuple(_normalise(scope_lists[j][position]) for j in range(len(meta.scope_columns)))
            bucket = table.scoped_values.setdefault(scope_key, set())
            bucket.add(normalised_value)
    else:
        for raw_value in value_list:
            normalised_value = _normalise(raw_value)
            if normalised_value == "":
                continue
            table.values.add(normalised_value)

    return table


class ReferenceStore:
    """Loads and queries reference tables declared in a manifest."""

    def __init__(self, tables: dict[str, ReferenceTable]):
        self.tables = tables

    # -- construction --------------------------------------------------------

    @classmethod
    def load(cls, manifest_path: str | Path, data_dir: str | Path | None = None) -> "ReferenceStore":
        """Load every table in the manifest. Tables marked pending_extract (or
        with a missing file) are registered with empty values so callers can
        see they exist but are not yet populated."""
        manifest_file = Path(manifest_path)
        base_dir = Path(data_dir) if data_dir else manifest_file.parent
        raw = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
        entries = raw.get("tables", []) if isinstance(raw, dict) else []

        tables: dict[str, ReferenceTable] = {}
        for entry in entries:
            meta = _meta_from_dict(entry)
            table = cls._load_one(meta, base_dir)
            tables[meta.name] = table
        return cls(tables=tables)

    @staticmethod
    def _load_one(meta: ReferenceTableMeta, base_dir: Path) -> ReferenceTable:  # v1.2
        table = ReferenceTable(meta=meta)
        xlsx_path: Path
        anchor: Optional[str]
        frame: pd.DataFrame

        # Nothing to load until it has been extracted.
        if meta.status != "loaded" or not meta.file:
            return table

        xlsx_path = base_dir / meta.file
        if not xlsx_path.exists():
            # Declared loaded but the file is absent: treat as pending, do not crash.
            table.meta.status = "pending_extract"
            return table

        # Load through the SE16N-aware loader so preamble, spacer column and
        # leading zeros are handled exactly as for the master data.
        anchor = meta.resolved_anchor
        frame = load_sap_table(str(xlsx_path), header_anchor=anchor, sheet=meta.sheet)
        return build_reference_table(meta, frame)

    # -- queries -------------------------------------------------------------

    def get_meta(self, table_name: str) -> ReferenceTableMeta:
        return self.tables[table_name].meta

    def is_loaded(self, table_name: str) -> bool:
        table = self.tables.get(table_name)
        if table is None:
            return False
        return table.meta.status == "loaded"

    def is_member(
        self, table_name: str, value: Any, scope: Optional[tuple[str, ...]] = None
    ) -> Optional[bool]:
        """Return True/False for membership, or None when the table is not
        loaded (absent evidence is not the same as a failed check)."""
        table = self.tables.get(table_name)
        if table is None or table.meta.status != "loaded":
            return None
        candidate = _normalise(value)
        if table.meta.is_scoped:
            scope_key = tuple(_normalise(part) for part in (scope or ()))
            bucket = table.scoped_values.get(scope_key, set())
            return candidate in bucket
        return candidate in table.values

    def values(  # v1.3
        self, table_name: str, scope: Optional[tuple[str, ...]] = None
    ) -> Optional[list[str]]:
        """Return the sorted permitted values for a table, or None when it is not
        loaded. Used to instantiate a bank-matched reference rule with the live
        domain. For a scoped table, a scope key must be supplied; without one the
        per-scope domains cannot be flattened, so None is returned."""
        table = self.tables.get(table_name)
        if table is None or table.meta.status != "loaded":
            return None
        if table.meta.is_scoped:
            if scope is None:
                return None
            scope_key = tuple(_normalise(part) for part in scope)
            return sorted(table.scoped_values.get(scope_key, set()))
        return sorted(table.values)

    def match_rate(
        self,
        table_name: str,
        values: Iterable[Any],
        scope: Optional[tuple[str, ...]] = None,
    ) -> Optional[float]:
        """Fraction of the supplied (non-empty) values found in the reference
        table. Returns None when the table is not loaded. Empty input yields
        0.0. This is the reference_match_rate an applicability signal uses."""
        table = self.tables.get(table_name)
        if table is None or table.meta.status != "loaded":
            return None

        total = 0
        hits = 0
        for value in values:
            candidate = _normalise(value)
            if candidate == "":
                continue
            total += 1
            member = self.is_member(table_name, candidate, scope=scope)
            if member:
                hits += 1
        if total == 0:
            return 0.0
        return hits / total

    def pending_tables(self) -> list[str]:
        """Names of tables still awaiting extract - useful for a readiness check."""
        pending: list[str] = []
        for name, table in self.tables.items():
            if table.meta.status != "loaded":
                pending.append(name)
        return sorted(pending)

    def missing_asof(self) -> list[str]:  # v1.2
        """Loaded tables lacking a source_system or extract_date. A loaded list
        with no 'as of when' is a mild data quality defect in its own right, so
        we surface it rather than let it pass as trustworthy truth."""
        undated: list[str] = []
        for name, table in self.tables.items():
            if table.meta.status == "loaded":
                if not table.meta.source_system or not table.meta.extract_date:
                    undated.append(name)
        return sorted(undated)
