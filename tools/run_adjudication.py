# ---------------------------------------------------------------------------
# tools/run_adjudication.py
# v1.0 | 11-Aug-2026 | Package 4g. The batch adjudication runner: assess a
#                      dataset, judge the uncertain band with a capped number
#                      of model calls, and write verdicts.json beside the
#                      dataset. The dashboard and the graph replay the artefact
#                      and spend nothing.
# ---------------------------------------------------------------------------
"""Judge the uncertain band and keep the verdicts.

Run from the repository root:

    uv run python -m tools.run_adjudication --data data/synthetic/degraded
    uv run python -m tools.run_adjudication --data data/raw --format xlsx --cap 200

The artefact is the cache. A second run skips every pair already judged under
the same settings fingerprint, so a re-run after an interruption continues
where the first run stopped. After the artefact exists, every assessment of
the same dataset applies it: confirmed pairs become clusters and findings, and
rejected pairs leave the open list.
"""

from __future__ import annotations

import argparse
from typing import Any

from src.agents.adjudicator import Adjudicator, load_verdicts, save_verdicts
from src.dspy_modules.lm_config import configure_lm
from src.reporting.assessment import assess


def _pair_context(pairs: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """The block values per pair, keyed the way the adjudicator reads them."""
    context: dict[str, dict[str, str]] = {}
    pair: dict[str, Any] = {}

    for pair in pairs:
        context[f"{pair['left_id']}|{pair['right_id']}"] = {
            key: str(value) for key, value in (pair.get("blocking_values") or {}).items()
        }
    return context


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Judge the uncertain band and write verdicts.json beside the dataset."
    )
    parser.add_argument("--data", required=True, help="dataset directory")
    parser.add_argument("--schema", default="config/schema", help="schema YAML directory")
    parser.add_argument("--rules", default="config/rules", help="rules directory")
    parser.add_argument("--tables", default="MARA,MARC,MAKT", help="comma separated tables")
    parser.add_argument("--format", default="parquet", choices=["parquet", "xlsx"])
    parser.add_argument("--model", default=None, help="model string; default from AGENTDQ_MODEL or gpt-4o-mini")
    parser.add_argument("--cap", type=int, default=500, help="maximum model calls this run")
    args: argparse.Namespace = parser.parse_args()

    model: str = configure_lm(args.model)
    tables: list[str] = [name.strip() for name in args.tables.split(",") if name.strip()]

    print(f"Assessing {args.data} to collect the uncertain band...")
    result = assess(args.data, args.schema, args.rules, tables, args.format)
    settings: dict[str, Any] = result.uniqueness_settings
    pairs: list[dict[str, Any]] = settings.get("candidate_pairs", [])
    fingerprint: str = str(settings.get("resolved", {}).get("fingerprint", ""))
    summary: dict[str, Any] = settings.get("summary", {})

    if not pairs:
        print("The uncertain band is empty. Nothing to judge.")
        return

    print(f"{len(pairs)} open pair(s) in the band; cap {args.cap}; model {model}.")

    import dspy
    from src.dspy_modules.adjudication_signatures import PairAdjudication

    texts: dict[str, str] = {}
    pair: dict[str, Any] = {}
    # The texts the rungs scored: read them back from the agent through a fresh
    # small call is wasteful, so the pairs carry ids and the assess() result's
    # settings carry nothing textual. The adjudicator reads the dataset once.
    from src.agents.embedding_store import subject_tables
    from src.data.schema import load_schemas
    from src.reporting.assessment import load_frames
    from src.reporting.cluster_view import compare_field_texts

    frames = load_frames(args.data, tables, args.format)
    schemas = load_schemas(args.schema, tables)
    subject: str = subject_tables(schemas)[0]
    by_field = compare_field_texts(frames, schemas, subject)
    for field_texts in by_field.values():
        for key, value in field_texts.items():
            texts[key] = f"{texts.get(key, '')} {value}".strip()

    adjudicator = Adjudicator(
        program=dspy.ChainOfThought(PairAdjudication),
        model_label=model,
        cap=args.cap,
    )
    artefact = adjudicator.judge(
        pairs=pairs,
        texts=texts,
        fingerprint=fingerprint,
        block_context=_pair_context(pairs),
        artefact=load_verdicts(args.data),
        match_mode=str(summary.get("mode", "full")),
    )
    path = save_verdicts(args.data, artefact)

    print(f"\nJudged {adjudicator.calls_made} pair(s) this run "
          f"({adjudicator.skipped_cached} already judged, skipped).")
    print(f"Verdicts on file: {artefact['meta']['judged_total']}.")
    print(f"Artefact: {path}")
    print("\nRun the assessment again to apply the verdicts:")
    print(f"  uv run python -m tools.run_assessment --data {args.data} --format {args.format}")


if __name__ == "__main__":
    main()
