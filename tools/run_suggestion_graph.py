# ---------------------------------------------------------------------------
# tools/run_suggestion_graph.py
# v1.0 | 19-Jul-2026 | Initial creation. CLI runner for the suggestion graph:
#                      profile -> interpret -> suggest -> write drafts, wired
#                      through LangGraph. Reuses tools/run_suggestion helpers
#                      (configure_lm, component build, artefact writer) so the
#                      graph and linear runners share one code path.
# ---------------------------------------------------------------------------
"""Run the suggestion graph for one table and write the suggestions artefact.

The LangGraph counterpart to tools/run_suggestion.py. Both produce the same
artefact; this one runs it as a graph so the orchestration is uniform across
suggestion and assessment. Configure the LM first (handled here via --model,
loading .env), exactly as the linear runner does.

Run:
    python -m tools.run_suggestion_graph --table MARA --input data/raw \\
        --out artefacts/suggestions_mara.json
"""

from __future__ import annotations

import argparse
from typing import Any

from src.orchestrator import build_suggestion_graph
from tools.run_suggestion import (
    _build_real_components,
    configure_lm,
    run_suggestion,
    write_artefact,
)


def _artefact_builder(dataset_label: str, model_label: str):
    """Return a build_artefact callable for the write_drafts node. Reuses the
    linear runner's serialisation by handing it a trivial suggester that just
    returns the candidates already in state."""
    def build(state: dict[str, Any]) -> dict[str, Any]:
        candidates: list[Any] = state.get("candidates", [])
        passthrough = type("Passthrough", (), {"suggest": staticmethod(lambda p, i: candidates)})()
        identity_interpreter = type("Identity", (), {"interpret": staticmethod(lambda p: state.get("interpretation"))})()
        return run_suggestion(
            state["profile"], identity_interpreter, passthrough,
            dataset_label=dataset_label, model_label=model_label,
        )
    return build


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Run the AgentDQ suggestion graph and write the artefact."
    )
    parser.add_argument("--table", required=True)
    parser.add_argument("--input", required=True, help="directory holding the extracts")
    parser.add_argument("--out", required=True, help="artefact JSON path")
    parser.add_argument("--schema-dir", default="config/schema")
    parser.add_argument("--model", default="openai/gpt-4o-mini",
                        help="DSPy model string, e.g. openai/gpt-4o-mini or ollama_chat/qwen2.5")
    parser.add_argument("--dataset-label", default="")
    args: argparse.Namespace = parser.parse_args()

    resolved_model: str = configure_lm(args.model)

    from src.data.profiler import profile_files  # heavy import kept local

    profiles: dict[str, Any] = profile_files(
        input_dir=args.input, tables=[args.table], pattern="{table}_EX_DATA.xlsx",
        out_dir=None, schema_dir=args.schema_dir,
    )
    profile: dict[str, Any] = profiles[args.table].model_dump()

    interpreter, suggester = _build_real_components(resolved_model)
    graph: Any = build_suggestion_graph(
        interpreter, suggester,
        _artefact_builder(args.dataset_label, resolved_model),
    )
    final: dict[str, Any] = graph.invoke({"table": args.table, "profile": profile})

    target = write_artefact(final["artefact"], args.out)
    print(f"wrote {target} ({final['artefact']['run']['counts']})")


if __name__ == "__main__":
    main()
