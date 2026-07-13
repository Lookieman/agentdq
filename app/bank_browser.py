# ---------------------------------------------------------------------------
# app/bank_browser.py
# v1.0 | 13-Jul-2026 | Initial creation. The Rule Bank browser: templates with
#                      binding, applicability and strength; plus strength
#                      governance over APPROVED rules (human-only, audited
#                      promotion via repository.promote_strength). Renders and
#                      invokes; never decides.
# ---------------------------------------------------------------------------
"""Rule Bank browser + strength governance surface.

Run standalone:      streamlit run app/bank_browser.py

Two panels: the bank's templates (read-only reference material - the bank is
built by tools/build_rule_bank.py and curated in YAML, not edited live), and
the approved-rule strength governance panel, where a Data Manager promotes or
demotes strength with a recorded reason. Agents never touch this verb.
"""

from __future__ import annotations

import os
from typing import Any

import streamlit as st

from src.rules.repository import FileRepository, RulesRepository, SessionRepository
from src.rules.rule_bank import RuleBank, Template

REPO_MODE: str = os.environ.get("AGENTDQ_REPO_MODE", "file")
REPO_DIR: str = os.environ.get("AGENTDQ_REPO_DIR", "data/repository")
BANK_DIR: str = os.environ.get("AGENTDQ_BANK_DIR", "config/rule_bank")


def get_repository() -> RulesRepository:
    if "repository" not in st.session_state:
        if REPO_MODE == "session":
            st.session_state.repository = SessionRepository()
        else:
            st.session_state.repository = FileRepository(REPO_DIR)
    return st.session_state.repository


def render_template(template: Template) -> None:
    with st.expander(f"{template.template_id}  [{template.prior_strength.strength}]"):
        st.caption(
            f"binding: table={template.binding.target_table} "
            f"field={template.binding.target_field or '-'} "
            f"role={template.binding.field_role or '-'}"
        )
        st.caption(
            f"strength: {template.prior_strength.strength} "
            f"({template.prior_strength.strength_reason}, "
            f"source={template.prior_strength.strength_source})"
        )
        st.json(
            {
                "applicability": vars(template.applicability),
                "parameterisation": [vars(p) for p in template.parameterisation],
                "provenance": template.provenance,
            },
            expanded=False,
        )


def render_strength_governance(repository: RulesRepository) -> None:
    st.subheader("Strength governance (approved rules)")
    st.caption(
        "Promotion is human-only and audited: every change records who, when "
        "and why in the ledger. Agents never write strength."
    )
    approved = repository.by_state("approved")
    if not approved:
        st.write("No approved rules yet.")
        return

    for record in approved:
        rule_id: str = str(record.rule_spec.get("rule_id", record.candidate_id))
        with st.expander(f"{rule_id}  [strength: {record.strength}]"):
            st.caption(f"reason: {record.strength_reason} | origin: {record.origin}")
            strength: str = st.selectbox(
                "New strength", ["strong", "moderate", "weak"],
                key=f"strength_{record.candidate_id}",
            )
            reason: str = st.selectbox(
                "Reason", ["regulatory", "governance_policy", "business_critical",
                           "proven_template", "unverified"],
                key=f"strength_reason_{record.candidate_id}",
            )
            note: str = st.text_input("Note (e.g. the mandate)", key=f"note_{record.candidate_id}")
            if st.button("Apply", key=f"promote_{record.candidate_id}"):
                try:
                    repository.promote_strength(
                        record.candidate_id, strength=strength, reason=reason,
                        actor=st.session_state.get("actor", "data_manager"), note=note,
                    )
                    st.success("Strength updated (ledger entry written).")
                    st.rerun()
                except Exception as error:
                    st.error(f"Not applied: {error}")


def main() -> None:
    st.set_page_config(page_title="AgentDQ - Rule Bank", layout="wide")
    st.title("Rule bank")
    st.session_state.setdefault("actor", "data_manager")
    with st.sidebar:
        st.text_input("Acting as", key="actor")

    bank: RuleBank = RuleBank.load(BANK_DIR)
    st.caption(f"{len(bank.templates)} templates | {len(bank.roles)} roles in the vocabulary")

    table_filter: str = st.text_input("Filter by table (blank = all)", value="")
    shown: int = 0
    template: Template = None
    for template in bank.templates:
        if table_filter and template.binding.target_table != table_filter.strip().upper():
            continue
        render_template(template)
        shown += 1
    if shown == 0:
        st.write("No templates match the filter (or the bank has not been built - "
                 "run tools/build_rule_bank.py).")

    st.divider()
    render_strength_governance(get_repository())


if __name__ == "__main__":
    main()
