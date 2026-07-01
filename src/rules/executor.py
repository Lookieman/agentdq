# v0.1 | 27-Jun-2026 | Initial pandas rule executor (predicate IR -> Findings)

"""Phase-1 pandas executor for the canonical rule IR.

Walks a rule's predicate tree against a DataFrame and emits one Finding per
violating row. It is entirely deterministic - no language model is involved,
which is the whole point of compiling rules from a declarative IR rather than
asking a model to write code.

Evaluation follows the uniform semantics the IR was designed for:

    in_scope  = scope is None OR eval(scope, row) is True
    violated  = in_scope AND NOT eval(assertion, row)

Null handling uses three-valued (Kleene) logic via pandas' nullable boolean
type. This matters for correctness: a validity check such as BESKZ in [E,F,X]
must treat a populated wrong code as a violation but a null value as unknown
(a completeness concern, not a validity one), so only definitively-false
assertions are flagged. pandas propagates NA through and/or/not for us.

In Phase 2 the same IR compiles to SQL instead; this module is the pandas
backend, hence the explicit class name.
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from src.contracts import (
    BoolNode,
    BoolOp,
    Comparison,
    Finding,
    Operator,
    Predicate,
    RuleArchetype,
    RuleSpec,
)
from src.data.schema import TableSchema


class PandasRuleExecutor:
    """Evaluates rule predicates against one table's DataFrame."""

    def __init__(self, schema: TableSchema) -> None:
        self.schema: TableSchema = schema

    # --- predicate evaluation -------------------------------------------------

    def evaluate(self, predicate: Predicate, frame: pd.DataFrame) -> pd.Series:
        """Evaluate a predicate to a nullable boolean Series (Kleene logic)."""
        if isinstance(predicate, Comparison):
            return self._eval_comparison(predicate, frame)
        return self._eval_bool(predicate, frame)

    def _eval_comparison(self, cmp: Comparison, frame: pd.DataFrame) -> pd.Series:
        """Evaluate a leaf comparison to a nullable boolean Series."""
        column: pd.Series = None
        populated: pd.Series = None
        result: pd.Series = None
        numeric: pd.Series = None
        threshold: float = 0.0

        if cmp.field not in frame.columns:
            return pd.Series(pd.NA, index=frame.index, dtype="boolean")

        column = frame[cmp.field]
        populated = column.notna()

        if cmp.op == Operator.IS_NULL:
            return (~populated).astype("boolean")
        if cmp.op == Operator.IS_NOT_NULL:
            return populated.astype("boolean")

        if cmp.op == Operator.IN:
            result = column.isin(cmp.value or []).astype("boolean")
        elif cmp.op == Operator.NOT_IN:
            result = (~column.isin(cmp.value or [])).astype("boolean")
        elif cmp.op == Operator.EQ:
            result = (column == cmp.value).astype("boolean")
        elif cmp.op == Operator.NE:
            result = (column != cmp.value).astype("boolean")
        elif cmp.op == Operator.MATCHES:
            result = column.astype("string").str.match(str(cmp.value)).astype("boolean")
        elif cmp.op in (Operator.GT, Operator.GE, Operator.LT, Operator.LE):
            numeric = pd.to_numeric(column.map(self.schema.parse_quantity), errors="coerce")
            threshold = float(cmp.value)
            if cmp.op == Operator.GT:
                result = (numeric > threshold).astype("boolean")
            elif cmp.op == Operator.GE:
                result = (numeric >= threshold).astype("boolean")
            elif cmp.op == Operator.LT:
                result = (numeric < threshold).astype("boolean")
            else:
                result = (numeric <= threshold).astype("boolean")
            populated = numeric.notna()
        else:
            result = pd.Series(pd.NA, index=frame.index, dtype="boolean")

        # Where the value is unpopulated the comparison is unknown, not false.
        result = result.mask(~populated, pd.NA)
        return result

    def _eval_bool(self, node: BoolNode, frame: pd.DataFrame) -> pd.Series:
        """Evaluate a boolean branch, combining child masks with Kleene logic."""
        child_masks: list[pd.Series] = [self.evaluate(operand, frame) for operand in node.operands]
        combined: pd.Series = None
        mask: pd.Series = None

        if node.op == BoolOp.NOT:
            return ~child_masks[0]
        if node.op == BoolOp.IMPLIES:
            # implies(a, b) == (not a) or b
            return (~child_masks[0]) | child_masks[1]

        combined = child_masks[0]
        for mask in child_masks[1:]:
            if node.op == BoolOp.AND:
                combined = combined & mask
            else:
                combined = combined | mask
        return combined

    # --- rule execution -------------------------------------------------------

    def run_rule(self, rule: RuleSpec, frame: pd.DataFrame) -> list[Finding]:
        """Return one Finding per row that violates the rule."""
        scope_mask: pd.Series = None
        assertion_mask: pd.Series = None
        violated: pd.Series = None
        findings: list[Finding] = []
        missing_fields: list[str] = [f for f in rule.fields if f not in frame.columns]
        idx: Any = None

        if missing_fields:
            return findings

        if rule.scope is None:
            scope_mask = pd.Series(True, index=frame.index, dtype="boolean")
        else:
            scope_mask = self.evaluate(rule.scope, frame)

        assertion_mask = self.evaluate(rule.assertion, frame)
        # A row is in violation only where it is in scope and the assertion is
        # definitively false; unknown (NA) never counts as a violation.
        violated = (scope_mask & (~assertion_mask)).fillna(False)

        for idx in frame.index[violated]:
            findings.append(self._build_finding(rule, frame.loc[idx]))
        return findings

    def run_rules(self, rules: list[RuleSpec], frame: pd.DataFrame) -> list[Finding]:
        """Run several rules (all for this executor's table) and gather findings."""
        findings: list[Finding] = []
        rule: RuleSpec = None
        for rule in rules:
            findings.extend(self.run_rule(rule, frame))
        return findings

    def _build_finding(self, rule: RuleSpec, row: pd.Series) -> Finding:
        """Construct a Finding for one violating row."""
        field: Optional[str] = rule.fields[0] if len(rule.fields) == 1 else None
        observed: Any = row[field] if field is not None and field in row.index else None
        issue: str = self._describe(rule, row)
        expected: Any = None
        field_values: dict[str, Any] = {}
        name: str = ""

        if rule.archetype in (RuleArchetype.DOMAIN_IN, RuleArchetype.REFERENCE_EXISTS):
            expected = rule.assertion.value if isinstance(rule.assertion, Comparison) else None
        elif rule.archetype == RuleArchetype.NOT_NULL:
            expected = "non-null"

        for name in rule.fields:
            if name in row.index:
                field_values[name] = row[name]

        return Finding(
            dimension=rule.dama_dimension,
            table=rule.table,
            record_id=self.schema.record_key(row.to_dict()),
            field=field if field is not None else ",".join(rule.fields),
            rule_id=rule.rule_id,
            issue=issue,
            severity=rule.severity,
            confidence=1.0,
            observed_value=observed,
            expected=expected,
            metadata={"field_values": field_values},
        )

    def _describe(self, rule: RuleSpec, row: pd.Series) -> str:
        """Produce a short, human-readable description of the violation."""
        field: str = rule.fields[0] if rule.fields else ""

        if rule.archetype == RuleArchetype.NOT_NULL:
            return f"mandatory field {field} is empty"
        if rule.archetype in (RuleArchetype.DOMAIN_IN, RuleArchetype.REFERENCE_EXISTS):
            return f"{field}={row[field]!r} is outside the permitted domain"
        if rule.archetype == RuleArchetype.CROSS_FIELD:
            return f"cross-field rule '{rule.name}' not satisfied"
        return f"rule '{rule.name}' violated"


def execute_ruleset(
    rules: list[RuleSpec],
    frames: dict[str, pd.DataFrame],
    schemas: dict[str, TableSchema],
) -> list[Finding]:
    """Run a mixed-table rule set against the matching frames.

    Rules are dispatched to the executor for their own table; rules whose table
    has no frame or schema are skipped.
    """
    findings: list[Finding] = []
    by_table: dict[str, list[RuleSpec]] = {}
    rule: RuleSpec = None
    table: str = ""
    executor: PandasRuleExecutor = None

    for rule in rules:
        by_table.setdefault(rule.table, []).append(rule)

    for table, table_rules in by_table.items():
        if table not in frames or table not in schemas:
            continue
        executor = PandasRuleExecutor(schemas[table])
        findings.extend(executor.run_rules(table_rules, frames[table]))

    return findings
