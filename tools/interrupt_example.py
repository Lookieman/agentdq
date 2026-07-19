# ---------------------------------------------------------------------------
# tools/interrupt_example.py
# v1.0 | 19-Jul-2026 | Initial creation. A minimal, self-contained LangGraph
#                      interrupt() example - the native human-in-the-loop
#                      primitive AgentDQ deliberately chose AGAINST for the
#                      approval gate (design doc 4.2). Kept to demonstrate the
#                      primitive and the reasoning, not used in the pipeline.
# ---------------------------------------------------------------------------
"""Why this file exists (and why it is NOT the gate).

LangGraph's interrupt() pauses a graph mid-run and waits for human input, then
resumes from a checkpoint. It is the obvious way to build a human-in-the-loop
approval step - and AgentDQ chose against it. The reason is operational, not
aesthetic: the approval of a suggested rule can take days, and interrupt()
requires the checkpoint to survive that entire gap. On the ephemeral filesystem
of the public demo host, and across ordinary restarts, a days-old checkpoint is
a fragile thing and a fresh failure mode.

So AgentDQ uses repository-as-gate instead: the suggestion graph ENDS by writing
drafts, a human approves at leisure through the Streamlit gate, and the
assessment graph BEGINS by reading approved rules. No long-lived checkpoint.

This example is kept so the choice is informed, not naive - it shows the
primitive works, in the one place it fits: a SHORT, same-session pause. Run it
directly to see the interrupt-and-resume cycle.

    python -m tools.interrupt_example
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class ReviewState(TypedDict, total=False):
    candidate: str
    decision: str


def _present_for_review(state: ReviewState) -> ReviewState:
    """Pause and ask a human to approve or reject the candidate. Execution stops
    here until the caller resumes with a decision."""
    decision: str = interrupt({"candidate": state["candidate"], "ask": "approve or reject?"})
    return {"decision": decision}


def _apply_decision(state: ReviewState) -> ReviewState:
    """Trivial downstream step that acts on the human's decision."""
    verdict: str = state.get("decision", "reject")
    return {"decision": verdict}


def build_review_graph() -> Any:
    """A two-node graph that interrupts for review. Needs a checkpointer, since
    interrupt/resume is exactly what checkpoints exist for."""
    graph: StateGraph = StateGraph(ReviewState)
    graph.add_node("review", _present_for_review)
    graph.add_node("apply", _apply_decision)
    graph.add_edge(START, "review")
    graph.add_edge("review", "apply")
    graph.add_edge("apply", END)
    return graph.compile(checkpointer=MemorySaver())


def main() -> None:
    graph: Any = build_review_graph()
    config: dict[str, Any] = {"configurable": {"thread_id": "demo-1"}}
    first: Any = None
    resumed: Any = None

    # First invocation runs until the interrupt, then returns control.
    first = graph.invoke({"candidate": "MATNR must not be null"}, config=config)
    print("paused at review; interrupt payload:")
    print(f"  {first.get('__interrupt__')}")

    # The human decides; we resume the SAME thread from its checkpoint.
    resumed = graph.invoke(Command(resume="reject"), config=config)
    print(f"resumed; final decision: {resumed['decision']}")
    print("\nThis works - for a SHORT pause. AgentDQ's gate can span days, so it")
    print("uses repository-as-gate instead (see this file's docstring).")


if __name__ == "__main__":
    main()
