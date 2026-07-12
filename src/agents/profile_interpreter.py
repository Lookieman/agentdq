# ---------------------------------------------------------------------------
# src/agents/profile_interpreter.py
# v1.0 | 09-Jul-2026 | Initial creation. The Profiling Agent: turns a
#                      deterministic table profile into (1) structured field
#                      characterisations for the Suggestion Agent and (2) a
#                      plain-language readout for humans. Role choices are
#                      validated against the controlled vocabulary in code;
#                      evidence citations are attached from the real profile.
# v1.1 | 09-Jul-2026 | Move dspy import inside the default-program branch so an
#                      injected program keeps interpret() entirely dspy-free.
# v1.2 | 09-Jul-2026 | Reconcile field_signals_from_profile to the real profiler
#                      shape (populated_pct, min_length/max_length, top_values);
#                      convert population to a 0..1 fraction; translate the
#                      type_hint vocabulary at the seam; cite RAW profile values
#                      in evidence_refs (split from derived matching signals).
# v1.3 | 09-Jul-2026 | Extract build_field_observations() to module level so the
#                      Rule Suggestion Agent can reuse the socket.
# ---------------------------------------------------------------------------
"""Profiling Agent (interpretation layer).

Deliberately NOT named profiler.py - that is the deterministic measurement
module (src/data/profiler.py). This module sits on top of that module's JSON
output and interprets it. The contract:

    The raw deterministic profile is the evidence of record. This agent adds
    hypotheses; it never replaces the numbers. Every characterisation's
    evidence_refs point back at the raw profile, and are built HERE in code -
    the model supplies interpretation, not figures.

Two grounding controls are enforced deterministically, after the model call,
so they are guarantees rather than requests:

  * Role validation. Any role the model returns that is not in the controlled
    vocabulary is dropped. A field left with no valid role becomes ['unknown'],
    which routes it to the Suggestion Agent's inference engine rather than
    forcing a bad bank join.
  * Evidence citations. evidence_refs are computed from the profile signals,
    not taken from the model, so a citation can never be fabricated.

DSPy is imported lazily (only when the default program is built), so the pure
helpers and the injected-program path are testable without DSPy or an LLM.

Configuring the LLM is the caller's job, e.g.:

    import dspy
    dspy.configure(lm=dspy.LM("anthropic/<your-claude-model>"))

then construct ProfileInterpreter() with no program and call interpret().

This module imports nothing from LangGraph; it is a plain class the orchestrator
will later wrap as a node.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.rules.rule_bank import FieldObservation, load_field_roles


# ---------------------------------------------------------------------------
# Public output types
# ---------------------------------------------------------------------------

@dataclass
class FieldCharacterisation:
    """The validated, evidence-anchored reading of one field. This is what the
    Suggestion Agent consumes."""

    field_name: str
    semantic_type_hypothesis: str
    field_role_candidates: list[str]
    domain_candidacy: str
    anomaly_notes: str
    evidence_refs: list[str] = field(default_factory=list)
    role_coerced_to_unknown: bool = False


@dataclass
class TableInterpretation:
    """The Profiling Agent's two outputs for a table: the structured
    characterisations and the human-facing readout."""

    table_name: str
    field_characterisations: list[FieldCharacterisation] = field(default_factory=list)
    health_summary: str = ""
    concerns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small accessor that works for both dicts and objects (DSPy may return either)
# ---------------------------------------------------------------------------

def _get(source: Any, key: str, default: Any = None) -> Any:
    """Read `key` from a dict or an attribute from an object."""
    value: Any = default
    if isinstance(source, dict):
        value = source.get(key, default)
    else:
        value = getattr(source, key, default)
    return value


# ---------------------------------------------------------------------------
# Pure helpers (no DSPy, no LLM) - the deterministic scaffolding
# ---------------------------------------------------------------------------

def format_allowed_roles(roles: dict[str, dict[str, Any]]) -> str:
    """Render the controlled vocabulary as a compact list the model can read.
    Each line is 'role_id: description'. `unknown` is included explicitly."""
    lines: list[str] = []
    role_id: str = ""
    entry: dict[str, Any] = {}
    description: str = ""

    for role_id, entry in roles.items():
        description = str(entry.get("description", "")).strip()
        lines.append(f"{role_id}: {description}")
    return "\n".join(lines)


# The profiler's type_hint vocabulary is tuned for the synthetic generator; the
# rule bank reasons over a coarser vocabulary. We translate at this seam so the
# profiler keeps its richer hints and the bank keeps its own. v1.2
_TYPE_HINT_TRANSLATION: dict[str, str] = {
    "categorical": "categorical_string",
    "constant": "categorical_string",
    "key": "identifier",
    "numeric_text": "numeric",
    "free_text": "free_text",
}


def translate_type_hint(raw_hint: Any) -> Optional[str]:  # v1.2
    """Map the profiler's type_hint onto the bank's normalised vocabulary.
    Unrecognised hints pass through unchanged; None stays None."""
    hint_text: str = ""
    if raw_hint is None:
        return None
    hint_text = str(raw_hint).strip()
    return _TYPE_HINT_TRANSLATION.get(hint_text, hint_text)


def _field_record(profile: dict[str, Any], field_name: str) -> dict[str, Any]:  # v1.2
    """Return the raw per-field record from the profiler's TableProfile JSON."""
    fields: dict[str, Any] = profile.get("fields", {}) if isinstance(profile, dict) else {}
    record: Any = fields.get(field_name, {}) if isinstance(fields, dict) else {}
    if not isinstance(record, dict):
        return {}
    return record


def field_signals_from_profile(profile: dict[str, Any], field_name: str) -> dict[str, Any]:
    """Extract the DERIVED matching signals for one field.

    INTEGRATION POINT - the single place that knows the deterministic profiler's
    JSON shape (src/data/profiler.py -> TableProfile / FieldProfile). Real keys:

        profile = {
          "table": "MARA", "row_count": 2798,
          "fields": {
            "MEINS": {
              "populated_count": 2798, "populated_pct": 100.0,   # 0..100 scale
              "distinct_count": 17,
              "type_hint": "categorical",                        # profiler vocab
              "min_length": 1, "max_length": 3,
              "top_values": [{"value": "ST", "count": 1200}, ...],
              "inferred_domain": [...], "sample_values": [...]
            }, ...
          }
        }

    Note the two DERIVATIONS (both code-computed, never from the model):
      * population is converted from populated_pct (0..100) to a 0..1 fraction,
        because the rule-bank retrieval floor (0.80) is a fraction.
      * type_hint is translated into the bank's normalised vocabulary.
    Raw values are cited separately by build_evidence_refs so the evidence of
    record stays the raw profile, not these derivations.
    """
    record: dict[str, Any] = _field_record(profile, field_name)
    row_count: Any = profile.get("row_count") if isinstance(profile, dict) else None
    populated_count: Any = record.get("populated_count")
    populated_pct: Any = record.get("populated_pct")
    top_values: Any = record.get("top_values")
    top_count: Any = None
    signals: dict[str, Any] = {}

    # population as a 0..1 fraction (profiler emits populated_pct on 0..100).
    if populated_pct is not None:
        signals["population"] = populated_pct / 100.0
    else:
        signals["population"] = None

    signals["populated_count"] = populated_count
    signals["distinct_count"] = record.get("distinct_count")
    signals["type_hint"] = translate_type_hint(record.get("type_hint"))
    signals["max_value_length"] = record.get("max_length")
    signals["min_value_length"] = record.get("min_length")

    # null_count is not emitted by the profiler; derive it from row_count.
    if row_count is not None and populated_count is not None:
        signals["null_count"] = row_count - populated_count
    else:
        signals["null_count"] = None

    signals["sample_values"] = record.get("sample_values")
    signals["inferred_domain"] = record.get("inferred_domain")
    signals["top_values"] = top_values

    # Dominant-value share: strong evidence for domain candidacy.
    signals["top_value_share"] = None
    if isinstance(top_values, list) and top_values and populated_count:
        top_count = _get(top_values[0], "count")
        if isinstance(top_count, (int, float)) and populated_count > 0:
            signals["top_value_share"] = top_count / populated_count

    return signals


def build_evidence_refs(table_name: str, field_name: str, profile: dict[str, Any]) -> list[str]:
    """Build canonical citations into the RAW profile, deterministically. These
    cite the profiler's own numbers (populated_pct on 0..100, the raw type_hint),
    NOT the derived matching signals, so the evidence of record is the profile
    itself. The model never supplies these, so they cannot be fabricated."""
    table_key: str = (table_name or "table").lower()
    field_key: str = (field_name or "field").lower()
    record: dict[str, Any] = _field_record(profile, field_name)
    refs: list[str] = []
    cited_keys: tuple[str, ...] = (
        "populated_pct",
        "distinct_count",
        "type_hint",
        "min_length",
        "max_length",
    )
    key: str = ""
    value: Any = None
    domain_value: Any = None
    top_values: Any = None
    top_entry: Any = None

    for key in cited_keys:
        value = record.get(key)
        if value is not None:
            refs.append(f"profile.{table_key}.{field_key}.{key}={value}")

    domain_value = record.get("inferred_domain")
    if isinstance(domain_value, list):
        refs.append(f"profile.{table_key}.{field_key}.inferred_domain_size={len(domain_value)}")

    # Most frequent value and its raw count - evidence for domain candidacy.
    top_values = record.get("top_values")
    if isinstance(top_values, list) and top_values:
        top_entry = top_values[0]
        refs.append(f"profile.{table_key}.{field_key}.top_value={_get(top_entry, 'value')}")
        refs.append(f"profile.{table_key}.{field_key}.top_value_count={_get(top_entry, 'count')}")

    return refs


def validate_role_candidates(
    candidates: Any,
    allowed_role_ids: set[str],
) -> tuple[list[str], bool]:
    """Enforce the controlled vocabulary. Keep only roles that exist in the
    vocabulary; if none survive, return (['unknown'], True) so the field is
    routed to inference rather than mis-bound. The escape hatch is a code
    guarantee, not a model courtesy."""
    validated: list[str] = []
    coerced: bool = False
    role: Any = None
    role_text: str = ""

    if isinstance(candidates, (list, tuple)):
        for role in candidates:
            role_text = str(role).strip()
            if role_text in allowed_role_ids and role_text not in validated:
                validated.append(role_text)

    if not validated:
        validated = ["unknown"]
        coerced = True
    return validated, coerced


def normalise_characterisation(
    raw: Any,
    allowed_role_ids: set[str],
    evidence_refs: list[str],
) -> FieldCharacterisation:
    """Turn a raw model reading (dict or object) into the validated public
    characterisation, attaching the deterministic evidence citations."""
    field_name: str = str(_get(raw, "field_name", "") or "")
    semantic_type: str = str(_get(raw, "semantic_type_hypothesis", "") or "")
    raw_roles: Any = _get(raw, "field_role_candidates", [])
    domain_candidacy: str = str(_get(raw, "domain_candidacy", "unclear") or "unclear")
    anomaly_notes: str = str(_get(raw, "anomaly_notes", "") or "")
    validated_roles: list[str] = []
    coerced: bool = False

    validated_roles, coerced = validate_role_candidates(raw_roles, allowed_role_ids)

    return FieldCharacterisation(
        field_name=field_name,
        semantic_type_hypothesis=semantic_type,
        field_role_candidates=validated_roles,
        domain_candidacy=domain_candidacy,
        anomaly_notes=anomaly_notes,
        evidence_refs=evidence_refs,
        role_coerced_to_unknown=coerced,
    )


def build_field_observations(  # v1.3
    profile: dict[str, Any],
    interpretation: "TableInterpretation",
) -> list[FieldObservation]:
    """Assemble FieldObservations (the rule bank's retrieval input) from the
    characterisations plus the profile signals. This is the socket: role
    candidates + profile evidence become the query the bank joins against.
    reference_match_rate is left None here; the Suggestion Agent fills it from
    the reference store when it needs it. Module-level so both the Profiling
    Agent and the Rule Suggestion Agent can call it."""
    observations: list[FieldObservation] = []
    characterisation: Any = None
    signals: dict[str, Any] = {}

    for characterisation in interpretation.field_characterisations:
        signals = field_signals_from_profile(profile, characterisation.field_name)
        observations.append(
            FieldObservation(
                table=interpretation.table_name,
                field_name=characterisation.field_name,
                role_candidates=characterisation.field_role_candidates,
                population=signals.get("population"),
                distinct_count=signals.get("distinct_count"),
                max_value_length=signals.get("max_value_length"),
                type_hint=signals.get("type_hint"),
                reference_match_rate=None,
            )
        )
    return observations


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class ProfileInterpreter:
    """Interprets a deterministic profile into structured characterisations and
    a readout. DSPy program is injectable for testing; the default is built
    lazily so importing this module does not require DSPy."""

    def __init__(
        self,
        roles_path: str | Path,
        program: Optional[Any] = None,
        business_context: str = "",
    ):
        self.roles = load_field_roles(roles_path)
        self.allowed_role_ids = set(self.roles.keys())
        self.business_context = business_context
        self._program = program

    # -- program construction -----------------------------------------------

    def _ensure_program(self) -> Any:
        """Return the DSPy program, building the default ChainOfThought lazily.
        DSPy is imported ONLY when the default program is actually built, so an
        injected program keeps this path entirely DSPy-free (and testable
        without DSPy installed)."""
        program: Any = self._program  # v1.1
        if program is None:  # v1.1
            import dspy  # local import by design; only when building the default
            from src.dspy_modules.suggestion_signatures import ProfileInterpretation

            program = dspy.ChainOfThought(ProfileInterpretation)
            self._program = program
        return program  # v1.1

    # -- main entry ----------------------------------------------------------

    def interpret(self, profile: dict[str, Any]) -> TableInterpretation:
        """Run interpretation for one table profile.

        Deterministic scaffolding assembles inputs and post-processes outputs;
        the only non-deterministic step is the program call in the middle."""
        table_name: str = str(profile.get("table", "") or "")
        profile_json: str = json.dumps(profile, ensure_ascii=False, default=str)
        allowed_roles_text: str = format_allowed_roles(self.roles)
        program: Any = self._ensure_program()
        prediction: Any = None
        raw_characterisations: Any = None
        characterisations: list[FieldCharacterisation] = []
        raw: Any = None
        field_name: str = ""
        evidence_refs: list[str] = []
        health_summary: str = ""
        concerns: Any = None

        prediction = program(
            table_name=table_name,
            table_profile=profile_json,
            allowed_roles=allowed_roles_text,
            business_context=self.business_context,
        )

        raw_characterisations = _get(prediction, "field_characterisations", []) or []
        for raw in raw_characterisations:
            field_name = str(_get(raw, "field_name", "") or "")
            evidence_refs = build_evidence_refs(table_name, field_name, profile)  # v1.2
            characterisations.append(
                normalise_characterisation(raw, self.allowed_role_ids, evidence_refs)
            )

        health_summary = str(_get(prediction, "health_summary", "") or "")
        concerns = _get(prediction, "concerns", []) or []
        if not isinstance(concerns, list):
            concerns = [str(concerns)]

        return TableInterpretation(
            table_name=table_name,
            field_characterisations=characterisations,
            health_summary=health_summary,
            concerns=list(concerns),
        )

    # -- bridge to the rule bank --------------------------------------------

    def field_observations(
        self,
        profile: dict[str, Any],
        interpretation: TableInterpretation,
    ) -> list[FieldObservation]:
        """Thin wrapper over the module-level build_field_observations()."""  # v1.3
        return build_field_observations(profile, interpretation)  # v1.3
