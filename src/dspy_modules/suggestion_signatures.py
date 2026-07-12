# ---------------------------------------------------------------------------
# src/dspy_modules/suggestion_signatures.py
# v1.0 | 09-Jul-2026 | Initial creation. DSPy signatures for the profiling and
#                      suggestion pipeline. This slice defines the profile
#                      interpretation signature + its structured output model;
#                      the bank-match adjudication and data-driven inference
#                      signatures are added in the Rule Suggestion Agent slice.
# v1.1 | 09-Jul-2026 | Add BankMatchAdjudication and DataDrivenInference
#                      signatures for the Rule Suggestion Agent's two engines.
# ---------------------------------------------------------------------------
"""DSPy signatures - the typed I/O contracts for the LLM steps.

These are DSPy signatures, not hand-written prompt strings: the instruction is
the class docstring and the field descriptions, and the output is a validated
object rather than free text to parse. That keeps prompting declarative,
optimisable, and portable (swapping the LLM backend in Phase 2 is a config
change, not a prompt rewrite).

Grounding is a first-class concern here. The signature is told to reason ONLY
from the provided profile evidence and the provided controlled role vocabulary,
never from outside SAP knowledge, and to fall back to the `unknown` role when no
listed role confidently fits. The agent layer (profile_interpreter) additionally
ENFORCES that discipline deterministically after the call - the model's role
choices are validated against the vocabulary in code, so this docstring is the
instruction, not the guarantee.
"""

from __future__ import annotations

from typing import Literal

import dspy
from pydantic import BaseModel, Field


class FieldCharacterisationLLM(BaseModel):
    """The model's structured reading of a single field.

    This is the RAW model output. It is validated and enriched (evidence
    citations attached from the real profile) by the agent layer into the public
    FieldCharacterisation dataclass; the model never supplies the evidence
    numbers itself, only its interpretation of them.
    """

    field_name: str = Field(description="The field this reading is about, exactly as it appears in the profile.")
    semantic_type_hypothesis: str = Field(
        description="A short plain-language guess at what the field IS, e.g. 'a unit-of-measure code'."
    )
    field_role_candidates: list[str] = Field(
        default_factory=list,
        description=(
            "Zero or more role ids drawn ONLY from the provided allowed_roles list. "
            "If no listed role confidently fits, return ['unknown']. Never invent a role."
        ),
    )
    domain_candidacy: Literal["closed", "open", "unclear"] = Field(
        default="unclear",
        description=(
            "Does the field behave like a closed domain (a small fixed set of codes) = 'closed', "
            "a free/open value space = 'open', or is it 'unclear' from the evidence?"
        ),
    )
    anomaly_notes: str = Field(
        default="",
        description="Any notable pattern grounded in the profile, e.g. 'population drops for MTART=ROH rows'. Empty if none.",
    )


class ProfileInterpretation(dspy.Signature):
    """Interpret a deterministic table profile for two audiences at once.

    You are given a table profile computed deterministically (population rates,
    distinct counts, value-length ranges, type hints, sample values). Treat it as
    the sole evidence of record. Do NOT use outside knowledge of SAP to assert
    facts the profile does not support; reason only from what the profile shows.

    Produce two things:
      1. A structured reading of each field (field_characterisations), assigning
         role candidates ONLY from the provided allowed_roles vocabulary. When no
         listed role confidently fits a field, assign ['unknown'] rather than
         guessing - a wrong role is worse than an honest 'unknown', which simply
         routes the field to data-driven inference later.
      2. A plain-language health_summary for a data-operations reader, plus a
         short list of concerns. Rank concerns by business impact, not by raw
         percentage: a small gap in a critical field outranks a large gap in a
         cosmetic one. Cite the figures you reason from.
    """

    table_name: str = dspy.InputField(description="The SAP table being interpreted, e.g. MARA.")
    table_profile: str = dspy.InputField(
        description="The deterministic profile as JSON: per-field population, distinct counts, type hints, value lengths, samples."
    )
    allowed_roles: str = dspy.InputField(
        description="The controlled field-role vocabulary. Role candidates MUST be chosen from this list; 'unknown' is always permitted."
    )
    business_context: str = dspy.InputField(
        description="Optional domain hints about which fields matter and what the table is for. May be empty."
    )

    field_characterisations: list[FieldCharacterisationLLM] = dspy.OutputField(
        description="One reading per field present in the profile."
    )
    health_summary: str = dspy.OutputField(
        description="A short plain-language readout of the table's data-quality health, citing the numbers it reasons from."
    )
    concerns: list[str] = dspy.OutputField(
        description="A short list of the most business-critical concerns, most important first. Empty if none."
    )


class BankMatchAdjudication(dspy.Signature):
    """Decide whether a rule-bank template genuinely fits a profiled field.

    Retrieval has already established that this template PLAUSIBLY applies. Your
    job is the precision step: does it REALLY fit, given the evidence? Reason
    only from the provided summaries - the field's profile, its characterisation,
    what the template asserts, and the reference-table evidence (a match rate and
    any non-matching values). Do not use outside knowledge to fill gaps.

    Accept only when the evidence supports the rule. When you accept, the
    rationale must cite counter-evidence too (e.g. the values that did NOT match
    and why they are plausibly typos rather than a second legitimate domain) - an
    explanation that presents only supporting evidence is advocacy, not analysis.
    Reject when the misfit is real (for instance, a high non-match rate that looks
    like a genuinely different value space rather than dirt).
    """

    field_summary: str = dspy.InputField(description="Population, distinct count, type, sample values for the field.")
    field_characterisation: str = dspy.InputField(description="Role candidates, domain candidacy and any anomaly notes.")
    template_summary: str = dspy.InputField(description="What the template asserts, its dimension, archetype and prior strength.")
    reference_summary: str = dspy.InputField(description="Reference match rate and any non-matching values, or 'not a reference rule'.")

    accepted: bool = dspy.OutputField(description="True if the template genuinely fits and should be suggested.")
    rationale: str = dspy.OutputField(description="Why, in plain language, citing supporting AND counter-evidence.")
    evidence_citations: list[str] = dspy.OutputField(description="Pointers to the specific figures reasoned from.")


class DataDrivenInference(dspy.Signature):
    """Infer a candidate rule for a field with NO matching bank template.

    Reason only from the field's profile and characterisation. Propose a rule
    ONLY when the evidence supports a genuine quality requirement, not merely a
    description of the current data. Choose 'not_null' when the field is
    near-fully populated and plausibly mandatory, or 'domain_in' when it behaves
    like a closed set of codes; otherwise set should_suggest false.

    You MUST fill description_risk honestly. It is the risk that your candidate
    merely DESCRIBES today's data rather than PRESCRIBING quality - the classic
    trap '97% of rows have status X, so X is mandatory' when the other 3% may be
    the correct rows. If you cannot argue why this is a rule and not a
    coincidence, say so with a high description_risk and, usually, should_suggest
    false. Confessing uncertainty is better than a confident over-fit.
    """

    field_summary: str = dspy.InputField(description="Population, distinct count, type, sample values, dominant-value share.")
    field_characterisation: str = dspy.InputField(description="Semantic guess, domain candidacy and any anomaly notes.")
    schema_context: str = dspy.InputField(description="Optional schema hints about the field. May be empty.")

    should_suggest: bool = dspy.OutputField(description="True only if the evidence supports a genuine rule.")
    proposed_archetype: str = dspy.OutputField(description="'not_null' or 'domain_in' when suggesting; else 'none'.")
    proposed_domain_values: list[str] = dspy.OutputField(description="For domain_in, the proposed permitted values; else an empty list.")
    rationale: str = dspy.OutputField(description="Why this is a rule and not a coincidence, citing the evidence.")
    evidence_citations: list[str] = dspy.OutputField(description="Pointers to the specific figures reasoned from.")
    description_risk: str = dspy.OutputField(description="'low', 'medium' or 'high': the risk this describes the data rather than prescribing quality.")
