# ---------------------------------------------------------------------------
# src/graph_nodes.py
# v1.0 | 19-Jul-2026 | Initial creation. Node functions for the suggestion and
#                      assessment graphs. Nodes are thin: unpack state, call the
#                      agent's run()/interpret()/suggest(), pack the result back.
#                      No node imports LangGraph; agents import nothing from
#                      here. The cross-agent advisory derivations live here as
#                      pure, testable helpers.
# ---------------------------------------------------------------------------
"""Graph nodes - the thin translation layer between graph state and agents.

The agents already expose clean, orchestration-free interfaces
(run(frames, schemas, rules) -> AgentResult; interpret(profile); suggest(...)),
so a node needs no wrapper - only to unpack the three inputs from state, call
the method, and pack the structured result back. That the nodes are this thin
is the payoff of keeping the agents graph-free.

Cross-agent advisories (design doc 4.3) are demonstrated on two edges with
clean timing under a parallel fan-out - both from a dimension agent to the
DOWNSTREAM uniqueness stage:

  * THRESHOLD MODIFIER  (completeness -> uniqueness): a sparsely populated
    compare field makes duplicate detection unreliable, so raise the match
    threshold for that field.
  * SIGNAL SUPPRESSION  (validity -> uniqueness): a compare field with many
    domain violations is untrustworthy as a match signal, so drop it.

Two DIFFERENT mechanisms - tuning a knob versus removing an input - which is
the point: the advisory channel is general, not one hardcoded trick.
"""

from __future__ import annotations

from typing import Any, Optional

from src.agents.base import AgentResult


# thresholds for the advisory derivations (presentation dials; the real
# calibration of anything like these lands in Package 5)
SPARSE_POPULATION_PCT: float = 90.0
SUPPRESSION_MIN_VIOLATIONS: int = 1


# ---------------------------------------------------------------------------
# Suggestion graph nodes
# ---------------------------------------------------------------------------

def profile_node(state: dict[str, Any]) -> dict[str, Any]:
    """Pass-through when a profile is already in state (the CLI profiles first);
    present as a named node so the graph reads as profile -> interpret -> ..."""
    return {"profile": state["profile"]}


def interpret_node(state: dict[str, Any], interpreter: Any) -> dict[str, Any]:
    """Profiling Agent: profile -> TableInterpretation."""
    interpretation: Any = interpreter.interpret(state["profile"])
    return {"interpretation": interpretation}


def suggest_node(state: dict[str, Any], suggester: Any) -> dict[str, Any]:
    """Rule Suggestion Agent: (profile, interpretation) -> candidates."""
    candidates: list[Any] = suggester.suggest(state["profile"], state["interpretation"])
    return {"candidates": candidates}


def write_drafts_node(state: dict[str, Any], build_artefact: Any) -> dict[str, Any]:
    """Terminal node: serialise candidates into the suggestions artefact. The
    graph ENDS here - drafts are handed to the repository, which is the gate
    between the two graphs (repository-as-gate; no long-lived checkpoint)."""
    artefact: dict[str, Any] = build_artefact(state)
    return {"artefact": artefact}


# ---------------------------------------------------------------------------
# Assessment graph nodes
# ---------------------------------------------------------------------------

def load_approved_node(state: dict[str, Any], load_rules: Any) -> dict[str, Any]:
    """Read the approved rules the dimension agents will run. load_rules is
    injected (the repository export in real use, a fake in tests)."""
    approved: list[Any] = load_rules(state)
    return {"approved_rules": approved}


def _run_dimension(state: dict[str, Any], agent: Any) -> AgentResult:
    """Shared body: call an agent's run() with the state's frames/schemas/rules."""
    return agent.run(state["frames"], state["schemas"], state.get("approved_rules", []))


def _agent_summary(result: AgentResult) -> dict[str, Any]:
    return {"agent": result.agent, "rules_run": result.rules_run,
            "findings": len(result.findings)}


def completeness_node(state: dict[str, Any], agent: Any) -> dict[str, Any]:
    """Completeness fan-out branch. Emits a THRESHOLD-MODIFIER advisory: if a
    uniqueness compare field is sparsely populated, tell uniqueness to demand
    more match confidence there."""
    result: AgentResult = _run_dimension(state, agent)
    advisories: dict[str, list[str]] = derive_threshold_advisory(state)
    return {
        "findings": list(result.findings),
        "agent_results": [_agent_summary(result)],
        "upstream_advisories": advisories,
    }


def validity_node(state: dict[str, Any], agent: Any) -> dict[str, Any]:
    """Validity fan-out branch. Emits a SIGNAL-SUPPRESSION advisory: if a
    uniqueness compare field has domain violations, tell uniqueness to drop it
    as a match signal."""
    result: AgentResult = _run_dimension(state, agent)
    advisories: dict[str, list[str]] = derive_suppression_advisory(state, result)
    return {
        "findings": list(result.findings),
        "agent_results": [_agent_summary(result)],
        "upstream_advisories": advisories,
    }


def consistency_node(state: dict[str, Any], agent: Any) -> dict[str, Any]:
    """Consistency fan-out branch. No advisory emitted here."""
    result: AgentResult = _run_dimension(state, agent)
    return {
        "findings": list(result.findings),
        "agent_results": [_agent_summary(result)],
    }


def aggregate_node(state: dict[str, Any]) -> dict[str, Any]:
    """Join point after the fan-out. The reducers have already merged findings,
    agent_results and advisories; this node is where a scorecard is computed."""
    return {}


def scorecard_node(state: dict[str, Any], compute: Any) -> dict[str, Any]:
    """Compute the scorecard from the merged findings and the frames."""
    scorecard: Any = compute(state["findings"], state["frames"])
    return {"scorecard": scorecard}


def uniqueness_node(state: dict[str, Any]) -> dict[str, Any]:
    """STUB for Package 4. Reads the advisories addressed to it and records how
    it WOULD adjust, so the advisory plumbing is exercised end to end now."""
    advisories: dict[str, list[str]] = state.get("upstream_advisories", {})
    for_uniqueness: list[str] = advisories.get("uniqueness", [])
    note: str = ""

    if for_uniqueness:
        note = "uniqueness (stub) would honour: " + "; ".join(for_uniqueness)
    else:
        note = "uniqueness (stub): no advisories"
    return {"agent_results": [{"agent": "Uniqueness Agent (stub)", "note": note}]}


def remediation_node(state: dict[str, Any]) -> dict[str, Any]:
    """STUB for Package 4."""
    return {"remediation": [{"agent": "Remediation Agent (stub)", "actions": []}]}


def report_node(state: dict[str, Any]) -> dict[str, Any]:
    """Assemble the final report object the runner prints or the dashboard reads."""
    scorecard: Any = state.get("scorecard")
    report: dict[str, Any] = {
        "dataset_label": state.get("dataset_label", ""),
        "total_findings": len(state.get("findings", [])),
        "agent_results": state.get("agent_results", []),
        "upstream_advisories": state.get("upstream_advisories", {}),
        "scorecard": scorecard,
    }
    return {"report": report}


# ---------------------------------------------------------------------------
# Advisory derivations (pure, testable)
# ---------------------------------------------------------------------------

def _uniqueness_compare_fields(state: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (table, field) pairs a table's schema nominates as uniqueness
    compare fields. compare_fields may be written 'MAKT.MAKTX' (other table) or
    'MAKTX' (same table); we take the field part and pair it with the schema's
    own table when the referenced table is not in scope."""
    schemas: dict[str, Any] = state.get("schemas", {})
    pairs: list[tuple[str, str]] = []
    table_name: str = ""
    schema: Any = None
    entry: str = ""
    parts: list[str] = []
    ref_table: str = ""
    field_name: str = ""

    for table_name, schema in schemas.items():
        uniqueness: Any = getattr(schema, "uniqueness", None)
        if uniqueness is None:
            continue
        for entry in getattr(uniqueness, "compare_fields", []) or []:
            parts = str(entry).split(".")
            if len(parts) == 2:
                ref_table, field_name = parts[0], parts[1]
            else:
                ref_table, field_name = table_name, parts[0]
            pairs.append((ref_table, field_name))
    return pairs


def derive_threshold_advisory(state: dict[str, Any]) -> dict[str, list[str]]:
    """THRESHOLD MODIFIER: for each uniqueness compare field that is sparsely
    populated in the frames, advise uniqueness to raise its match threshold."""
    frames: dict[str, Any] = state.get("frames", {})
    messages: list[str] = []
    table_name: str = ""
    field_name: str = ""
    frame: Any = None
    populated_pct: float = 0.0

    for table_name, field_name in _uniqueness_compare_fields(state):
        frame = frames.get(table_name)
        if frame is None or field_name not in getattr(frame, "columns", []):
            continue
        populated_pct = 100.0 * float(frame[field_name].notna().mean())
        if populated_pct < SPARSE_POPULATION_PCT:
            messages.append(
                f"raise match threshold for {table_name}.{field_name}: only "
                f"{populated_pct:.1f}% populated, dedup on it is unreliable"
            )
    if messages:
        return {"uniqueness": messages}
    return {}


def derive_suppression_advisory(state: dict[str, Any], result: AgentResult) -> dict[str, list[str]]:
    """SIGNAL SUPPRESSION: for each uniqueness compare field with validity
    findings, advise uniqueness to drop it as a match signal."""
    messages: list[str] = []
    violations_by_field: dict[str, int] = {}
    finding: Any = None
    table_name: str = ""
    field_name: str = ""
    key: str = ""
    count: int = 0

    for finding in result.findings:
        key = f"{finding.table}.{finding.field}"
        violations_by_field[key] = violations_by_field.get(key, 0) + 1

    for table_name, field_name in _uniqueness_compare_fields(state):
        key = f"{table_name}.{field_name}"
        count = violations_by_field.get(key, 0)
        if count >= SUPPRESSION_MIN_VIOLATIONS:
            messages.append(
                f"drop {key} as a match signal: {count} domain violation(s) make "
                f"it unreliable for matching"
            )
    if messages:
        return {"uniqueness": messages}
    return {}
