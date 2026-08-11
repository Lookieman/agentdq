# ---------------------------------------------------------------------------
# src/orchestrator.py
# v1.0 | 19-Jul-2026 | Initial creation. Builds the two LangGraph StateGraphs:
#                      the linear suggestion graph and the assessment graph with
#                      a parallel dimension fan-out. Agents/programs are injected
#                      so the graphs compile and run without an LLM in tests.
# v1.2 | 10-Aug-2026 | Package 4f. The injected scorecard callable takes a third
#                      argument: the per-dimension denominators. Uniqueness is
#                      assessed on one table, so it must not be divided by the
#                      whole run.
# v1.1 | 04-Aug-2026 | Package 4d. Uniqueness moves BEFORE the scorecard, so
#                      its findings reach the score. They could not before.
# ---------------------------------------------------------------------------
"""The two orchestrations.

Suggestion graph (linear):
    profile -> interpret -> suggest -> write_drafts -> END

Assessment graph (parallel fan-out over the three deterministic dimensions):
    load_approved -> { completeness | validity | consistency }
                  -> aggregate -> scorecard -> uniqueness -> remediation -> report

The two graphs are joined by the repository, not by a long-lived checkpoint:
the suggestion graph ENDS by writing drafts; a human approves at the gate; the
assessment graph BEGINS by loading approved rules. That is the repository-as-
gate decision (design doc 4.2); a small interrupt example is kept separately to
show the primitive we chose against.

Everything the nodes need (agents, the artefact builder, the rule loader, the
scorecard function) is injected when the graph is built, so the graphs compile
and run offline with fakes. Node functions live in graph_nodes; this module
only wires them.
"""

from __future__ import annotations

from functools import partial
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from src import graph_nodes as nodes
from src.state import AssessmentState, SuggestionState


def build_suggestion_graph(
    interpreter: Any,
    suggester: Any,
    build_artefact: Callable[[dict[str, Any]], dict[str, Any]],
) -> Any:
    """Compile the suggestion graph. interpreter/suggester are the agents;
    build_artefact serialises candidates into the artefact dict."""
    graph: StateGraph = StateGraph(SuggestionState)

    graph.add_node("profile", nodes.profile_node)
    graph.add_node("interpret", partial(nodes.interpret_node, interpreter=interpreter))
    graph.add_node("suggest", partial(nodes.suggest_node, suggester=suggester))
    graph.add_node("write_drafts", partial(nodes.write_drafts_node, build_artefact=build_artefact))

    graph.add_edge(START, "profile")
    graph.add_edge("profile", "interpret")
    graph.add_edge("interpret", "suggest")
    graph.add_edge("suggest", "write_drafts")
    graph.add_edge("write_drafts", END)
    return graph.compile()


def build_assessment_graph(
    completeness_agent: Any,
    validity_agent: Any,
    consistency_agent: Any,
    load_rules: Callable[[dict[str, Any]], list[Any]],
    compute_scorecard: Callable[[list[Any], dict[str, Any], dict[str, Any]], Any],  # v1.2
) -> Any:
    """Compile the assessment graph with the three dimension agents fanning out
    in parallel. load_rules yields the approved rules from state; compute_
    scorecard turns merged findings + frames + per-dimension denominators into a
    scorecard. The third argument arrived in v1.2 (Package 4f): Uniqueness
    states its own denominator and it has to reach the score."""
    graph: StateGraph = StateGraph(AssessmentState)

    graph.add_node("load_approved", partial(nodes.load_approved_node, load_rules=load_rules))
    graph.add_node("completeness", partial(nodes.completeness_node, agent=completeness_agent))
    graph.add_node("validity", partial(nodes.validity_node, agent=validity_agent))
    graph.add_node("consistency", partial(nodes.consistency_node, agent=consistency_agent))
    graph.add_node("aggregate", nodes.aggregate_node)
    graph.add_node("scorecard", partial(nodes.scorecard_node, compute=compute_scorecard))
    graph.add_node("uniqueness", nodes.uniqueness_node)
    graph.add_node("remediation", nodes.remediation_node)
    graph.add_node("report", nodes.report_node)

    graph.add_edge(START, "load_approved")

    # Fan out: the three dimension agents run in parallel after the rules load.
    graph.add_edge("load_approved", "completeness")
    graph.add_edge("load_approved", "validity")
    graph.add_edge("load_approved", "consistency")

    # Join: aggregate runs once, after all three branches complete.
    graph.add_edge("completeness", "aggregate")
    graph.add_edge("validity", "aggregate")
    graph.add_edge("consistency", "aggregate")

    # Downstream chain. Uniqueness runs BEFORE the scorecard (v1.1): its
    # findings are part of the score, and computing the score first meant they
    # could never reach it.
    graph.add_edge("aggregate", "uniqueness")   # v1.1
    graph.add_edge("uniqueness", "scorecard")   # v1.1
    graph.add_edge("scorecard", "remediation")  # v1.1
    graph.add_edge("remediation", "report")
    graph.add_edge("report", END)
    return graph.compile()
