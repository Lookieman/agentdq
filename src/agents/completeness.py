# v0.1 | 27-Jun-2026 | Initial Completeness agent

"""Completeness agent.

Owns the DAMA Completeness dimension: are the fields that must be populated
actually populated? It executes the not-null rules imported from Information
Steward against each table and reports the gaps. Being rule-backed, it is a thin,
deterministic wrapper over the executor - the interesting work already happened
in the rules and the executor.
"""

from __future__ import annotations

from src.agents.base import RuleBackedAgent
from src.contracts import Dimension


class CompletenessAgent(RuleBackedAgent):
    """Executes the not-null rules for the Completeness dimension."""

    def __init__(self) -> None:
        super().__init__(Dimension.COMPLETENESS, "Completeness Agent")
