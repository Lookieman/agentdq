# ---------------------------------------------------------------------------
# tools/benchmark_embedding_models.py
# v1.0 | 04-Aug-2026 | Package 4f preparation. Runs the same twins-and-decoys
#                      evaluation across four embedding models, so a choice
#                      between them rests on measurement rather than a hunch.
#                      Vectors go to a SEPARATE _bench/ folder so the live
#                      files are never overwritten.
# v1.1 | 04-Aug-2026 | Fix: v1.0 pointed the matcher at
#                      <data>/embeddings/_bench/<model> as if it were a dataset
#                      directory. The matcher's own file locator then looked
#                      for <that>/embeddings/<TABLE>_<FIELD>.npz - one folder
#                      too deep - could not find the file, and fell back to
#                      FUZZY ONLY on every model. Every row came back
#                      identical. v1.1 stages a proper dataset directory per
#                      model so the matcher's own locator finds the vectors,
#                      and REFUSES to report a benchmark row whose mode is
#                      fuzzy_only. A silent fuzzy-only run can never be
#                      mistaken for a real measurement again.
# ---------------------------------------------------------------------------
"""Measure how well each candidate embedding model separates near-duplicates
from decoys on our own data.

The tool answers three questions about each model:

    twin recall           Of the injected twins, how many landed in the same
                          cluster as their source? Higher is better.
    decoy error rate      Of the decoy pairs, how many did the agent wrongly
                          join? Lower is better. This is the headline
                          precision figure.
    uncertain pair count  How many pairs the ladder cannot settle on its own?
                          Lower is better: each of these will need adjudication.

Run as a module:

    python -m tools.benchmark_embedding_models \\
        --data data/synthetic/degraded --format parquet

To include OpenAI text-embedding-3-large, set OPENAI_API_KEY in your .env
file. Without a key, that model is skipped and the tool says so.

The tool writes its vectors to
    <data>/embeddings/_bench/<model_slug>/<TABLE>_<FIELD>.npz
which is a folder the matcher does not read. The live vectors under
<data>/embeddings/ are never touched.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from src.agents.embedding_store import (
    DEFAULT_BATCH_SIZE,
    collect_texts,
    content_code,
    identity_code,
    resolve_compare_field,
    subject_tables,
    write_artefact,
)
from src.agents.text_normaliser import NORMALISATION_VERSION
from src.agents.uniqueness import UniquenessAgent
from src.data.schema import COMPARE_LANGUAGE, TableSchema, load_schemas
from src.reporting.assessment import load_frames
from src.reporting.uniqueness_eval import evaluate_uniqueness

# Every candidate model. The 'kind' field is written into the output table so
# a reader can see at a glance which models were local and which were cloud.
MODELS: list[dict[str, Any]] = [
    {"name": "all-MiniLM-L6-v2",       "kind": "local  (baseline)"},
    {"name": "all-mpnet-base-v2",      "kind": "local  (medium)"},
    {"name": "BAAI/bge-large-en-v1.5", "kind": "local  (large)"},
    {"name": "text-embedding-3-large", "kind": "cloud  (OpenAI)"},
]

OPENAI_KEY_VAR: str = "OPENAI_API_KEY"


def _slug(name: str) -> str:
    """Make a safe folder name out of a model name."""
    return name.replace("/", "-").replace(" ", "-")


def _local_encoder(model_name: str) -> Callable[[list[str]], np.ndarray]:
    """Return a callable that encodes a batch of texts with a local model."""
    from sentence_transformers import SentenceTransformer

    model: Any = SentenceTransformer(model_name)

    def encode(texts: list[str]) -> np.ndarray:
        return np.asarray(model.encode(texts, batch_size=DEFAULT_BATCH_SIZE), dtype=np.float32)

    return encode


def _openai_encoder(model_name: str) -> Optional[Callable[[list[str]], np.ndarray]]:
    """Return a callable that encodes with the OpenAI Embeddings API.

    Returns None (and prints the reason) when no key is present, so the tool
    keeps working offline.
    """
    api_key: str = ""
    client: Any = None

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    api_key = os.getenv(OPENAI_KEY_VAR, "")
    if not api_key:
        print(f"  {model_name} skipped: {OPENAI_KEY_VAR} is not set")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        print(f"  {model_name} skipped: the openai package is not installed")
        return None
    client = OpenAI(api_key=api_key)

    def encode(texts: list[str]) -> np.ndarray:
        response: Any = None
        rows: list[list[float]] = []
        record: Any = None
        batch: list[str] = []
        start: int = 0
        # OpenAI accepts many inputs per request; 256 is a comfortable batch.
        for start in range(0, len(texts), 256):
            batch = texts[start:start + 256]
            response = client.embeddings.create(model=model_name, input=batch)
            for record in response.data:
                rows.append(record.embedding)
        return np.asarray(rows, dtype=np.float32)

    return encode


def _get_encoder(model_name: str) -> Optional[Callable[[list[str]], np.ndarray]]:
    if model_name.startswith("text-embedding-"):
        return _openai_encoder(model_name)
    try:
        return _local_encoder(model_name)
    except Exception as error:
        print(f"  {model_name} skipped: {error}")
        return None


def _bench_dir(data_dir: str, model_name: str) -> Path:
    """The folder each model's vectors live in.

    The matcher expects a DATASET directory whose 'embeddings/' subfolder
    holds the .npz. So each model gets its own dataset directory here, and
    its vectors are placed at <dataset>/embeddings/<TABLE>_<FIELD>.npz.
    """
    return Path(data_dir) / "embeddings" / "_bench" / _slug(model_name)  # v1.1


def _bench_vector_dir(data_dir: str, model_name: str) -> Path:  # v1.1
    """Where the .npz actually goes: the matcher looks inside 'embeddings/'."""
    return _bench_dir(data_dir, model_name) / "embeddings"


def _link_ground_truth(data_dir: str, model_name: str) -> None:  # v1.1
    """Copy the labels and decoys into each per-model dataset directory.

    The evaluator reads ground_truth.parquet and decoys.json from the same
    directory it treats as the dataset. Each model's dataset directory
    inherits those files from the real dataset.
    """
    import shutil
    src_data: Path = Path(data_dir)
    bench: Path = _bench_dir(data_dir, model_name)
    filename: str = ""
    for filename in ("ground_truth.parquet", "decoys.json"):
        if (src_data / filename).exists():
            shutil.copy2(src_data / filename, bench / filename)


def _build_vectors(
    encoder: Callable[[list[str]], np.ndarray],
    frames: dict[str, pd.DataFrame],
    schemas: dict[str, TableSchema],
    model_name: str,
    out_dir: Path,
    language: str,
) -> None:
    """Build one .npz per compare field, under a benchmark-only folder.

    Same shape as tools/build_embeddings.py so the matcher's own reader can
    load them by pointing data_dir at this folder.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    subject: str = ""
    entry: Any = None
    source_table: str = ""
    field: str = ""
    keys: list[str] = []
    texts: list[str] = []
    vectors: np.ndarray = None
    lengths: np.ndarray = None
    seen: set[tuple[str, str]] = set()

    for subject in subject_tables(schemas):
        for entry in schemas[subject].uniqueness.compare_fields:
            source_table, field = resolve_compare_field(subject, entry.field)
            if (source_table, field) in seen:
                continue
            seen.add((source_table, field))
            keys, texts, _, _ = collect_texts(
                frames[source_table], schemas[subject], field, language
            )
            if not texts:
                print(f"    no usable text for {source_table}.{field}, skipping")
                continue
            vectors = encoder(texts)
            lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
            lengths[lengths == 0.0] = 1.0
            vectors = (vectors / lengths).astype(np.float32)
            write_artefact(
                out_dir / f"{source_table}_{field}.npz",
                keys, vectors,
                {
                    "identity_code": identity_code(model_name, source_table, field, language),
                    "content_code": content_code(keys, texts),
                    "model": model_name,
                    "source_table": source_table,
                    "subject_table": subject,
                    "field": field,
                    "language": language,
                    "normalisation_version": NORMALISATION_VERSION,
                    "unit_length": True,
                    "dimensions": int(vectors.shape[1]),
                    "vector_count": int(vectors.shape[0]),
                },
            )


def _evaluate_model(
    frames: dict[str, pd.DataFrame],
    schemas: dict[str, TableSchema],
    data_dir: str,
    model_name: str,
) -> dict[str, Any]:
    """One model, three numbers.

    data_dir here is the ORIGINAL dataset. The agent gets the per-model
    dataset directory - _bench_dir(...) - so it reads that model's vectors,
    and the evaluator reads the ground truth from the same folder (linked
    in by _link_ground_truth).
    """
    agent: UniquenessAgent = None
    result: Any = None
    summary: dict[str, Any] = {}
    evaluation: Any = None
    bench_data: Path = _bench_dir(data_dir, model_name)  # v1.1

    agent = UniquenessAgent(
        data_dir=str(bench_data),  # v1.1
        model_name=model_name,
    )
    result = agent.run(frames, schemas, [])
    summary = agent.summary()
    # v1.1: if the matcher fell back to fuzzy_only, this is NOT a real
    # measurement of the model. Refuse to report a number that would look
    # like a valid comparison.
    if summary.get("mode") == "fuzzy_only":
        raise RuntimeError(
            f"the matcher ran in FUZZY_ONLY mode for {model_name} - "
            f"{summary.get('mode_reason', 'reason not recorded')}. The model was "
            f"never used, so this is not a valid measurement. Fix the setup and "
            f"try again."
        )
    evaluation = evaluate_uniqueness(
        clusters=result.clusters,
        findings=result.findings,
        labels_path=bench_data / "ground_truth.parquet",  # v1.1
        decoys_path=bench_data / "decoys.json",  # v1.1
        score_spread=summary.get("score_spread", {}),
        frames=frames,
        blocking_keys=schemas["MARA"].uniqueness.blocking_keys,
    )
    return {
        "twin_recall": evaluation.twin_recall,
        "decoy_result": evaluation.decoy_result,
        "uncertain_pairs": summary.get("score_spread", {}).get("uncertain", 0),
        "duplicate_pairs": summary.get("score_spread", {}).get("duplicate", 0),
        "cluster_count": len(result.clusters),
        "strategy_scores": evaluation.strategy_scores,
        "mode": summary.get("mode"),
        "mode_reason": summary.get("mode_reason", ""),
    }


def _print_headline(results: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 82)
    print("Model comparison: twins-and-decoys evaluation")
    print("=" * 82)
    header: str = f"{'Model':<32} {'Kind':<20} {'Twin recall':>12} {'Decoy err':>10} {'Uncertain':>10}"
    print(header)
    print("-" * 82)
    entry: dict[str, Any] = {}
    mode: str = ""

    for entry in results:
        if entry.get("skipped"):
            reason: str = entry.get("error", "skipped")
            print(f"{entry['name']:<32} {entry['kind']:<20} {'skipped':>12} {'':>10} {'':>10}")
            print(f"    reason: {reason}")
            continue
        recall_pct: float = entry["result"]["twin_recall"].recall_pct
        recall_match: float = entry["result"]["twin_recall"].recall_on_matchable_pct
        error_pct: float = entry["result"]["decoy_result"].error_rate_pct
        uncertain: int = entry["result"]["uncertain_pairs"]
        mode = entry["result"].get("mode", "?")  # v1.1
        print(
            f"{entry['name']:<32} {entry['kind']:<20} "
            f"{recall_pct:>6.1f}% ({recall_match:>4.1f}%) "
            f"{error_pct:>9.1f}% {uncertain:>10,}"
        )
        print(f"    mode: {mode}")  # v1.1
    print("\n(twin recall shows overall and, in brackets, on the matchable set)")


def _print_by_strategy(results: list[dict[str, Any]]) -> None:
    strategies: list[str] = sorted({
        strategy
        for entry in results
        if not entry.get("skipped")
        for strategy in entry["result"]["twin_recall"].by_strategy
    })
    if not strategies:
        return
    print("\nTwin recall by change strategy (matched of matchable):")
    header_names: list[str] = [entry["name"][:20] for entry in results if not entry.get("skipped")]
    header: str = f"{'strategy':<14}" + "".join(f"{name:>22}" for name in header_names)
    print(header)
    print("-" * len(header))
    entry: dict[str, Any] = {}
    line: str = ""
    counts: dict[str, int] = {}
    strategy: str = ""

    for strategy in strategies:
        line = f"{strategy:<14}"
        for entry in results:
            if entry.get("skipped"):
                continue
            counts = entry["result"]["twin_recall"].by_strategy.get(strategy, {})
            matchable = counts.get("total", 0) - counts.get("hidden", 0)
            line += f"{counts.get('matched', 0):>10}/{matchable:<10} "
        print(line)


def benchmark(data_dir: str, data_format: str, schema_dir: str, tables: list[str],
              language: str = COMPARE_LANGUAGE) -> list[dict[str, Any]]:
    schemas: dict[str, TableSchema] = load_schemas(schema_dir, tables)
    frames: dict[str, pd.DataFrame] = load_frames(data_dir, tables, data_format)
    results: list[dict[str, Any]] = []
    encoder: Optional[Callable[[list[str]], np.ndarray]] = None
    started: float = 0.0
    elapsed: float = 0.0
    model: dict[str, Any] = {}
    result: dict[str, Any] = {}

    for model in MODELS:
        print(f"\n>>> {model['name']} ({model['kind']})")
        encoder = _get_encoder(model["name"])
        if encoder is None:
            results.append({"name": model["name"], "kind": model["kind"], "skipped": True})
            continue
        started = time.time()
        try:
            _bench_vector_dir(data_dir, model["name"]).mkdir(parents=True, exist_ok=True)  # v1.1
            _build_vectors(
                encoder, frames, schemas, model["name"],
                _bench_vector_dir(data_dir, model["name"]), language,  # v1.1
            )
            _link_ground_truth(data_dir, model["name"])  # v1.1
            elapsed = time.time() - started
            print(f"    vectors built in {elapsed:.1f} seconds")
            result = _evaluate_model(frames, schemas, data_dir, model["name"])
            results.append({"name": model["name"], "kind": model["kind"], "result": result})
        except Exception as error:
            print(f"    failed: {error}")
            results.append({"name": model["name"], "kind": model["kind"], "skipped": True,
                            "error": str(error)})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure how well each candidate embedding model separates near-duplicates from decoys."
    )
    parser.add_argument("--data", required=True, help="dataset directory, e.g. data/synthetic/degraded")
    parser.add_argument("--format", default="parquet", choices=["parquet", "xlsx"])
    parser.add_argument("--schema", default="config/schema")
    parser.add_argument("--tables", default="MARA,MARC,MAKT")
    parser.add_argument("--language", default=COMPARE_LANGUAGE)
    args = parser.parse_args()

    tables: list[str] = [name.strip() for name in args.tables.split(",") if name.strip()]
    results: list[dict[str, Any]] = benchmark(
        data_dir=args.data, data_format=args.format, schema_dir=args.schema,
        tables=tables, language=args.language,
    )
    _print_headline(results)
    _print_by_strategy(results)
    print("\nGuidance: pick the model with the HIGHEST twin recall AND the")
    print("LOWEST decoy error rate. Fewer uncertain pairs is a bonus, because it")
    print("means less adjudication work for the language model in Package 4g.\n")
    print("Vectors were written to <dataset>/embeddings/_bench/<model_slug>/ and\n"
          "do NOT overwrite the live vectors under <dataset>/embeddings/.")


if __name__ == "__main__":
    main()
