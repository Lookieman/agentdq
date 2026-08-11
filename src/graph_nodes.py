# ---------------------------------------------------------------------------
# src/graph_nodes.py
# v1.0 | 19-Jul-2026 | Initial creation. Node functions for the suggestion and
#                      assessment graphs. Nodes are thin: unpack state, call the
#                      agent's run()/interpret()/suggest(), pack the result back.
#                      No node imports LangGraph; agents import nothing from
#                      here. The cross-agent advisory derivations live here as
#                      pure, testable helpers.
# v1.1 | 04-Aug-2026 | Package 4a. _uniqueness_compare_fields reads the name off
#                      a CompareField (schema v0.4) rather than treating the
#                      entry as a plain string.
# v1.2 | 04-Aug-2026 | Package 4b. Advisories become small dictionaries with six
#                      named keys instead of sentences, so the downstream stage
#                      reads a field rather than searching for words inside a
#                      sentence. Signal suppression is replaced by record
#                      exclusion: a description that failed validity holds its
#                      record out of deduplication, which also stops a large
#                      false cluster of placeholder descriptions forming.
# v1.4 | 10-Aug-2026 | Package 4f. Two gaps closed. scorecard_node now passes
#                      dimension_totals, so the per-dimension denominator built
#                      in 4d finally reaches the scorecard; without it the
#                      Uniqueness score divided its findings by every row of
#                      every loaded table and read far better than the truth.
#                      uniqueness_node also passes the LIST of candidate pairs,
#                      not only their count, because the screen and the
#                      adjudicator (4g) both need the pairs themselves.
# v1.3 | 04-Aug-2026 | Package 4d. The real Uniqueness agent replaces the stub.
#                      Record exclusion now covers the BLOCKING keys as well as
#                      the compare fields: a wrong MTART or MEINS puts a record
#                      in the wrong block, so its duplicate is never considered
#                      and it would otherwise be reported as unique.
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

from typing import Any

from src.agents.base import AgentResult
from src.agents.uniqueness import UniquenessAgent  # v1.3
from src.agents.uniqueness_settings import (  # v1.2
    build_advisory,
    describe_advisory,
)
from src.contracts import AdvisoryAction  # v1.2

# thresholds for the advisory derivations (presentation dials; the real
# calibration of anything like these lands in Package 5)
SPARSE_POPULATION_PCT: float = 90.0
EXCLUSION_MIN_VIOLATIONS: int = 1  # v1.2
# How far the match bands move when a compare field is thinly populated. This
# number is STATED, not calibrated: nothing has yet measured what shift is
# right. A shift that varied with how sparse the field is would look more
# careful and would be invented. Package 5 measures it.
BAND_SHIFT_SPARSE: float = 0.05  # v1.2
# The table deduplication is performed ON. MAKT holds one description per
# material per language, so a MAKT duplicate is a key violation the profiler
# already reports; MAKT gives evidence about MARA rather than being a subject
# in its own right. MARC is assumed clean.
UNIQUENESS_SUBJECT_TABLE: str = "MARA"  # v1.2


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
    advisories: dict[str, list[dict[str, Any]]] = derive_threshold_advisory(state)  # v1.2
    return {
        "findings": list(result.findings),
        "agent_results": [_agent_summary(result)],
        "upstream_advisories": advisories,
    }


def validity_node(state: dict[str, Any], agent: Any) -> dict[str, Any]:
    """Validity fan-out branch. Emits a RECORD-EXCLUSION advisory: if a
    uniqueness compare field has validity findings, tell uniqueness to hold
    those records out of deduplication."""
    result: AgentResult = _run_dimension(state, agent)
    advisories: dict[str, list[dict[str, Any]]] = derive_exclusion_advisory(state, result)  # v1.2
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


def dimension_totals(agent_results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:  # v1.4
    """Collect the denominators the agents state for themselves.

    Most agents check every row of every loaded table, so the whole-run row
    count is the right denominator and they say nothing. Uniqueness checks ONE
    table and holds records back, so it reports records_assessed and
    records_excluded, and those numbers must reach the scorecard. Divided by the
    whole run instead, a real duplicate problem shrinks to near-invisibility and
    a record that was never compared counts as a clean one.

    A pure function over the agent results, so it is testable without a graph.
    """
    totals: dict[str, dict[str, int]] = {}
    entry: dict[str, Any] = {}
    name: str = ""

    for entry in agent_results or []:
        if "records_assessed" not in entry:
            continue
        name = str(entry.get("dimension", ""))
        if not name:
            continue
        totals[name] = {
            "assessed": int(entry.get("records_assessed", 0)),
            "excluded": int(entry.get("records_excluded", 0)),
        }
    return totals


def scorecard_node(state: dict[str, Any], compute: Any) -> dict[str, Any]:
    """Compute the scorecard from the merged findings and the frames."""
    totals: dict[str, dict[str, int]] = dimension_totals(state.get("agent_results", []))  # v1.4
    scorecard: Any = compute(state["findings"], state["frames"], totals)  # v1.4
    return {"scorecard": scorecard}


def uniqueness_node(state: dict[str, Any]) -> dict[str, Any]:  # v1.3
    """Run the real Uniqueness agent.

    The node stays thin, as every node does: unpack the state, call run(), pack
    the result. The agent resolves its own settings from the advisories so it
    can also be run and tested on its own.

    data_dir names the dataset, and the agent reads the vector file that was
    built beside it. With no data_dir, or with a file that fails its checks, the
    agent scores with the fuzzy rung alone and says so on every finding.
    """
    advisories: dict[str, list[dict[str, Any]]] = state.get("upstream_advisories", {})
    for_uniqueness: list[dict[str, Any]] = advisories.get("uniqueness", [])
    schemas: dict[str, Any] = state.get("schemas", {})
    frames: dict[str, Any] = state.get("frames", {})
    findings: list[Any] = state.get("findings", [])
    agent: Any = state.get("uniqueness_agent")
    result: Any = None
    summary: dict[str, Any] = {}

    if agent is None:
        agent = UniquenessAgent(
            advisories=for_uniqueness,
            prior_findings=findings,
            data_dir=state.get("data_dir"),
        )
    result = agent.run(frames, schemas, state.get("rules", []))
    # v1.4: the agent's own summary knows how it matched; the AgentResult knows
    # how many records it matched over. A screen needs both in one place.
    summary = dict(agent.summary())  # v1.4
    summary["records_assessed"] = result.records_assessed  # v1.4
    summary["records_excluded"] = result.records_excluded  # v1.4
    return {
        "findings": result.findings,
        "agent_results": [{
            "agent": result.agent,
            "dimension": result.dimension.value,
            "findings": len(result.findings),
            "clusters": len(result.clusters),
            "records_assessed": result.records_assessed,
            "records_excluded": result.records_excluded,
        }],
        "clusters": result.clusters,  # v1.3
        "uniqueness_settings": {  # v1.3
            "resolved": agent.settings,
            "excluded_counts": summary["held_back"],
            "readable": [describe_advisory(entry) for entry in for_uniqueness],
            "summary": summary,
            # v1.4: the pairs themselves, not only how many. The summary keeps
            # the count so no existing reader breaks.
            "candidate_pairs": list(agent.candidate_pairs),  # v1.4
        },
    }


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
        "uniqueness_settings": state.get("uniqueness_settings", {}),  # v1.2
        "scorecard": scorecard,
    }
    return {"report": report}


# ---------------------------------------------------------------------------
# Advisory derivations (pure, testable)
# ---------------------------------------------------------------------------

def _uniqueness_blocking_fields(state: dict[str, Any]) -> list[tuple[str, str]]:  # v1.3
    """Return (table, field) pairs a table's schema uses to BLOCK on.

    A blocking key must agree exactly before two records are compared, so a
    wrong value is not merely a weak signal. It moves the record into a block
    where its true duplicate cannot be, and the record is then reported as
    unique although it was never really checked."""
    schemas: dict[str, Any] = state.get("schemas", {})
    pairs: list[tuple[str, str]] = []
    table_name: str = ""
    schema: Any = None
    key_field: str = ""

    for table_name, schema in schemas.items():
        uniqueness: Any = getattr(schema, "uniqueness", None)
        if uniqueness is None:
            continue
        for key_field in getattr(uniqueness, "blocking_keys", []) or []:
            pairs.append((table_name, key_field))
    return pairs


def _uniqueness_compare_fields(state: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (table, field) pairs a table's schema nominates as uniqueness
    compare fields. A compare field may be written 'MAKT.MAKTX' (other table) or
    'MAKTX' (same table); we take the field part and pair it with the schema's
    own table when the referenced table is not in scope.

    Since schema v0.4 each entry is a CompareField carrying a weight, so the
    name is read from entry.field. The weight plays no part here: an advisory
    is about whether a signal is trustworthy, not how much it counts."""
    schemas: dict[str, Any] = state.get("schemas", {})
    pairs: list[tuple[str, str]] = []
    table_name: str = ""
    schema: Any = None
    entry: Any = None
    parts: list[str] = []
    ref_table: str = ""
    field_name: str = ""

    for table_name, schema in schemas.items():
        uniqueness: Any = getattr(schema, "uniqueness", None)
        if uniqueness is None:
            continue
        for entry in getattr(uniqueness, "compare_fields", []) or []:
            parts = str(getattr(entry, "field", entry)).split(".")  # v1.1
            if len(parts) == 2:
                ref_table, field_name = parts[0], parts[1]
            else:
                ref_table, field_name = table_name, parts[0]
            pairs.append((ref_table, field_name))
    return pairs


def derive_threshold_advisory(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:  # v1.2
    """THRESHOLD MODIFIER: for each uniqueness compare field that is thinly
    populated across the table, ask uniqueness to demand more match evidence.

    This works on the SETTINGS. It says nothing about any one record; it says
    the signal as a whole is weak, so every pair must clear a higher bar."""
    frames: dict[str, Any] = state.get("frames", {})
    advisories: list[dict[str, Any]] = []
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
            advisories.append(build_advisory(  # v1.2
                action=AdvisoryAction.RAISE_THRESHOLD,
                source="Completeness",
                table=table_name,
                field=field_name,
                value=BAND_SHIFT_SPARSE,
                why=f"only {populated_pct:.1f}% populated, so matching on it is unreliable",
            ))
    if advisories:
        return {"uniqueness": advisories}
    return {}


def derive_exclusion_advisory(state: dict[str, Any], result: AgentResult) -> dict[str, list[dict[str, Any]]]:  # v1.2
    """RECORD EXCLUSION: for each uniqueness compare field carrying validity
    findings, ask uniqueness to hold the offending RECORDS out of matching.

    This works on the DATA, not on the settings, and it replaces the earlier
    signal-suppression advisory for a good reason. MARA has ONE compare field,
    so dropping that field would leave nothing to compare and every material
    would score as unique - a silent, perfect, meaningless result.

    Excluding records is also the safer answer on its own merits. A material
    described "XXXX" or "TEST" has no text worth matching on, and, worse, all
    such materials normalise to the same text and score a perfect match against
    each other. Left in, they would form one large cluster of genuinely
    different materials that the survivorship rules would then merge
    automatically. Holding them back removes that whole failure.

    Since v1.3 this covers the BLOCKING keys as well as the compare fields. A
    wrong MTART or MEINS is not a weak signal. It moves the record into a block
    where its true duplicate cannot be, so the record would be reported as
    unique although nothing ever compared it to anything.

    The advisory names the SIGNAL that went bad, not the records. Record keys
    can run to thousands, and they are already in the findings.
    """
    advisories: list[dict[str, Any]] = []
    violations_by_field: dict[str, int] = {}
    finding: Any = None
    table_name: str = ""
    field_name: str = ""
    key: str = ""
    count: int = 0

    for finding in result.findings:
        key = f"{finding.table}.{finding.field}"
        violations_by_field[key] = violations_by_field.get(key, 0) + 1

    for table_name, field_name in (  # v1.3
        _uniqueness_compare_fields(state) + _uniqueness_blocking_fields(state)
    ):
        key = f"{table_name}.{field_name}"
        count = violations_by_field.get(key, 0)
        if count >= EXCLUSION_MIN_VIOLATIONS:
            advisories.append(build_advisory(  # v1.2
                action=AdvisoryAction.EXCLUDE_RECORDS,
                source="Validity",
                table=table_name,
                field=field_name,
                value=None,
                why=f"{count} record(s) hold a description that failed a validity check",
            ))
    if advisories:
        return {"uniqueness": advisories}
    return {}
