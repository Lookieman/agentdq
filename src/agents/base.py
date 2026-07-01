# v0.1 | 27-Jun-2026 | Initial agent base class and result contract

"""Base classes for the dimension agents.

Every agent takes the loaded tables, their schemas and the rule set, and returns
an AgentResult: the findings it raised plus a small summary. The deterministic
dimension agents (Completeness, Validity, Consistency) share almost all of their
behaviour, so it lives in RuleBackedAgent; each concrete agent differs only in
the DAMA dimension it owns.

The language-model agents to come (Accuracy, and the interpretation layer) will
subclass BaseAgent directly, since their run method does something other than
execute deterministic rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from src.contracts import Dimension, Finding, RuleSpec
from src.data.schema import TableSchema
from src.rules.executor import execute_ruleset


class AgentResult(BaseModel):
    """What an agent returns: its findings and a compact summary."""

    dimension: Dimension
    agent: str
    findings: list[Finding] = Field(default_factory=list)
    rules_run: int = 0
    findings_by_table: dict[str, int] = Field(default_factory=dict)
    findings_by_field: dict[str, int] = Field(default_factory=dict)


class BaseAgent(ABC):
    """Common interface every agent implements."""

    dimension: Dimension
    name: str

    @abstractmethod
    def run(
        self,
        frames: dict[str, pd.DataFrame],
        schemas: dict[str, TableSchema],
        rules: list[RuleSpec],
    ) -> AgentResult:
        """Assess the data and return findings plus a summary."""
        raise NotImplementedError


class RuleBackedAgent(BaseAgent):
    """A deterministic agent that executes the rules for its dimension.

    It selects the executable rules whose DAMA dimension it owns, runs them
    through the pandas executor, and rolls the findings up into a summary. No
    language model is involved: the result is fully reproducible.
    """

    def __init__(self, dimension: Dimension, name: str) -> None:
        self.dimension = dimension
        self.name = name

    def select_rules(self, rules: list[RuleSpec]) -> list[RuleSpec]:
        """Return the executable rules this agent is responsible for."""
        selected: list[RuleSpec] = []
        rule: RuleSpec = None
        for rule in rules:
            if rule.executable and rule.dama_dimension == self.dimension:
                selected.append(rule)
        return selected

    def run(
        self,
        frames: dict[str, pd.DataFrame],
        schemas: dict[str, TableSchema],
        rules: list[RuleSpec],
    ) -> AgentResult:
        """Execute this dimension's rules and summarise the findings."""
        selected: list[RuleSpec] = self.select_rules(rules)
        findings: list[Finding] = execute_ruleset(selected, frames, schemas)
        by_table: Counter = Counter()
        by_field: Counter = Counter()
        finding: Finding = None

        for finding in findings:
            by_table[finding.table] += 1
            by_field[f"{finding.table}.{finding.field}"] += 1

        return AgentResult(
            dimension=self.dimension,
            agent=self.name,
            findings=findings,
            rules_run=len(selected),
            findings_by_table=dict(by_table),
            findings_by_field=dict(by_field),
        )
