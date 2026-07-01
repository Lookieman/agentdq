# v0.1 | 27-Jun-2026 | Initial Validity agent

"""Validity agent.

Owns the DAMA Validity dimension: do populated values conform to their permitted
domains? It executes the domain rules (imported reference and inline-domain
checks, bound to our self-defined domains) against each table. Null values are
left to the Completeness agent - the executor's three-valued logic ensures a
missing value is treated as unknown here, not as a validity breach.
"""

from __future__ import annotations

from src.agents.base import RuleBackedAgent
from src.contracts import Dimension


class ValidityAgent(RuleBackedAgent):
    """Executes the domain rules for the Validity dimension."""

    def __init__(self) -> None:
        super().__init__(Dimension.VALIDITY, "Validity Agent")
