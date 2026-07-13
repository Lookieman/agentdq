# ---------------------------------------------------------------------------
# app/gate.py
# v1.0 | 13-Jul-2026 | Initial creation. The suggestion review surface (the
#                      approval gate): decomposed confidence card, approve /
#                      edit / reject with reason, manual rule authoring. The
#                      surface renders and invokes repository verbs; it NEVER
#                      decides. Backend selected by AGENTDQ_REPO_MODE:
#                      "file" (default, real store) or "session" (public demo
#                      sandbox, per-visitor, discarded on exit).
# ---------------------------------------------------------------------------
"""Approval gate surface.

Run standalone:      streamlit run app/gate.py
Or mount as a page in the existing dashboard (st.Page / pages/ directory).

Every button handler is one repository verb call. If a piece of decision logic
is tempted to live in this file, it belongs in src/rules/repository.py instead
- that rule is what keeps the Phase 2 CAP/Fiori re-plumbing and the public demo
cheap (design doc 3.4-3.5).
"""

from __future__ import annotations

import json
import os
from typing import Any

import streamlit as st

from src.rules.repository import (
    CandidateRecord,
    FileRepository,
    RulesRepository,
    SessionRepository,
)

REPO_MODE: str = os.environ.get("AGENTDQ_REPO_MODE", "file")
REPO_DIR: str = os.environ.get("AGENTDQ_REPO_DIR", "data/repository")
DEFAULT_ARTEFACT: str = os.environ.get("AGENTDQ_SUGGESTIONS", "artefacts/suggestions_mara.json")
HIGHLIGHT_FLOOR: float = 0.95  # presentation dial only; calibrated in Package 5


def get_repository() -> RulesRepository:
    """One repository per session. The session backend gives each demo visitor
    an isolated sandbox; the file backend is the real store."""
    if "repository" not in st.session_state:
        if REPO_MODE == "session":
            st.session_state.repository = SessionRepository()
        else:
            st.session_state.repository = FileRepository(REPO_DIR)
    return st.session_state.repository


def render_confidence_card(record: CandidateRecord) -> None:
    """The decomposed confidence card: the explanation IS the calculation."""
    confidence: dict[str, Any] = record.confidence or {}
    columns = st.columns(4)
    columns[0].metric("Confidence", f"{confidence.get('confidence', '-')}")
    columns[1].metric("Prior", f"{confidence.get('s_prior', '-')}")
    columns[2].metric("Support", f"{confidence.get('s_support', '-')}")
    columns[3].metric("Coverage", f"{confidence.get('s_coverage', '-')}")
    st.caption(
        f"origin: {record.origin} | parameter source: {record.parameter_source} "
        f"| description risk: {record.description_risk} "
        f"| template: {record.template_ref or '-'} | strength: {record.strength} "
        f"({record.strength_reason})"
    )


def render_draft(record: CandidateRecord, repository: RulesRepository) -> None:
    rule_id: str = str(record.rule_spec.get("rule_id", record.candidate_id))
    confidence_value: Any = (record.confidence or {}).get("confidence")
    is_highlight: bool = isinstance(confidence_value, (int, float)) and confidence_value >= HIGHLIGHT_FLOOR
    title: str = f"{'[HIGH] ' if is_highlight else ''}{rule_id}"

    with st.expander(title, expanded=is_highlight):
        render_confidence_card(record)
        st.markdown(f"**Rationale:** {record.rationale or '-'}")
        if record.evidence_citations:
            st.markdown("**Evidence:**")
            for citation in record.evidence_citations:
                st.code(citation, language=None)
        st.json(record.rule_spec, expanded=False)

        actor: str = st.session_state.get("actor", "steward")
        left, middle, right = st.columns(3)

        if left.button("Approve", key=f"approve_{record.candidate_id}"):
            repository.approve(record.candidate_id, actor=actor)
            st.rerun()

        reason: str = middle.text_input("Rejection reason", key=f"reason_{record.candidate_id}")
        if middle.button("Reject", key=f"reject_{record.candidate_id}"):
            if reason:
                repository.reject(record.candidate_id, actor=actor, reason=reason)
                st.rerun()
            else:
                middle.warning("A rejection requires a reason.")

        edited: str = right.text_area(
            "Edited RuleSpec (JSON)", key=f"edit_{record.candidate_id}",
            value=json.dumps(record.rule_spec, indent=2), height=180,
        )
        if right.button("Edit + approve", key=f"editapprove_{record.candidate_id}"):
            try:
                repository.edit_and_approve(
                    record.candidate_id, json.loads(edited), actor=actor,
                    reason="edited at the gate",
                )
                st.rerun()
            except Exception as error:  # surfaced, not swallowed
                right.error(f"Edit rejected: {error}")


def render_manual_form(repository: RulesRepository) -> None:
    """Customer-authored rules enter through the SAME gate (design doc 9.3):
    validated against contracts, provenance.source=customer_authored, strength
    'unverified'. No side door."""
    st.subheader("Author a customer-specific rule")
    st.caption("The rule enters the same draft -> approved lifecycle as agent suggestions.")
    spec_text: str = st.text_area("RuleSpec (JSON)", height=220, key="manual_spec")
    rationale: str = st.text_input("Why this rule?", key="manual_rationale")
    if st.button("Add as draft", key="manual_add"):
        try:
            record = repository.add_manual_candidate(
                json.loads(spec_text),
                actor=st.session_state.get("actor", "steward"),
                rationale=rationale,
            )
            st.success(f"Draft created: {record.candidate_id}")
            st.rerun()
        except Exception as error:
            st.error(f"Not accepted: {error}")


def main() -> None:
    st.set_page_config(page_title="AgentDQ - Suggestion Review", layout="wide")
    st.title("Suggestion review")
    if REPO_MODE == "session":
        st.info(
            "Sandbox mode: your decisions are private to this browser session "
            "and vanish on refresh. The suggestions shown are real agent "
            "output, pre-computed by a batch run."
        )

    repository: RulesRepository = get_repository()
    st.session_state.setdefault("actor", "steward")

    with st.sidebar:
        st.text_input("Acting as", key="actor")
        artefact_path: str = st.text_input("Suggestions artefact", value=DEFAULT_ARTEFACT)
        if st.button("Ingest artefact"):
            try:
                count: int = repository.load_candidates(artefact_path)
                st.success(f"Ingested {count} candidates as drafts.")
            except Exception as error:
                st.error(f"Ingest failed: {error}")
        st.metric("Drafts", len(repository.by_state("draft")))
        st.metric("Approved", len(repository.by_state("approved")))
        st.metric("Rejected", len(repository.by_state("rejected")))

    drafts: list[CandidateRecord] = sorted(
        repository.drafts(),
        key=_confidence_sort_key,
        reverse=True,
    )
    if not drafts:
        st.write("No drafts awaiting review. Ingest a suggestions artefact from the sidebar.")
    for record in drafts:
        render_draft(record, repository)

    st.divider()
    render_manual_form(repository)


def _confidence_sort_key(record: CandidateRecord) -> float:
    value: Any = (record.confidence or {}).get("confidence")
    return float(value) if isinstance(value, (int, float)) else 0.0


if __name__ == "__main__":
    main()
