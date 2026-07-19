# ---------------------------------------------------------------------------
# src/state.py
# v1.0 | 19-Jul-2026 | Initial creation. Typed graph state for the two
#                      LangGraph orchestrations (suggestion and assessment),
#                      with reducers so the parallel dimension fan-out can write
#                      findings, agent results and cross-agent advisories
#                      concurrently without clobbering.
# ---------------------------------------------------------------------------
"""Graph state for the AgentDQ orchestrations.

Two graphs, two states. The suggestion state carries a table's profile through
interpretation to candidates; the assessment state carries approved rules
through the dimension agents to a scorecard.

Reducers matter here. In the assessment graph the three dimension agents fan
out in PARALLEL, so any key several of them write - findings, agent_results,
upstream_advisories - needs a reducer, or LangGraph raises a concurrent-write
error. Keys written by a single node (scorecard, report) need none.

This module imports nothing from LangGraph beyond the typing helpers, and
nothing from the agents; it is pure state definition.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional, TypedDict


def merge_advisories(
    left: Optional[dict[str, list[str]]],
    right: Optional[dict[str, list[str]]],
) -> dict[str, list[str]]:
    """Reducer for upstream_advisories.

    upstream_advisories maps a downstream target (e.g. 'uniqueness') to a list
    of advisory messages. When two parallel producers both advise the same
    target, their lists concatenate rather than one overwriting the other.
    Defensive against either side being None, since a reducer channel may start
    empty.
    """
    merged: dict[str, list[str]] = dict(left or {})
    key: str = ""
    values: list[str] = []

    for key, values in (right or {}).items():
        merged[key] = merged.get(key, []) + list(values)
    return merged


class SuggestionState(TypedDict, total=False):
    """State for the suggestion graph: profile -> interpret -> suggest -> drafts.

    Linear, so no reducers are needed - each node writes its own keys once.
    """

    table: str
    profile: dict[str, Any]
    interpretation: Any            # TableInterpretation (kept loose to avoid import)
    candidates: list[Any]          # list[CandidateSuggestion]
    dataset_label: str
    model_label: str
    artefact: dict[str, Any]


class AssessmentState(TypedDict, total=False):
    """State for the assessment graph: approved rules -> dimension fan-out ->
    aggregate -> (uniqueness, remediation) -> report.

    findings, agent_results and upstream_advisories carry reducers because the
    three dimension nodes write them in parallel. frames and schemas are read
    by the fan-out but written once upstream, so they need none.
    """

    # inputs / single-writer context
    tables: list[str]
    frames: dict[str, Any]          # dict[str, pd.DataFrame]
    schemas: dict[str, Any]         # dict[str, TableSchema]
    approved_rules: list[Any]       # list[RuleSpec]
    dataset_label: str

    # written by the parallel fan-out -> need reducers
    findings: Annotated[list[Any], operator.add]
    agent_results: Annotated[list[dict[str, Any]], operator.add]
    upstream_advisories: Annotated[dict[str, list[str]], merge_advisories]

    # written once, downstream of the fan-out
    scorecard: Any
    remediation: list[dict[str, Any]]
    report: dict[str, Any]
