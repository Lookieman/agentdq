# v0.1 | 27-Jun-2026 | Initial schema loader and parsing helpers
# v0.2 | 27-Jun-2026 | Add inverse formatters (format_quantity, format_date) for the generator
# v0.3 | 13-Jul-2026 | Add the remaining onboarding fields (file_pattern,
#                      uniqueness) so ONE schema YAML is all a steward creates
#                      to onboard an object. primary_key and header_anchor were
#                      already here; the profiler's hardcoded dicts now defer to
#                      the schema, as its own comments always intended.

"""Runtime loader for the table schema YAMLs.

The schema is the shared contract that the generator, the defect injector and
every agent read from. Beyond field metadata it carries the SAP-specific
parsing rules the extracts demand:

- Quantities use a comma decimal separator and a dot thousands separator, so
  '1.000,000' is one thousand and '157,000' is one hundred and fifty-seven.
- Dates are MM/DD/YYYY with an empty-date sentinel of '00/00/0000', which must
  be read as no date rather than a real one.
- The business key is composite; record_key composes a stable identifier from
  the key fields so findings can point at the exact offending row.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml
from pydantic import BaseModel, Field


class TableFormatting(BaseModel):
    """Locale and sentinel rules for reading a table's values."""

    decimal_separator: str = ","
    thousands_separator: str = "."
    date_format: str = "%m/%d/%Y"
    null_date_sentinels: list[str] = Field(default_factory=lambda: ["00/00/0000"])


class FieldLength(BaseModel):
    """Observed value-length range for a field."""

    min: int
    max: int


class FieldSpec(BaseModel):
    """Specification of a single field within a table."""

    name: str
    description: str = ""
    role: str = "attribute"
    type: str = "text"
    mandatory: bool = False
    decimal: bool = False
    domain: Optional[list[str]] = None
    length: Optional[FieldLength] = None
    observed_population_pct: Optional[float] = None


class UniquenessConfig(BaseModel):  # v0.3
    """Per-object configuration for the Uniqueness agent (Package 4).

    The blocking key partitions the search space (only records sharing it are
    compared); compare_fields are the fields whose similarity indicates a
    duplicate. Kept here so one schema YAML is all a steward writes to onboard
    an object. MARA blocks on MTART and compares MAKT.MAKTX; EQUI would block on
    EQART and compare EQKT.EQKTX - the same mechanism, different parameters.
    """

    blocking_key: Optional[str] = None
    compare_fields: list[str] = Field(default_factory=list)


class TableSchema(BaseModel):
    """A whole table schema with parsing helpers bound to its formatting.

    Beyond field metadata this is the single onboarding contract for a table:
    primary_key and header_anchor (long-standing), plus file_pattern and
    uniqueness (v0.3). A steward onboarding EQUI writes ONE file.

    Note on vocabularies: FieldSpec.role here is STRUCTURAL (key, attribute,
    flag, temporal, client) and drives parsing and generation. It is a different
    vocabulary from the rule bank's SEMANTIC field_role (unit_of_measure,
    material_type, ...) in config/rule_bank/field_roles.yaml, which drives
    template retrieval. They share a word, not a meaning; do not conflate them.
    """

    table: str
    description: str = ""
    source_system: str = ""
    primary_key: list[str]
    header_anchor: str = "MATNR"
    file_pattern: str = "{table}_EX_DATA.xlsx"  # v0.3
    uniqueness: UniquenessConfig = Field(default_factory=UniquenessConfig)  # v0.3
    formatting: TableFormatting = Field(default_factory=TableFormatting)
    fields: dict[str, FieldSpec] = Field(default_factory=dict)

    def resolve_file(self, base_dir: str) -> Path:  # v0.3
        """The extract path this schema expects under a data directory."""
        return Path(base_dir) / self.file_pattern.format(table=self.table)

    def field(self, name: str) -> Optional[FieldSpec]:
        """Return the spec for one field, or None if it is not in the schema."""
        return self.fields.get(name)

    def mandatory_fields(self) -> list[str]:
        """Return the names of all baseline-mandatory fields."""
        result: list[str] = []
        spec: FieldSpec = None
        for spec in self.fields.values():
            if spec.mandatory:
                result.append(spec.name)
        return result

    def domain(self, name: str) -> Optional[list[str]]:
        """Return the permitted value list for a field, if one is defined."""
        spec: Optional[FieldSpec] = self.fields.get(name)
        return spec.domain if spec is not None else None

    def quantity_fields(self) -> list[str]:
        """Return the names of all decimal quantity fields."""
        result: list[str] = []
        spec: FieldSpec = None
        for spec in self.fields.values():
            if spec.type == "quantity":
                result.append(spec.name)
        return result

    def date_fields(self) -> list[str]:
        """Return the names of all date fields."""
        result: list[str] = []
        spec: FieldSpec = None
        for spec in self.fields.values():
            if spec.type == "date":
                result.append(spec.name)
        return result

    def is_null(self, field_name: str, value: Any) -> bool:
        """Decide whether a raw value counts as unpopulated for this field.

        Empty strings and None are always unpopulated. For date fields the SAP
        empty-date sentinel counts as unpopulated too.
        """
        spec: Optional[FieldSpec] = self.fields.get(field_name)
        text: str = ""

        if value is None:
            return True
        text = str(value).strip()
        if text == "":
            return True
        if spec is not None and spec.type == "date":
            if text in self.formatting.null_date_sentinels:
                return True
        return False

    def parse_quantity(self, value: Any) -> Optional[float]:
        """Parse a comma-decimal, dot-thousands quantity into a float.

        Returns None for empty values. '1.000,000' becomes 1000.0 and
        '157,000' becomes 157.0.
        """
        result: Optional[float] = None
        text: str = ""
        thousands: str = self.formatting.thousands_separator
        decimal: str = self.formatting.decimal_separator

        if value is None:
            return None
        text = str(value).strip()
        if text == "":
            return None
        if thousands:
            text = text.replace(thousands, "")
        if decimal:
            text = text.replace(decimal, ".")
        try:
            result = float(text)
        except ValueError:
            result = None
        return result

    def parse_date(self, value: Any) -> Optional[datetime]:
        """Parse a date string, treating the SAP sentinel as no date."""
        text: str = ""

        if value is None:
            return None
        text = str(value).strip()
        if text == "" or text in self.formatting.null_date_sentinels:
            return None
        try:
            return datetime.strptime(text, self.formatting.date_format)
        except ValueError:
            return None

    def format_quantity(self, value: Optional[float], decimals: int = 3) -> str:  # v0.2
        """Render a float as a SAP comma-decimal, dot-thousands string.

        The inverse of parse_quantity: 1000.0 becomes '1.000,000' and 157.0
        becomes '157,000'. None renders as an empty string.
        """
        thousands: str = self.formatting.thousands_separator  # v0.2
        decimal: str = self.formatting.decimal_separator  # v0.2
        rendered: str = ""  # v0.2
        placeholder: str = "\x00"  # v0.2

        if value is None:  # v0.2
            return ""  # v0.2
        # Build US-style grouping first (comma thousands, dot decimal), then swap
        # the separators into the SAP convention using a placeholder to avoid
        # clobbering one separator with the other.
        rendered = f"{value:,.{decimals}f}"  # v0.2
        rendered = rendered.replace(",", placeholder)  # v0.2
        rendered = rendered.replace(".", decimal)  # v0.2
        rendered = rendered.replace(placeholder, thousands)  # v0.2
        return rendered  # v0.2

    def format_date(self, value: Optional[datetime]) -> str:  # v0.2
        """Render a datetime as MM/DD/YYYY, or the null sentinel when absent."""
        sentinel: str = ""  # v0.2

        if value is None:  # v0.2
            sentinel = self.formatting.null_date_sentinels[0] if self.formatting.null_date_sentinels else ""  # v0.2
            return sentinel  # v0.2
        return value.strftime(self.formatting.date_format)  # v0.2

    def record_key(self, row: Mapping[str, Any]) -> str:
        """Compose a stable identifier from the composite key fields.

        For MARC this yields 'MATNR=...|WERKS=...' so a finding can name the
        exact row rather than an ambiguous material number.
        """
        parts: list[str] = []
        key_field: str = ""
        for key_field in self.primary_key:
            parts.append(f"{key_field}={row.get(key_field)}")
        return "|".join(parts)


def load_table_schema(path: str) -> TableSchema:
    """Load one schema YAML, injecting field names from their keys."""
    file_path: Path = Path(path)
    raw: dict[str, Any] = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    field_blocks: dict[str, Any] = raw.get("fields", {})
    field_name: str = ""
    block: dict[str, Any] = {}

    if "table" not in raw or not raw["table"]:
        raw["table"] = file_path.stem.upper()

    for field_name, block in field_blocks.items():
        block["name"] = field_name

    return TableSchema.model_validate(raw)


def load_schemas(schema_dir: str, tables: list[str]) -> dict[str, TableSchema]:
    """Load several schemas keyed by upper-case table name."""
    base: Path = Path(schema_dir)
    schemas: dict[str, TableSchema] = {}
    table: str = ""
    for table in tables:
        schema_path: Path = base / f"{table.lower()}.yaml"
        schemas[table] = load_table_schema(str(schema_path))
    return schemas


def load_all_schemas(schema_dir: str) -> dict[str, TableSchema]:  # v0.3
    """Load every schema YAML in a directory, keyed by table name. Unlike
    load_schemas this does not need the table names up front, so callers (the
    profiler, the onboarding tools) can discover what is registered. A missing
    directory yields an empty dict rather than raising: schemas are the
    preferred source of onboarding config, not yet a mandatory one."""
    base: Path = Path(schema_dir)
    schemas: dict[str, TableSchema] = {}
    schema_path: Path = None
    schema: TableSchema = None

    if not base.exists():
        return schemas
    for schema_path in sorted(base.glob("*.yaml")):
        schema = load_table_schema(str(schema_path))
        schemas[schema.table] = schema
    return schemas
