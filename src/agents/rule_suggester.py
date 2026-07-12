# ---------------------------------------------------------------------------
# src/agents/rule_suggester.py
# v1.0 | 09-Jul-2026 | Initial creation. The Rule Suggestion Agent: two engines
#                      (deterministic bank-match retrieval -> LLM adjudication;
#                      and data-driven inference for fields with no template),
#                      both emitting one CandidateSuggestion shape. Confidence
#                      is decomposable arithmetic, not a model-invented number.
#                      Inferred rules emit real RuleSpec IR (via contracts).
# ---------------------------------------------------------------------------
"""Rule Suggestion Agent - the centrepiece.

Two engines, one output shape:

  Engine 1 (bank match). Deterministic retrieval (RuleBank.retrieve) surfaces
  templates that PLAUSIBLY apply; an LLM adjudicator judges genuine fit. The
  rule IR is NOT authored by the model - it is the template's existing RuleSpec,
  with reference-sourced domains INSTANTIATED from the live reference table in
  code. Correct by construction.

  Engine 2 (inference). For fields with no template, the LLM proposes a rule
  SHAPE (archetype + values + rationale + an honest description_risk); code then
  assembles the RuleSpec IR from that structured decision. The model emits a
  decision, never executable IR - grounding made structural.

Confidence is a declared function of three inputs, computed in code:

    confidence = w_p * s_prior + w_s * s_support + w_c * s_coverage

so the review card can DECOMPOSE the number rather than assert it. The model
never supplies the confidence; it supplies judgement (accept/reject, rationale)
and the arithmetic is ours.

DSPy is imported lazily (only when a default program is built), so the pure
helpers and the injected-program paths are testable without DSPy or an LLM.
This module imports nothing from LangGraph.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from src.contracts import (
    Comparison,
    Operator,
    Provenance,
    RuleArchetype,
    RuleSpec,
    Severity,
    map_to_dama,
)
from src.agents.profile_interpreter import (
    FieldCharacterisation,
    TableInterpretation,
    build_field_observations,
    field_signals_from_profile,
)
from src.rules.reference_store import ReferenceStore
from src.rules.rule_bank import FieldObservation, RetrievalCandidate, RuleBank, Template


# ---------------------------------------------------------------------------
# Confidence: decomposable arithmetic
# ---------------------------------------------------------------------------

# Prior strength -> score. Origin sets strength; this maps it onto [0,1].
_STRENGTH_PRIOR: dict[str, float] = {"strong": 1.0, "moderate": 0.6, "weak": 0.3}

# Inference support is gated by the agent's own description_risk confession.
_DESCRIPTION_RISK_SUPPORT: dict[str, float] = {"low": 0.9, "medium": 0.6, "high": 0.3}

# Default weights. The rediscovery harness will calibrate these later; they are
# declared here so confidence is reproducible in the meantime.
DEFAULT_WEIGHTS: dict[str, float] = {"prior": 0.4, "support": 0.4, "coverage": 0.2}


@dataclass
class ConfidenceBreakdown:
    """The three inputs, the weights, and the resulting confidence - so the
    review card can show the calculation instead of asserting a number."""

    s_prior: float
    s_support: float
    s_coverage: float
    w_prior: float
    w_support: float
    w_coverage: float
    confidence: float


def strength_prior_score(strength: Optional[str]) -> float:
    """Map a prior strength label onto its prior score; unknown -> weak."""
    key: str = str(strength or "weak").strip().lower()
    return _STRENGTH_PRIOR.get(key, _STRENGTH_PRIOR["weak"])


def coverage_score(populated_count: Optional[int]) -> float:
    """How much data backs a rule, as a banded score. More rows -> more trust;
    a neat pattern in 40 rows should not score like one in 10,000."""
    count: int = int(populated_count) if isinstance(populated_count, (int, float)) else 0
    if count >= 1000:
        return 1.0
    if count >= 200:
        return 0.8
    if count >= 50:
        return 0.6
    if count >= 10:
        return 0.4
    return 0.2


def compute_confidence(
    s_prior: float,
    s_support: float,
    s_coverage: float,
    weights: Optional[dict[str, float]] = None,
) -> ConfidenceBreakdown:
    """Combine the three declared inputs into a decomposable confidence."""
    active: dict[str, float] = weights if weights is not None else DEFAULT_WEIGHTS
    w_prior: float = active.get("prior", DEFAULT_WEIGHTS["prior"])
    w_support: float = active.get("support", DEFAULT_WEIGHTS["support"])
    w_coverage: float = active.get("coverage", DEFAULT_WEIGHTS["coverage"])
    total: float = w_prior * s_prior + w_support * s_support + w_coverage * s_coverage
    clamped: float = max(0.0, min(1.0, total))
    return ConfidenceBreakdown(
        s_prior=s_prior,
        s_support=s_support,
        s_coverage=s_coverage,
        w_prior=w_prior,
        w_support=w_support,
        w_coverage=w_coverage,
        confidence=round(clamped, 4),
    )


# ---------------------------------------------------------------------------
# One candidate shape (both engines emit this)
# ---------------------------------------------------------------------------

@dataclass
class CandidateSuggestion:
    """A proposed rule with everything the approval gate needs to judge it."""

    rule_spec: dict[str, Any]
    origin: str                       # bank_match | inferred
    parameter_source: Optional[str]   # reference | template_fixed | data_derived
    rationale: str
    evidence_citations: list[str]
    confidence: ConfidenceBreakdown
    description_risk: str              # low for bank matches
    template_ref: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure helpers - deterministic scaffolding
# ---------------------------------------------------------------------------

def template_parameter_source(template: Template) -> Optional[str]:
    """The declared source of truth for the template's parameters."""
    params: list[Any] = template.parameterisation or []
    if params:
        return params[0].source
    return None


def template_reference_table(template: Template) -> Optional[str]:
    """The rule's own reference table (authoritative), read from provenance."""
    rule_spec: Any = template.rule_spec or {}
    provenance: Any = rule_spec.get("provenance", {}) if isinstance(rule_spec, dict) else {}
    if isinstance(provenance, dict):
        return provenance.get("reference_table")
    return None


def field_values_from_profile(profile: dict[str, Any], field_name: str) -> list[str]:
    """The distinct-ish values to test membership against a reference/domain.
    Prefers the profiler's inferred_domain, then top_values, then samples."""
    signals: dict[str, Any] = field_signals_from_profile(profile, field_name)
    inferred: Any = signals.get("inferred_domain")
    top_values: Any = signals.get("top_values")
    samples: Any = signals.get("sample_values")
    values: list[str] = []
    entry: Any = None

    if isinstance(inferred, list) and inferred:
        return [str(v) for v in inferred]
    if isinstance(top_values, list) and top_values:
        for entry in top_values:
            if isinstance(entry, dict):
                values.append(str(entry.get("value")))
            else:
                values.append(str(getattr(entry, "value", entry)))
        return values
    if isinstance(samples, list):
        return [str(v) for v in samples]
    return values


def reference_evidence(
    reference_store: ReferenceStore,
    reference_table: Optional[str],
    values: list[str],
    scope: Optional[tuple[str, ...]] = None,
) -> tuple[Optional[float], list[str]]:
    """Return (match_rate over the supplied distinct values, non-matching values).
    match_rate is None when the reference table is not loaded - absent evidence,
    not a failed check."""
    match_rate: Optional[float] = None
    non_matching: list[str] = []
    value: str = ""
    member: Optional[bool] = None

    if reference_table is None:
        return None, []
    match_rate = reference_store.match_rate(reference_table, values, scope=scope)
    if match_rate is None:
        return None, []
    for value in values:
        member = reference_store.is_member(reference_table, value, scope=scope)
        if member is False:
            non_matching.append(value)
    return match_rate, non_matching


def instantiate_bank_rulespec(
    template: Template,
    reference_values: Optional[list[str]],
) -> dict[str, Any]:
    """Build the candidate rule IR from the template's existing RuleSpec, with a
    reference-sourced domain instantiated from the live reference table. For
    template_fixed and not_null rules the IR is used as-is."""
    spec: dict[str, Any] = copy.deepcopy(template.rule_spec) if isinstance(template.rule_spec, dict) else {}
    param_source: Optional[str] = template_parameter_source(template)
    assertion: Any = spec.get("assertion")

    if param_source == "reference" and reference_values is not None:
        if isinstance(assertion, dict) and assertion.get("node") == "cmp" and assertion.get("op") == "in":
            assertion["value"] = list(reference_values)
    return spec


def assemble_inferred_rulespec(
    table: str,
    field_name: str,
    archetype: str,
    domain_values: list[str],
    description: str,
) -> Optional[dict[str, Any]]:
    """Assemble a real RuleSpec IR from the inference engine's structured
    decision. Emits IR, never executable code; validated against contracts so a
    malformed suggestion is dropped rather than passed downstream. Returns the
    serialised dict, or None for an unsupported archetype."""
    archetype_key: str = str(archetype or "").strip().lower()
    resolved_archetype: Optional[RuleArchetype] = None
    assertion: Optional[Comparison] = None
    dama: Any = None
    rule_id: str = ""
    spec: Optional[RuleSpec] = None

    if archetype_key == "not_null":
        resolved_archetype = RuleArchetype.NOT_NULL
        assertion = Comparison(field=field_name, op=Operator.IS_NOT_NULL)
    elif archetype_key == "domain_in":
        resolved_archetype = RuleArchetype.DOMAIN_IN
        if not domain_values:
            return None
        assertion = Comparison(field=field_name, op=Operator.IN, value=list(domain_values))
    else:
        return None

    dama = map_to_dama(None, resolved_archetype)
    rule_id = f"INF_{table}_{field_name}_{resolved_archetype.value}".upper()
    spec = RuleSpec(
        rule_id=rule_id,
        name=f"{field_name} {resolved_archetype.value} (inferred)",
        table=table,
        dama_dimension=dama,
        archetype=resolved_archetype,
        severity=Severity.MEDIUM,
        description=description or "",
        fields=[field_name],
        assertion=assertion,
        executable=True,
        provenance=Provenance(source="data_driven_inference", natural_language=description or None),
    )
    return json.loads(spec.model_dump_json(exclude_none=True))


# -- input formatters for the DSPy calls (kept small and grounded) ----------

def format_field_summary(profile: dict[str, Any], field_name: str) -> str:
    signals: dict[str, Any] = field_signals_from_profile(profile, field_name)
    parts: list[str] = []
    parts.append(f"field={field_name}")
    parts.append(f"population={signals.get('population')}")
    parts.append(f"distinct_count={signals.get('distinct_count')}")
    parts.append(f"type_hint={signals.get('type_hint')}")
    parts.append(f"top_value_share={signals.get('top_value_share')}")
    parts.append(f"sample_values={signals.get('sample_values')}")
    return "; ".join(parts)


def format_characterisation(characterisation: Optional[FieldCharacterisation]) -> str:
    if characterisation is None:
        return "none"
    parts: list[str] = []
    parts.append(f"semantic={characterisation.semantic_type_hypothesis}")
    parts.append(f"role_candidates={characterisation.field_role_candidates}")
    parts.append(f"domain_candidacy={characterisation.domain_candidacy}")
    parts.append(f"anomaly_notes={characterisation.anomaly_notes}")
    return "; ".join(parts)


def format_template_summary(template: Template) -> str:
    rule_spec: dict[str, Any] = template.rule_spec if isinstance(template.rule_spec, dict) else {}
    parts: list[str] = []
    parts.append(f"template_id={template.template_id}")
    parts.append(f"asserts={rule_spec.get('assertion')}")
    parts.append(f"dimension={template.provenance.get('dimension') if isinstance(template.provenance, dict) else None}")
    parts.append(f"prior_strength={template.prior_strength.strength}")
    parts.append(f"parameter_source={template_parameter_source(template)}")
    return "; ".join(parts)


def format_reference_summary(
    reference_table: Optional[str],
    match_rate: Optional[float],
    non_matching: list[str],
) -> str:
    if reference_table is None or match_rate is None:
        return "not a reference rule"
    shown: list[str] = non_matching[:10]
    return f"table={reference_table}; match_rate={round(match_rate, 4)}; non_matching={shown}"


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class RuleSuggester:
    """Runs both engines over a table's characterisations and returns candidate
    suggestions. Adjudicator and inferrer DSPy programs are injectable for
    testing; defaults are built lazily so importing this module needs no DSPy."""

    def __init__(
        self,
        bank: RuleBank,
        reference_store: ReferenceStore,
        adjudicator: Optional[Any] = None,
        inferrer: Optional[Any] = None,
        weights: Optional[dict[str, float]] = None,
        schema_context: str = "",
    ):
        self.bank = bank
        self.reference_store = reference_store
        self.weights = weights if weights is not None else DEFAULT_WEIGHTS
        self.schema_context = schema_context
        self._adjudicator = adjudicator
        self._inferrer = inferrer

    # -- lazy programs -------------------------------------------------------

    def _ensure_adjudicator(self) -> Any:
        program: Any = self._adjudicator
        if program is None:
            import dspy
            from src.dspy_modules.suggestion_signatures import BankMatchAdjudication

            program = dspy.ChainOfThought(BankMatchAdjudication)
            self._adjudicator = program
        return program

    def _ensure_inferrer(self) -> Any:
        program: Any = self._inferrer
        if program is None:
            import dspy
            from src.dspy_modules.suggestion_signatures import DataDrivenInference

            program = dspy.ChainOfThought(DataDrivenInference)
            self._inferrer = program
        return program

    # -- entry ---------------------------------------------------------------

    def suggest(
        self,
        profile: dict[str, Any],
        interpretation: TableInterpretation,
    ) -> list[CandidateSuggestion]:
        """Route each field: templates retrieved -> bank-match adjudication;
        no template -> data-driven inference. Returns all accepted candidates."""
        observations: list[FieldObservation] = build_field_observations(profile, interpretation)
        char_by_field: dict[str, FieldCharacterisation] = {}
        candidates: list[CandidateSuggestion] = []
        observation: FieldObservation = None
        characterisation: Optional[FieldCharacterisation] = None
        retrieved: list[RetrievalCandidate] = []

        for characterisation in interpretation.field_characterisations:
            char_by_field[characterisation.field_name] = characterisation

        for observation in observations:
            characterisation = char_by_field.get(observation.field_name)
            retrieved = self.bank.retrieve(observation)
            if retrieved:
                candidates.extend(
                    self._run_bank_match(profile, observation, characterisation, retrieved)
                )
            else:
                candidates.extend(
                    self._run_inference(profile, observation, characterisation)
                )
        return candidates

    # -- engine 1: bank match ------------------------------------------------

    def _run_bank_match(
        self,
        profile: dict[str, Any],
        observation: FieldObservation,
        characterisation: Optional[FieldCharacterisation],
        retrieved: list[RetrievalCandidate],
    ) -> list[CandidateSuggestion]:
        program: Any = self._ensure_adjudicator()
        candidates: list[CandidateSuggestion] = []
        signals: dict[str, Any] = field_signals_from_profile(profile, observation.field_name)
        values: list[str] = field_values_from_profile(profile, observation.field_name)
        retrieval_candidate: RetrievalCandidate = None
        template: Template = None
        param_source: Optional[str] = None
        reference_table: Optional[str] = None
        match_rate: Optional[float] = None
        non_matching: list[str] = []
        decision: Any = None
        accepted: bool = False
        rule_spec: dict[str, Any] = {}
        reference_values: Optional[list[str]] = None
        support: float = 0.0
        breakdown: ConfidenceBreakdown = None

        for retrieval_candidate in retrieved:
            template = retrieval_candidate.template
            param_source = template_parameter_source(template)
            reference_table = template_reference_table(template)
            match_rate, non_matching = reference_evidence(
                self.reference_store, reference_table, values
            )

            decision = program(
                field_summary=format_field_summary(profile, observation.field_name),
                field_characterisation=format_characterisation(characterisation),
                template_summary=format_template_summary(template),
                reference_summary=format_reference_summary(reference_table, match_rate, non_matching),
            )
            accepted = bool(_get(decision, "accepted", False))
            if not accepted:
                continue

            reference_values = None
            if param_source == "reference" and reference_table is not None:
                reference_values = self.reference_store.values(reference_table)
            rule_spec = instantiate_bank_rulespec(template, reference_values)
            if not _validates(rule_spec):
                continue

            support = _bank_support_score(template, signals, match_rate)
            breakdown = compute_confidence(
                s_prior=strength_prior_score(template.prior_strength.strength),
                s_support=support,
                s_coverage=coverage_score(signals.get("populated_count")),
                weights=self.weights,
            )
            candidates.append(
                CandidateSuggestion(
                    rule_spec=rule_spec,
                    origin="bank_match",
                    parameter_source=param_source,
                    rationale=str(_get(decision, "rationale", "")),
                    evidence_citations=_string_list(_get(decision, "evidence_citations", [])),
                    confidence=breakdown,
                    description_risk="low",
                    template_ref=template.template_id,
                )
            )
        return candidates

    # -- engine 2: inference -------------------------------------------------

    def _run_inference(
        self,
        profile: dict[str, Any],
        observation: FieldObservation,
        characterisation: Optional[FieldCharacterisation],
    ) -> list[CandidateSuggestion]:
        program: Any = self._ensure_inferrer()
        candidates: list[CandidateSuggestion] = []
        signals: dict[str, Any] = field_signals_from_profile(profile, observation.field_name)
        decision: Any = None
        should_suggest: bool = False
        archetype: str = ""
        domain_values: list[str] = []
        description_risk: str = "high"
        rule_spec: Optional[dict[str, Any]] = None
        breakdown: ConfidenceBreakdown = None

        decision = program(
            field_summary=format_field_summary(profile, observation.field_name),
            field_characterisation=format_characterisation(characterisation),
            schema_context=self.schema_context,
        )
        should_suggest = bool(_get(decision, "should_suggest", False))
        if not should_suggest:
            return candidates

        archetype = str(_get(decision, "proposed_archetype", "none"))
        domain_values = _string_list(_get(decision, "proposed_domain_values", []))
        description_risk = str(_get(decision, "description_risk", "high")).strip().lower()

        rule_spec = assemble_inferred_rulespec(
            table=observation.table,
            field_name=observation.field_name,
            archetype=archetype,
            domain_values=domain_values,
            description=str(_get(decision, "rationale", "")),
        )
        if rule_spec is None or not _validates(rule_spec):
            return candidates

        breakdown = compute_confidence(
            s_prior=_STRENGTH_PRIOR["weak"],
            s_support=_DESCRIPTION_RISK_SUPPORT.get(description_risk, 0.3),
            s_coverage=coverage_score(signals.get("populated_count")),
            weights=self.weights,
        )
        candidates.append(
            CandidateSuggestion(
                rule_spec=rule_spec,
                origin="inferred",
                parameter_source="data_derived",
                rationale=str(_get(decision, "rationale", "")),
                evidence_citations=_string_list(_get(decision, "evidence_citations", [])),
                confidence=breakdown,
                description_risk=description_risk,
                template_ref=None,
            )
        )
        return candidates


# ---------------------------------------------------------------------------
# Small internal utilities
# ---------------------------------------------------------------------------

def _get(source: Any, key: str, default: Any = None) -> Any:
    value: Any = default
    if isinstance(source, dict):
        value = source.get(key, default)
    else:
        value = getattr(source, key, default)
    return value


def _string_list(value: Any) -> list[str]:
    """Coerce an output into a list of strings; tolerate a bare string or None."""
    items: list[str] = []
    entry: Any = None
    if value is None:
        return items
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        for entry in value:
            items.append(str(entry))
        return items
    return [str(value)]


def _validates(rule_spec: dict[str, Any]) -> bool:
    """True if the dict round-trips through the real RuleSpec contract."""
    ok: bool = False
    try:
        RuleSpec.model_validate(rule_spec)
        ok = True
    except Exception:
        ok = False
    return ok


def _bank_support_score(
    template: Template,
    signals: dict[str, Any],
    match_rate: Optional[float],
) -> float:
    """Choose the support basis for a bank match: reference match rate for
    reference rules, population for not-null rules, else a neutral 0.7."""
    param_source: Optional[str] = template_parameter_source(template)
    rule_spec: dict[str, Any] = template.rule_spec if isinstance(template.rule_spec, dict) else {}
    archetype: str = str(rule_spec.get("archetype", "")).lower()
    population: Any = signals.get("population")

    if param_source == "reference" and match_rate is not None:
        return match_rate
    if "not_null" in archetype and isinstance(population, (int, float)):
        return float(population)
    if match_rate is not None:
        return match_rate
    return 0.7
