# v0.1 | 27-Jun-2026 | Initial schema loader and parsing helpers
# v0.2 | 27-Jun-2026 | Add inverse formatters (format_quantity, format_date) for the generator
# v0.3 | 13-Jul-2026 | Add the remaining onboarding fields (file_pattern,
#                      uniqueness) so ONE schema YAML is all a steward creates
#                      to onboard an object. primary_key and header_anchor were
#                      already here; the profiler's hardcoded dicts now defer to
#                      the schema, as its own comments always intended.
# v0.4 | 04-Aug-2026 | Package 4a. UniquenessConfig gains scope, methods and
#                      bands; blocking_key becomes blocking_keys (a list, so
#                      MTART AND MEINS both narrow the search) and
#                      compare_fields carries a per-field weight. Adds
#                      effective_bands() for steward-versus-advisory precedence
#                      and fingerprint() so a run records the settings it used.
#                      The v0.3 singular 'blocking_key' now raises rather than
#                      being silently ignored.

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

import hashlib  # v0.4
import json  # v0.4
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator  # v0.4

from src.contracts import Predicate  # v0.4


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


# The language key used to read a material description for matching. English
# only in Phase 1: a multi-language object needs a decision about which
# language wins, and that decision is not needed to prove the mechanism.
COMPARE_LANGUAGE: str = "E"  # v0.4

# Text comparison metrics the fuzzy rung accepts. Each maps to a rapidfuzz
# scorer in the Uniqueness agent. Named here so a wrong value in a YAML fails
# at load time with a clear message, not deep inside a scoring loop.
ALLOWED_FUZZY_METRICS: tuple[str, ...] = (  # v0.4
    "jaro_winkler",
    "token_sort_ratio",
    "token_set_ratio",
    "ratio",
)

# The highest a band may be pushed to by an advisory. A band of 1.0 would mean
# only a perfect match counts, which silently switches near-duplicate detection
# off, so the shift is capped below it.
MAX_BAND: float = 0.99  # v0.4


class CompareField(BaseModel):  # v0.4
    """One field whose similarity contributes to a duplicate score.

    field may name another table ('MAKT.MAKTX') or this table ('MAKTX').
    weight is that field's share of the record score; weights are relative, so
    0.7 and 0.3 mean the same as 7 and 3.
    """

    field: str
    weight: float = 1.0

    @field_validator("weight")  # v0.4
    @classmethod
    def _weight_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError(
                "compare field weight must be greater than 0; to stop using a "
                "field, remove it from compare_fields"
            )
        return value


class FuzzyMethod(BaseModel):  # v0.4
    """Letter-by-letter text comparison. Catches typing slips and small edits.

    Fast, and it needs no model. It has no idea that 'Bolt' and 'Screw' mean
    nearly the same thing, which is why the semantic rung exists.
    """

    metric: str = "jaro_winkler"
    weight: float = 0.5

    @field_validator("metric")  # v0.4
    @classmethod
    def _metric_must_be_known(cls, value: str) -> str:
        if value not in ALLOWED_FUZZY_METRICS:
            raise ValueError(
                f"unknown fuzzy metric '{value}'; allowed: "
                f"{', '.join(ALLOWED_FUZZY_METRICS)}"
            )
        return value

    @field_validator("weight")  # v0.4
    @classmethod
    def _weight_not_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("method weight cannot be negative; use 0 to switch it off")
        return value


class SemanticMethod(BaseModel):  # v0.4
    """Meaning-based comparison. Turns each description into a list of numbers
    that stands for its meaning, so texts that say the same thing in different
    words score highly.

    Set weight to 0 to switch it off and run the fuzzy rung alone, which is
    what happens on a machine with no embeddings artefact built.
    """

    model: str = "all-MiniLM-L6-v2"
    weight: float = 0.5

    @field_validator("weight")  # v0.4
    @classmethod
    def _weight_not_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("method weight cannot be negative; use 0 to switch it off")
        return value


class UniquenessMethods(BaseModel):  # v0.4
    """The two ways two texts are compared, and how much each one counts."""

    fuzzy: FuzzyMethod = Field(default_factory=FuzzyMethod)
    semantic: SemanticMethod = Field(default_factory=SemanticMethod)

    @model_validator(mode="after")  # v0.4
    def _at_least_one_method(self) -> UniquenessMethods:
        if self.fuzzy.weight + self.semantic.weight <= 0:
            raise ValueError(
                "fuzzy and semantic weights are both 0, so no comparison would "
                "run; give at least one of them a weight"
            )
        return self


class UniquenessBands(BaseModel):  # v0.4
    """The two score lines that split a pair into three outcomes.

        score >= duplicate    -> duplicate, no model needed
        review_low <= score   -> uncertain, ask the language model
                   < duplicate
        score < review_low    -> not a duplicate, no model needed

    These numbers are presentation dials. They are stated, not calibrated;
    calibration lands in Package 5.
    """

    duplicate: float = 0.92
    review_low: float = 0.80

    @model_validator(mode="after")  # v0.4
    def _bands_must_be_ordered(self) -> UniquenessBands:
        if not 0.0 < self.review_low < self.duplicate <= 1.0:
            raise ValueError(
                f"bands must satisfy 0 < review_low ({self.review_low}) < "
                f"duplicate ({self.duplicate}) <= 1"
            )
        return self


class UniquenessConfig(BaseModel):  # v0.4
    """Per-object configuration for the Uniqueness agent (Package 4).

    Blocking keys partition the search space: only records that agree EXACTLY
    on every blocking key are ever compared. MARA blocks on MTART and MEINS, so
    two materials of a different type, or measured in different units, are
    never proposed as duplicates. This is how SAP's own duplicate check works,
    and it is also what keeps an all-pairs comparison affordable.

    compare_fields are then scored for similarity within a block. MARA compares
    MAKT.MAKTX; EQUI would block on EQART and compare EQKT.EQKTX - the same
    mechanism, different parameters.

    Known limitation: blocking is exact. A material with a wrong MEINS sits in
    the wrong block, so its true duplicate is never found. That is honest
    behaviour, and it is a reason the Validity agent and this agent need each
    other.
    """

    scope: Optional[Predicate] = None  # v0.4
    blocking_keys: list[str] = Field(default_factory=list)  # v0.4
    compare_fields: list[CompareField] = Field(default_factory=list)
    methods: UniquenessMethods = Field(default_factory=UniquenessMethods)  # v0.4
    bands: UniquenessBands = Field(default_factory=UniquenessBands)  # v0.4
    # v0.5: the ceiling on ONE block. Comparison inside a block is all-pairs,
    # so cost is n(n-1)/2 and a big block is the only way this stage becomes
    # slow. A block above the ceiling is HELD BACK rather than compared, and
    # its records are excluded from the uniqueness denominator, so a block
    # nobody looked at never counts as a clean one.
    #
    # 20,000,000 pairs is about 20 to 25 seconds of scoring on the fuzzy rung
    # alone and roughly double that with the semantic rung. Alternatives: 5M
    # (the previous value, which a 3,180-record block already exceeds and
    # which fires on datasets that take seconds to score), or 50M (about two
    # minutes, which never fires on anything in this repository). 20M was
    # chosen because it holds the wait on a screen under a minute and still
    # covers every block in the synthetic and real datasets here.
    max_block_pairs: int = 20_000_000  # v0.5

    @model_validator(mode="before")  # v0.4
    @classmethod
    def _reject_legacy_singular_key(cls, data: Any) -> Any:
        """Fail loudly on the v0.3 spelling.

        Pydantic ignores unknown keys by default, so a YAML still saying
        'blocking_key: MTART' would load with NO blocking keys at all and
        compare every record against every other one. A silent change of that
        size deserves an error, not a shrug.
        """
        if isinstance(data, dict) and "blocking_key" in data:
            raise ValueError(
                "'blocking_key' was replaced by 'blocking_keys' (a list) in "
                "schema v0.4; write 'blocking_keys: [MTART, MEINS]'"
            )
        return data

    @field_validator("compare_fields", mode="before")  # v0.4
    @classmethod
    def _accept_plain_field_names(cls, value: Any) -> Any:
        """Allow 'MAKT.MAKTX' as shorthand for {field: MAKT.MAKTX, weight: 1.0}
        so a single-field object needs no weight at all."""
        entries: list[Any] = []
        entry: Any = None

        if not isinstance(value, list):
            return value
        for entry in value:
            if isinstance(entry, str):
                entries.append({"field": entry.strip(), "weight": 1.0})
            else:
                entries.append(entry)
        return entries

    def normalised_compare_weights(self) -> dict[str, float]:
        """Each compare field's share of the record score, summing to 1.0.

        The steward writes relative numbers; scoring needs proportions. The
        declared values are left untouched so a screen can show what was
        actually written.
        """
        total: float = 0.0
        shares: dict[str, float] = {}
        entry: Optional[CompareField] = None

        for entry in self.compare_fields:
            total += entry.weight
        if total <= 0:
            return shares
        for entry in self.compare_fields:
            shares[entry.field] = entry.weight / total
        return shares

    def normalised_method_weights(self) -> dict[str, float]:
        """The fuzzy and semantic shares of a field score, summing to 1.0."""
        total: float = self.methods.fuzzy.weight + self.methods.semantic.weight
        shares: dict[str, float] = {}

        if total <= 0:
            return shares
        shares["fuzzy"] = self.methods.fuzzy.weight / total
        shares["semantic"] = self.methods.semantic.weight / total
        return shares

    def effective_bands(self, shift: float = 0.0) -> dict[str, float]:
        """Apply an upstream advisory's band shift and report the arithmetic.

        A steward sets the bands; an advisory from Completeness or Validity may
        raise them. Both numbers and the result are returned together, so a
        finding can show all three and a steward can see why their setting did
        not take effect on its own.
        """
        moved_duplicate: float = 0.0
        moved_review_low: float = 0.0

        moved_duplicate = min(self.bands.duplicate + shift, MAX_BAND)
        moved_review_low = min(self.bands.review_low + shift, moved_duplicate - 0.01)
        return {
            "steward_duplicate": self.bands.duplicate,
            "steward_review_low": self.bands.review_low,
            "shift": shift,
            "duplicate": round(moved_duplicate, 4),
            "review_low": round(moved_review_low, 4),
        }

    def fingerprint(self) -> str:
        """A short, stable code for this configuration.

        Change any dial and the code changes. A run stamps it, so a screen can
        warn that a cluster on display was found under different settings and
        may no longer hold.
        """
        payload: str = ""

        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


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
