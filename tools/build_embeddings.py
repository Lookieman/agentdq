# ---------------------------------------------------------------------------
# tools/build_embeddings.py
# v1.1 | 04-Aug-2026 | Package 4d. Now a thin CLI. The artefact logic moved to
#                      src/agents/embedding_store.py, because the Uniqueness
#                      agent needs the same reading and checking code and an
#                      agent must not import from the CLI layer.
# v1.0 | 04-Aug-2026 | Package 4c. Builds the semantic vectors the Uniqueness
#                      agent reads. Runs in the BATCH layer, never inside the
#                      Streamlit process: the sentence-transformers model is too
#                      heavy for the free tier. One artefact per table and field,
#                      written BESIDE its dataset so vectors from one dataset can
#                      never be used against another.
# ---------------------------------------------------------------------------
"""Turn descriptions into vectors, once, so the matcher does not have to.

A vector is a list of numbers that stands for the meaning of a text. Two texts
that say the same thing in different words give two vectors that point in
almost the same direction. That is how "Hex Bolt M8" and "M8 Hexagon Screw"
score highly although they share few letters.

Run as a module:

    python -m tools.build_embeddings --data data/synthetic/degraded \\
        --format parquet --schema config/schema --tables MARA,MARC,MAKT

The output goes to <data>/embeddings/<TABLE>_<FIELD>.npz. That path holds the
dataset name, so the baseline vectors, the degraded vectors and the real CAL
vectors all exist at the same time and cannot be confused. Both data/raw/ and
data/synthetic/ are already in .gitignore, so the artefacts stay out of git.

Each artefact carries two independent checks:

    identity code   model, table, field, language, normalisation version.
                    It answers "was this built under the same conditions?"
    content code    the record keys and the normalised text.
                    It answers "was this built from the same data?"

The identity code deliberately EXCLUDES the match bands, the blocking keys and
the field weights. None of those change one number in a vector. A code that
included them would force a rebuild every time a steward moved a band, and a
rebuild needs the model and the network.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.agents.embedding_store import (  # v1.1
    DEFAULT_BATCH_SIZE,
    DEFAULT_MODEL,
    EMBEDDINGS_DIRNAME,
    collect_texts,
    content_code,
    encode_texts,
    identity_code,
    resolve_compare_field,
    subject_tables,
    write_artefact,
)
from src.agents.text_normaliser import NORMALISATION_VERSION
from src.data.schema import COMPARE_LANGUAGE, TableSchema, load_schemas
from src.reporting.assessment import load_frames


def build_one(
    subject: str,
    subject_schema: TableSchema,
    source_table: str,
    field: str,
    frame: pd.DataFrame,
    out_dir: Path,
    model_name: str,
    batch_size: int,
    language: str,
    encoder: Any = None,
) -> dict[str, Any]:
    """Build the vector file for one compare field. Returns a summary."""
    keys: list[str] = []
    texts: list[str] = []
    vectors: np.ndarray = None
    metadata: dict[str, Any] = {}
    out_path: Path = out_dir / f"{source_table}_{field}.npz"
    row_count: int = 0
    empty_count: int = 0

    keys, texts, row_count, empty_count = collect_texts(frame, subject_schema, field, language)
    if not texts:
        raise ValueError(
            f"{source_table}.{field} has no usable text in language '{language}'. "
            f"Nothing can be encoded."
        )
    vectors = encode_texts(texts, model_name, batch_size, encoder)
    metadata = {
        "identity_code": identity_code(model_name, source_table, field, language),
        "content_code": content_code(keys, texts),
        "model": model_name,
        "subject_table": subject,
        "source_table": source_table,
        "field": field,
        "language": language,
        "normalisation_version": NORMALISATION_VERSION,
        "unit_length": True,
        "dimensions": int(vectors.shape[1]),
        "vector_count": int(vectors.shape[0]),
        "rows_read": row_count,
        "rows_without_text": empty_count,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_artefact(out_path, keys, vectors, metadata)
    return {"path": str(out_path), **metadata}


def build_all(
    data_dir: str,
    data_format: str,
    schema_dir: str,
    tables: list[str],
    model_name: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    language: str = COMPARE_LANGUAGE,
    encoder: Any = None,
    out_dir: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build one vector file for every compare field named in the schemas."""
    schemas: dict[str, TableSchema] = load_schemas(schema_dir, tables)
    frames: dict[str, pd.DataFrame] = load_frames(data_dir, tables, data_format)
    target_dir: Path = Path(out_dir) if out_dir else Path(data_dir) / EMBEDDINGS_DIRNAME
    summaries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    subject: str = ""
    source_table: str = ""
    field: str = ""
    entry: Any = None

    for subject in subject_tables(schemas):
        for entry in schemas[subject].uniqueness.compare_fields:
            source_table, field = resolve_compare_field(subject, entry.field)
            if (source_table, field) in seen:
                continue
            seen.add((source_table, field))
            if source_table not in frames:
                raise ValueError(
                    f"{subject} compares {entry.field}, but table {source_table} was "
                    f"not loaded. Add it to --tables."
                )
            if field not in frames[source_table].columns:
                raise ValueError(
                    f"{source_table} has no column {field}, named by {subject} as a "
                    f"compare field."
                )
            summaries.append(build_one(
                subject=subject,
                subject_schema=schemas[subject],
                source_table=source_table,
                field=field,
                frame=frames[source_table],
                out_dir=target_dir,
                model_name=model_name,
                batch_size=batch_size,
                language=language,
                encoder=encoder,
            ))
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the semantic vectors for uniqueness matching.")
    parser.add_argument("--data", required=True, help="dataset directory, e.g. data/synthetic/degraded")
    parser.add_argument("--format", default="parquet", choices=["parquet", "xlsx"])
    parser.add_argument("--schema", default="config/schema")
    parser.add_argument("--tables", default="MARA,MARC,MAKT")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--language", default=COMPARE_LANGUAGE)
    parser.add_argument("--out", default=None, help="override the output directory")
    args = parser.parse_args()

    tables: list[str] = [name.strip() for name in args.tables.split(",") if name.strip()]
    summaries: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}

    summaries = build_all(
        data_dir=args.data,
        data_format=args.format,
        schema_dir=args.schema,
        tables=tables,
        model_name=args.model,
        batch_size=args.batch_size,
        language=args.language,
        out_dir=args.out,
    )
    if not summaries:
        print("No compare fields are configured. Nothing was built.")
        return
    print(f"Embeddings built from {args.data}")
    for summary in summaries:
        print(f"\n  {summary['source_table']}.{summary['field']} -> {summary['path']}")
        print(f"    model          : {summary['model']}")
        print(f"    language       : {summary['language']}")
        print(f"    vectors        : {summary['vector_count']} of {summary['dimensions']} numbers")
        print(f"    rows read      : {summary['rows_read']}")
        print(f"    no usable text : {summary['rows_without_text']}")
        print(f"    identity code  : {summary['identity_code']}")
        print(f"    content code   : {summary['content_code']}")


if __name__ == "__main__":
    main()
