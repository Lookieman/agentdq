# ---------------------------------------------------------------------------
# src/rules/rule_bank.py
# v1.0 | 05-Jul-2026 | Initial creation. Template schema dataclasses, bank +
#                      field-role loaders, and the DETERMINISTIC retrieval
#                      join (recall-oriented, single rate floor). No LLM here.
# ---------------------------------------------------------------------------
"""Rule bank: templates (IS rules wrapped with match metadata) and the
deterministic retrieval join that surfaces candidate templates for a profiled
field.

Design contract (see project plan, "Rule Bank, Profiling and Suggestion"):

  * A Template = the RuleSpec IR (held here denormalised as a dict, to keep the
    bank self-contained and portable) + a match layer: binding, applicability
    signals, parameterisation, and a governed prior_strength_block.
  * Retrieval is RECALL-oriented. It is a hard gate on binding plus a single
    generous rate floor (default 0.80) on min-rate signals; everything else is
    a soft score used only for ranking. A miss at retrieval is silent and
    invisible, so we retrieve generously and let the (later) adjudicator judge.
  * The 0.95 "highlight" dial lives downstream in the Suggestion Agent / review
    UI, NOT here. Retrieval must never inherit a precision threshold.

This module deliberately imports nothing from LangGraph or DSPy; it is pure,
deterministic, and unit-testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Strength governance
# ---------------------------------------------------------------------------

VALID_STRENGTHS = ("strong", "moderate", "weak")
VALID_STRENGTH_SOURCES = ("default", "steward_set")
VALID_STRENGTH_REASONS = (
    "proven_template",
    "regulatory",
    "governance_policy",
    "business_critical",
    "inferred",
    "unverified",
)


@dataclass
class PriorStrengthBlock:
    """The governed strength attribute. Origin sets the default; governance
    (a human Data Manager) owns the current value. Agents NEVER write this."""

    strength: str = "weak"
    strength_source: str = "default"
    strength_reason: str = "inferred"
    set_by: Optional[str] = None
    set_at: Optional[str] = None
    note: Optional[str] = None

    def validate(self) -> None:
        # Declare-first, then check.
        strength_ok = self.strength in VALID_STRENGTHS
        source_ok = self.strength_source in VALID_STRENGTH_SOURCES
        reason_ok = self.strength_reason in VALID_STRENGTH_REASONS
        if not strength_ok:
            raise ValueError(f"invalid strength: {self.strength!r}")
        if not source_ok:
            raise ValueError(f"invalid strength_source: {self.strength_source!r}")
        if not reason_ok:
            raise ValueError(f"invalid strength_reason: {self.strength_reason!r}")


# ---------------------------------------------------------------------------
# Match layer
# ---------------------------------------------------------------------------

PARAMETER_SOURCES = ("reference", "template_fixed", "data_derived")


@dataclass
class Binding:
    """Where a template attaches. Bind by field role for generalisation, or by
    explicit field name for a table-specific rule. `target_table` may be ANY."""

    target_table: str = "ANY"
    target_field: Optional[str] = None
    field_role: Optional[str] = None


@dataclass
class Applicability:
    """The profile fingerprint that makes a template a CANDIDATE. Every signal
    is optional. Signals are typed by how retrieval treats them:

      min-rate (hard-gated by the rate floor): population_min, reference_match_min
      soft band (score only):                   distinct_count_min/max
      soft hint (score only):                   max_value_length, type_hint
    """

    population_min: Optional[float] = None
    reference_match_min: Optional[float] = None
    distinct_count_min: Optional[int] = None
    distinct_count_max: Optional[int] = None
    max_value_length: Optional[int] = None
    type_hint: Optional[str] = None


@dataclass
class Parameter:
    """A parameter of the rule and, crucially, its source of truth - the
    anti-overfitting handle carried into the approval UI."""

    name: str
    source: str = "template_fixed"

    def validate(self) -> None:
        source_ok = self.source in PARAMETER_SOURCES
        if not source_ok:
            raise ValueError(f"invalid parameter source: {self.source!r}")


@dataclass
class Template:
    """A rule-bank template: RuleSpec IR + match metadata."""

    template_id: str
    source_rule_id: Optional[str]
    rule_spec: dict[str, Any]
    provenance: dict[str, Any]
    binding: Binding
    applicability: Applicability
    parameterisation: list[Parameter] = field(default_factory=list)
    prior_strength: PriorStrengthBlock = field(default_factory=PriorStrengthBlock)

    def validate(self) -> None:
        self.prior_strength.validate()
        for parameter in self.parameterisation:
            parameter.validate()


# ---------------------------------------------------------------------------
# The profiled-field observation that retrieval is run against
# ---------------------------------------------------------------------------

@dataclass
class FieldObservation:
    """The evidence retrieval reasons over for one field. Assembled from the
    deterministic profiler output and the Profiling Agent's field
    characterisation (role_candidates). reference_match_rate is optional and,
    when present, is supplied by the reference store."""

    table: str
    field_name: str
    role_candidates: list[str] = field(default_factory=list)
    population: Optional[float] = None
    distinct_count: Optional[int] = None
    max_value_length: Optional[int] = None
    type_hint: Optional[str] = None
    reference_match_rate: Optional[float] = None


@dataclass
class RetrievalConfig:
    """Two dials, kept far apart. Only `rate_floor` gates retrieval here;
    `highlight_floor` is recorded for the downstream review UI and is NOT used
    to exclude candidates at retrieval."""

    rate_floor: float = 0.80       # recall dial - the generous retrieval floor
    highlight_floor: float = 0.95  # precision dial - used downstream, not here


@dataclass
class RetrievalCandidate:
    """A retrieved (field, template) pair with a ranking score and the reasons
    it matched. This is NOT yet a suggestion - adjudication comes later."""

    template: Template
    match_score: float
    matched_on: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_field_roles(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load the controlled role vocabulary, keyed by role id."""
    role_path = Path(path)
    raw = yaml.safe_load(role_path.read_text(encoding="utf-8"))
    roles_list = raw.get("roles", []) if isinstance(raw, dict) else []
    roles_by_id: dict[str, dict[str, Any]] = {}
    for entry in roles_list:
        role_id = entry.get("id")
        if role_id is not None:
            roles_by_id[role_id] = entry
    return roles_by_id


def _binding_from_dict(data: dict[str, Any]) -> Binding:
    source = data or {}
    return Binding(
        target_table=source.get("target_table", "ANY"),
        target_field=source.get("target_field"),
        field_role=source.get("field_role"),
    )


def _applicability_from_dict(data: dict[str, Any]) -> Applicability:
    source = data or {}
    return Applicability(
        population_min=source.get("population_min"),
        reference_match_min=source.get("reference_match_min"),
        distinct_count_min=source.get("distinct_count_min"),
        distinct_count_max=source.get("distinct_count_max"),
        max_value_length=source.get("max_value_length"),
        type_hint=source.get("type_hint"),
    )


def _parameters_from_list(data: list[dict[str, Any]]) -> list[Parameter]:
    source = data or []
    parameters: list[Parameter] = []
    for entry in source:
        parameters.append(
            Parameter(name=entry.get("name", ""), source=entry.get("source", "template_fixed"))
        )
    return parameters


def _strength_from_dict(data: dict[str, Any]) -> PriorStrengthBlock:
    source = data or {}
    return PriorStrengthBlock(
        strength=source.get("strength", "weak"),
        strength_source=source.get("strength_source", "default"),
        strength_reason=source.get("strength_reason", "inferred"),
        set_by=source.get("set_by"),
        set_at=source.get("set_at"),
        note=source.get("note"),
    )


def template_from_dict(data: dict[str, Any]) -> Template:
    """Rehydrate a Template from its serialised YAML form."""
    template = Template(
        template_id=data["template_id"],
        source_rule_id=data.get("source_rule_id"),
        rule_spec=data.get("rule_spec", {}),
        provenance=data.get("provenance", {}),
        binding=_binding_from_dict(data.get("binding", {})),
        applicability=_applicability_from_dict(data.get("applicability", {})),
        parameterisation=_parameters_from_list(data.get("parameterisation", [])),
        prior_strength=_strength_from_dict(data.get("prior_strength", {})),
    )
    return template


class RuleBank:
    """Holds templates and the controlled role vocabulary; answers retrieval."""

    def __init__(self, templates: list[Template], roles: dict[str, dict[str, Any]]):
        self.templates = templates
        self.roles = roles

    # -- construction --------------------------------------------------------

    @classmethod
    def load(cls, bank_dir: str | Path, roles_path: str | Path | None = None) -> "RuleBank":
        """Load all *.yaml template files in `bank_dir` (except field_roles.yaml)
        and the role vocabulary. If `roles_path` is None it defaults to
        `bank_dir/field_roles.yaml`."""
        directory = Path(bank_dir)
        resolved_roles_path = Path(roles_path) if roles_path else directory / "field_roles.yaml"
        roles = load_field_roles(resolved_roles_path)

        templates: list[Template] = []
        for yaml_path in sorted(directory.glob("*.yaml")):
            if yaml_path.name == "field_roles.yaml":
                continue
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            entries = raw.get("templates", []) if isinstance(raw, dict) else []
            for entry in entries:
                template = template_from_dict(entry)
                template.validate()
                templates.append(template)
        return cls(templates=templates, roles=roles)

    # -- retrieval -----------------------------------------------------------

    def retrieve(
        self,
        observation: FieldObservation,
        config: RetrievalConfig | None = None,
    ) -> list[RetrievalCandidate]:
        """Return candidate templates for a field, ranked by match_score.

        Hard gates (must pass): binding match, and any present min-rate signal
        clearing the rate floor. Soft signals adjust the score only.
        """
        active_config = config if config is not None else RetrievalConfig()
        candidates: list[RetrievalCandidate] = []

        for template in self.templates:
            binding_ok, binding_reasons = self._binding_matches(template, observation)
            if not binding_ok:
                continue
            rate_ok = self._rate_gates_pass(template, observation, active_config.rate_floor)
            if not rate_ok:
                continue
            score, soft_reasons = self._soft_score(template, observation)
            matched_on = binding_reasons + soft_reasons
            candidates.append(
                RetrievalCandidate(template=template, match_score=score, matched_on=matched_on)
            )

        candidates.sort(key=_candidate_sort_key, reverse=True)
        return candidates

    # -- gates ---------------------------------------------------------------

    @staticmethod
    def _binding_matches(
        template: Template, observation: FieldObservation
    ) -> tuple[bool, list[str]]:
        binding = template.binding
        reasons: list[str] = []

        table_ok = binding.target_table == "ANY" or binding.target_table == observation.table
        if not table_ok:
            return False, reasons
        reasons.append(f"table:{binding.target_table}")

        # Field match precedence: explicit name, else role, else table-level.
        if binding.target_field is not None:
            if binding.target_field == observation.field_name:
                reasons.append(f"field:{binding.target_field}")
                return True, reasons
            return False, reasons

        if binding.field_role is not None:
            if binding.field_role in observation.role_candidates:
                reasons.append(f"role:{binding.field_role}")
                return True, reasons
            return False, reasons

        # No field/role binding: a table-level template applies broadly.
        reasons.append("table_level")
        return True, reasons

    @staticmethod
    def _rate_gates_pass(
        template: Template, observation: FieldObservation, rate_floor: float
    ) -> bool:
        applicability = template.applicability

        # population: only gates when the template cares AND we observed it.
        if applicability.population_min is not None and observation.population is not None:
            if observation.population < rate_floor:
                return False

        # reference match rate: same treatment; absent evidence never excludes.
        if applicability.reference_match_min is not None and observation.reference_match_rate is not None:
            if observation.reference_match_rate < rate_floor:
                return False

        return True

    @staticmethod
    def _soft_score(
        template: Template, observation: FieldObservation
    ) -> tuple[float, list[str]]:
        applicability = template.applicability
        score = 0.5
        reasons: list[str] = []

        # Reward rate signals that clear the template's own (stricter) ideal.
        if applicability.population_min is not None and observation.population is not None:
            if observation.population >= applicability.population_min:
                score += 0.10
                reasons.append("population_ideal_met")
        if applicability.reference_match_min is not None and observation.reference_match_rate is not None:
            if observation.reference_match_rate >= applicability.reference_match_min:
                score += 0.20
                reasons.append("reference_match_ideal_met")

        # Distinct-count band (soft): inside band boosts, outside gently penalises.
        if observation.distinct_count is not None:
            low = applicability.distinct_count_min
            high = applicability.distinct_count_max
            if low is not None or high is not None:
                inside_low = low is None or observation.distinct_count >= low
                inside_high = high is None or observation.distinct_count <= high
                if inside_low and inside_high:
                    score += 0.10
                    reasons.append("distinct_count_in_band")
                else:
                    score -= 0.05

        # Type hint (soft).
        if applicability.type_hint is not None and observation.type_hint is not None:
            if applicability.type_hint == observation.type_hint:
                score += 0.05
                reasons.append("type_hint_match")

        # Max value length (soft).
        if applicability.max_value_length is not None and observation.max_value_length is not None:
            if observation.max_value_length <= applicability.max_value_length:
                score += 0.05
                reasons.append("value_length_ok")

        clamped = max(0.0, min(1.0, score))
        return clamped, reasons


def _candidate_sort_key(candidate: RetrievalCandidate) -> tuple[float, str]:
    """Deterministic ordering: score desc, then template_id for stable ties."""
    return (candidate.match_score, candidate.template.template_id)
