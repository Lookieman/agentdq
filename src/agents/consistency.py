# v0.1 | 27-Jun-2026 | Initial Consistency agent

"""Consistency agent.

Owns the DAMA Consistency dimension: do cross-field and cross-record logical
relationships hold? It executes the compositional cross-field rules (scoped
implies rules such as "reorder-point MRP types require a maximum stock level")
against each table. Like the other rule-backed agents it is a thin, deterministic
wrapper over the executor; the executor's three-valued logic handles the
scope-and-implies evaluation, and only rules flagged executable are run - so
IR examples that are not yet activated for evaluation are skipped.
"""

from __future__ import annotations

from src.agents.base import RuleBackedAgent
from src.contracts import Dimension


class ConsistencyAgent(RuleBackedAgent):
    """Executes the cross-field rules for the Consistency dimension."""

    def __init__(self) -> None:
        super().__init__(Dimension.CONSISTENCY, "Consistency Agent")
