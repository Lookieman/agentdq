# ---------------------------------------------------------------------------
# src/dspy_modules/adjudication_signatures.py
# v1.0 | 11-Aug-2026 | Package 4g/4h. Two signatures: the pair adjudication that
#                      judges one uncertain pair, and the remediation synthesis
#                      that writes the narrative for a grouped plan. Both are
#                      typed contracts, not prompt strings, so the backend swap
#                      in Package 6 is a configuration change.
# ---------------------------------------------------------------------------
"""DSPy signatures for the judgement steps of Package 4.

Grounding discipline applies to both. The instruction tells the model to reason
ONLY from the supplied evidence, never from what it knows about SAP or about
any part catalogue. The agent layer enforces the output shape in code after the
call, so the docstring is the instruction and the code is the guarantee.
"""

from __future__ import annotations

from typing import Literal

import dspy
from pydantic import BaseModel, Field


class PairAdjudication(dspy.Signature):
    """Decide whether two material descriptions name ONE material or TWO.

    Reason only from the evidence given below. Do not use outside knowledge of
    part catalogues or of SAP. The deterministic scores could not settle this
    pair, so attend to what a difference MEANS: a changed part number or size
    usually names a different material; a reordered, abbreviated or reworded
    description usually names the same one.

    State your confidence honestly. A steward reads the reasoning and decides;
    nothing is merged on your answer alone.
    """

    description_a: str = dspy.InputField(desc="The first description, as written in the data.")
    description_b: str = dspy.InputField(desc="The second description, as written in the data.")
    material_type: str = dspy.InputField(desc="The shared material type code (MTART) of the block.")
    unit: str = dspy.InputField(desc="The shared base unit of measure (MEINS) of the block.")
    fuzzy_score: float = dspy.InputField(desc="Letter-by-letter similarity, 0 to 1.")
    semantic_score: float = dspy.InputField(
        desc="Meaning-based similarity, 0 to 1. -1 when the semantic rung did not run."
    )
    decision: Literal["duplicate", "not_duplicate"] = dspy.OutputField(
        desc="duplicate when the two descriptions name one material."
    )
    confidence: float = dspy.OutputField(desc="Your confidence in the decision, 0 to 1.")
    reasoning: str = dspy.OutputField(
        desc="One or two sentences a data steward can act on, citing the evidence above."
    )


class ActionNarrative(BaseModel):
    """The model's narrative for ONE remediation group."""

    group_id: str = Field(description="The id of the group this narrative is for, copied exactly.")
    action: str = Field(description="One imperative sentence: what to do.")
    rationale: str = Field(
        description="One or two sentences: why this action, citing only the supplied numbers."
    )


class RemediationSynthesis(dspy.Signature):
    """Write the narrative for a ranked data-quality remediation plan.

    Reason only from the groups supplied below: their dimension, table, field,
    affected record counts, severity and reason codes. Do not invent SAP
    transactions, table names or numbers that are not in the input. Where two
    groups share a cause (for example, records that failed a validity check on
    a description were also held out of duplicate matching), say so in the
    executive summary, because one fix that serves two dimensions outranks two
    separate fixes.
    """

    groups_json: str = dspy.InputField(
        desc="A JSON list of remediation groups: group_id, dimension, table, field, "
             "affected_records, severity, reason, sample_issues."
    )
    narratives: list[ActionNarrative] = dspy.OutputField(
        desc="One narrative per group, keyed by group_id. Cover every group."
    )
    executive_summary: str = dspy.OutputField(
        desc="Three to five sentences for a data-operations lead: the state of the data, "
             "the top priorities, and any one-fix-serves-two link you found in the groups."
    )
