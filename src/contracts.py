# v0.1 | 27-Jun-2026 | Initial shared domain contracts for AgentDQ
# v0.2 | 27-Jun-2026 | Add predicate-tree IR (Predicate, RuleSpec, Provenance); supersede Rule
# v0.3 | 27-Jun-2026 | Add DefectLabel (ground-truth counterpart to Finding)

"""Shared domain contracts for AgentDQ.

These are the small, foundational types passed between every module: the
findings agents produce, the rule definitions agents consume, and the canonical
re-mapping from the legacy Information Steward dimension taxonomy onto the six
DAMA data quality dimensions.

Centralising them stops each agent inventing its own shape for a finding or a
rule, and keeps the IS-to-DAMA lineage auditable in one place.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


class Dimension(str, Enum):
    """The six DAMA data quality dimensions, one per specialist agent."""

    COMPLETENESS = "Completeness"
    VALIDITY = "Validity"
    CONSISTENCY = "Consistency"
    ACCURACY = "Accuracy"
    TIMELINESS = "Timeliness"
    UNIQUENESS = "Uniqueness"


class ISDimension(str, Enum):
    """The legacy Information Steward dimension taxonomy.

    Retained for lineage so each imported rule records the label it carried in
    the source workbook alongside its re-mapped DAMA dimension.
    """

    COMPLETENESS = "Completeness"
    CONSISTENCY = "Consistency"
    CONFORMITY = "Conformity"
    ACCURACY = "Accuracy"


class RuleArchetype(str, Enum):
    """Structural shape of a rule expression.

    The archetype drives both the DAMA re-mapping and how an agent executes the
    rule, so it is captured explicitly rather than re-derived at run time.
    """

    NOT_NULL = "not_null"                   # field must be populated
    DOMAIN_IN = "domain_in"                 # value must be in an inline list
    REFERENCE_EXISTS = "reference_exists"   # value must exist in a check table
    FORMAT_REGEX = "format_regex"           # value must match a pattern
    CROSS_FIELD = "cross_field"             # multi-field conditional logic
    OTHER = "other"


class Severity(str, Enum):
    """Business impact ranking for a finding."""

    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


# Default IS -> DAMA mapping by IS label alone. The rules importer overrides
# this using the rule archetype where the IS label is ambiguous - most notably
# IS "Accuracy", which splits into DAMA Validity for domain checks and DAMA
# Consistency for cross-field logic.
IS_DIMENSION_DEFAULT: dict[ISDimension, Dimension] = {
    ISDimension.COMPLETENESS: Dimension.COMPLETENESS,
    ISDimension.CONSISTENCY: Dimension.VALIDITY,
    ISDimension.CONFORMITY: Dimension.VALIDITY,
    ISDimension.ACCURACY: Dimension.VALIDITY,
}

# Archetype -> DAMA mapping. Takes precedence over the label default when the
# archetype is known, because the structural shape is the more reliable signal
# of what the rule actually tests.
ARCHETYPE_DIMENSION: dict[RuleArchetype, Dimension] = {
    RuleArchetype.NOT_NULL: Dimension.COMPLETENESS,
    RuleArchetype.DOMAIN_IN: Dimension.VALIDITY,
    RuleArchetype.REFERENCE_EXISTS: Dimension.VALIDITY,
    RuleArchetype.FORMAT_REGEX: Dimension.VALIDITY,
    RuleArchetype.CROSS_FIELD: Dimension.CONSISTENCY,
}


def map_to_dama(is_dimension: Optional[ISDimension], archetype: RuleArchetype) -> Dimension:
    """Resolve the DAMA dimension for a rule.

    The archetype wins when known; otherwise the IS label default applies. The
    final fallback is Validity, the most common case in the source workbook.
    Timeliness and Uniqueness are never produced here - the workbook contains no
    such rules and those agents are built from scratch.
    """
    resolved: Optional[Dimension] = None

    if archetype in ARCHETYPE_DIMENSION:
        resolved = ARCHETYPE_DIMENSION[archetype]
    elif is_dimension in IS_DIMENSION_DEFAULT:
        resolved = IS_DIMENSION_DEFAULT[is_dimension]
    else:
        resolved = Dimension.VALIDITY

    return resolved


class Operator(str, Enum):
    """Comparison operators usable in a leaf predicate."""

    IN = "in"
    NOT_IN = "not_in"
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    MATCHES = "matches"


class BoolOp(str, Enum):
    """Boolean connectives usable in a branch predicate."""

    AND = "and"
    OR = "or"
    NOT = "not"
    IMPLIES = "implies"


class Comparison(BaseModel):
    """A leaf predicate: one field compared against a value or tested for null.

    value holds a list for IN/NOT_IN, a regex string for MATCHES, a scalar for
    the ordered comparisons, and is unused for the null tests.
    """

    node: Literal["cmp"] = "cmp"
    field: str
    op: Operator
    value: Optional[Any] = None


class BoolNode(BaseModel):
    """A branch predicate: a boolean connective over child predicates.

    NOT takes one operand; IMPLIES takes exactly two (antecedent, consequent);
    AND and OR take two or more.
    """

    node: Literal["bool"] = "bool"
    op: BoolOp
    operands: list["Predicate"]


# A predicate is either a leaf comparison or a boolean branch. The 'node'
# discriminator keeps the union unambiguous when loaded from YAML or JSON.
Predicate = Annotated[Union[Comparison, BoolNode], Field(discriminator="node")]


class Provenance(BaseModel):
    """Where a rule came from, retained for lineage and audit.

    Both authoring front-ends populate this: the IS importer records the
    original expression and reference table, the natural-language agent records
    the original request text.
    """

    source: str = "information_steward"
    original_name: Optional[str] = None
    original_expression: Optional[str] = None
    reference_table: Optional[str] = None
    rule_doc: Optional[str] = None
    natural_language: Optional[str] = None


class RuleSpec(BaseModel):
    """The canonical, platform-neutral rule representation.

    scope selects the rows a rule applies to (a same-table filter); assertion is
    what must hold for those rows. A row is in violation when it is in scope and
    the assertion does not hold. Simple archetypes use a single Comparison as the
    assertion; compositional rules use a BoolNode tree.

    executable records whether the rule can run against the current schema (its
    fields exist and any required domain is resolved); dormant rules are retained
    in the catalogue with a note rather than discarded.
    """

    rule_id: str
    name: str
    table: str
    dama_dimension: Dimension
    is_dimension: Optional[ISDimension] = None
    archetype: RuleArchetype
    severity: Severity = Severity.MEDIUM
    description: str = ""
    fields: list[str] = Field(default_factory=list)
    scope: Optional[Predicate] = None
    assertion: Predicate
    executable: bool = True
    binding_notes: Optional[str] = None
    provenance: Provenance = Field(default_factory=Provenance)


class Finding(BaseModel):
    """A single data quality issue raised by an agent.

    record_id is the business key of the offending record (MATNR, or a composite
    such as MATNR/WERKS for plant-level tables). field is None for record-level
    findings such as a duplicate cluster. confidence is 1.0 for deterministic
    rule checks and below 1.0 for fuzzy or LLM-adjudicated findings.
    """

    dimension: Dimension
    table: str
    record_id: str
    field: Optional[str] = None
    rule_id: Optional[str] = None
    issue: str = ""
    severity: Severity = Severity.MEDIUM
    confidence: float = 1.0
    observed_value: Optional[Any] = None
    expected: Optional[Any] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return value


class DefectLabel(BaseModel):
    """Ground truth for one injected defect.

    The counterpart to Finding: where a Finding is what an agent claims, a
    DefectLabel is what the injector knows to be true. Comparing the two yields
    precision, recall and F1 per dimension. record_key matches the composite key
    a Finding reports, so the two line up directly.
    """

    defect_id: str
    table: str
    record_key: str
    dimension: Dimension
    field: Optional[str] = None
    rule_id: Optional[str] = None
    original_value: Optional[Any] = None
    corrupted_value: Optional[Any] = None
    detail: dict[str, Any] = Field(default_factory=dict)


# Resolve the forward reference in BoolNode.operands now that Predicate exists.
BoolNode.model_rebuild()
