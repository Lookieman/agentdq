# ---------------------------------------------------------------------------
# tools/run_suggestion.py
# v1.0 | 13-Jul-2026 | Initial creation. The batch suggestion runner (design
#                      doc 3.6): profile -> interpret -> suggest -> write a
#                      suggestions artefact the repository ingests and the
#                      dashboard replays. One decision serving three masters:
#                      correct gate architecture, demo-without-live-LLM, and
#                      the Phase 2 scheduled-job shape.
# v1.1 | 13-Jul-2026 | Source per-table config from config/schema (not the
#                      retired config/objects).
# v1.2 | 15-Jul-2026 | Wire LM configuration into the CLI: load .env and call
#                      dspy.configure before the pipeline runs, with a --model
#                      flag. The testable core stays LM-free (programs are still
#                      injectable), so the offline tests are unaffected.
# ---------------------------------------------------------------------------
"""Batch suggestion runner.

Produces the suggestions artefact:

    {
      "run": {"run_id", "at", "table", "dataset", "model", "counts"},
      "candidates": [ <serialised CandidateSuggestion>, ... ]
    }

The core (run_suggestion) takes injectable components so it is fully testable
offline; the CLI wires the real ones and expects the caller to have configured
the language model first, e.g.:

    import dspy
    dspy.configure(lm=dspy.LM("openai/<model>"))     # today
    dspy.configure(lm=dspy.LM("ollama_chat/<model>")) # Package 6

Run:
    python -m tools.run_suggestion --table MARA --input data/raw \\
        --out artefacts/suggestions_mara.json
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.rules.repository import candidate_suggestion_to_dict


def run_suggestion(
    profile: dict[str, Any],
    interpreter: Any,
    suggester: Any,
    dataset_label: str = "",
    model_label: str = "",
) -> dict[str, Any]:
    """The testable core: interpret a profile, suggest, serialise. The
    interpreter and suggester are injected (real ones from the CLI; fakes in
    tests)."""
    interpretation: Any = interpreter.interpret(profile)
    candidates: list[Any] = suggester.suggest(profile, interpretation)
    serialised: list[dict[str, Any]] = [candidate_suggestion_to_dict(c) for c in candidates]
    origin_counts: dict[str, int] = {}
    candidate: dict[str, Any] = {}

    for candidate in serialised:
        origin: str = str(candidate.get("origin", "unknown"))
        origin_counts[origin] = origin_counts.get(origin, 0) + 1

    artefact: dict[str, Any] = {
        "run": {
            "run_id": uuid.uuid4().hex[:12],
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "table": str(profile.get("table", "")),
            "dataset": dataset_label,
            "model": model_label,
            "counts": {"total": len(serialised), **origin_counts},
        },
        "candidates": serialised,
    }
    return artefact


def write_artefact(artefact: dict[str, Any], out_path: str | Path) -> Path:
    target: Path = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(artefact, handle, ensure_ascii=False, indent=2)
    return target


def _build_real_components(model_label: str) -> tuple[Any, Any]:
    """Assemble the real interpreter and suggester. Imported here so the
    testable core stays dependency-light."""
    from src.agents.profile_interpreter import ProfileInterpreter
    from src.agents.rule_suggester import RuleSuggester
    from src.rules.reference_store import ReferenceStore
    from src.rules.rule_bank import RuleBank

    bank = RuleBank.load("config/rule_bank")
    store = ReferenceStore.load("config/reference/manifest.yaml")
    interpreter = ProfileInterpreter(roles_path="config/rule_bank/field_roles.yaml")
    suggester = RuleSuggester(bank=bank, reference_store=store)
    return interpreter, suggester


def configure_lm(model: str) -> str:  # v1.2
    """Load .env and configure the DSPy language model. Returns the resolved
    model string (used as the artefact's model_label if none was given).

    The key is read from the environment, not passed on the command line, so it
    never lands in shell history or the artefact. For OpenAI set OPENAI_API_KEY
    in a .env file at the repo root; for a local model later (Package 6) point
    --model at 'ollama_chat/<name>' and no key is needed."""
    import dspy  # heavy import kept local

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        # python-dotenv is optional; env vars may already be set another way.
        pass

    lm = dspy.LM(model)
    dspy.configure(lm=lm)
    return model


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Run the suggestion pipeline for one table and write the artefact."
    )
    parser.add_argument("--table", required=True)
    parser.add_argument("--input", required=True, help="directory holding the extracts")
    parser.add_argument("--out", required=True, help="artefact JSON path")
    parser.add_argument("--schema-dir", default="config/schema")  # v1.1
    parser.add_argument("--model", default="openai/gpt-4o-mini",
                        help="DSPy model string, e.g. openai/gpt-4o-mini or ollama_chat/qwen2.5")  # v1.2
    parser.add_argument("--dataset-label", default="")
    parser.add_argument("--model-label", default="", help="recorded in the artefact for provenance")
    args: argparse.Namespace = parser.parse_args()

    resolved_model: str = configure_lm(args.model)  # v1.2
    model_label: str = args.model_label or resolved_model  # v1.2

    from src.data.profiler import profile_files  # heavy import kept local

    pattern: str = "{table}_EX_DATA.xlsx"
    profiles: dict[str, Any] = profile_files(
        input_dir=args.input, tables=[args.table], pattern=pattern,
        out_dir=None, schema_dir=args.schema_dir,  # v1.1
    )
    profile: dict[str, Any] = profiles[args.table].model_dump()

    interpreter, suggester = _build_real_components(model_label)  # v1.2
    artefact: dict[str, Any] = run_suggestion(
        profile, interpreter, suggester,
        dataset_label=args.dataset_label, model_label=model_label,  # v1.2
    )
    target: Path = write_artefact(artefact, args.out)
    print(f"wrote {target} ({artefact['run']['counts']})")


if __name__ == "__main__":
    main()
