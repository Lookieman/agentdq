# ---------------------------------------------------------------------------
# tools/build_embeddings.py
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
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.agents.text_normaliser import NORMALISATION_VERSION, normalise_text
from src.data.schema import COMPARE_LANGUAGE, TableSchema, load_schemas
from src.reporting.assessment import load_frames

DEFAULT_MODEL: str = "all-MiniLM-L6-v2"
DEFAULT_BATCH_SIZE: int = 256
EMBEDDINGS_DIRNAME: str = "embeddings"
# The column that carries the language key in an SAP text table.
LANGUAGE_FIELD: str = "SPRAS"


def identity_code(model_name: str, table: str, field: str, language: str) -> str:
    """A short code for the conditions a vector file was built under.

    It covers only what changes the numbers: the model, the source column, the
    language, and the normalisation rules. It does NOT cover the match bands or
    the blocking keys, because those change no vector.
    """
    payload: str = ""

    payload = json.dumps(
        {
            "model": model_name,
            "table": table,
            "field": field,
            "language": language,
            "normalisation": NORMALISATION_VERSION,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def content_code(keys: list[str], texts: list[str]) -> str:
    """A short code for the data a vector file was built from.

    The matcher recalculates this from the frame it is about to score. A
    mismatch means the data moved and the vectors did not, so the matcher uses
    the fuzzy rung alone and records the reason.
    """
    digest = hashlib.sha256()
    pairs: list[str] = []
    index: int = 0

    for index in range(len(keys)):
        pairs.append(f"{keys[index]}\u0000{texts[index]}")
    for line in sorted(pairs):
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()[:12]


def subject_tables(schemas: dict[str, TableSchema]) -> list[str]:
    """Tables that are deduplicated, that is, tables with compare fields.

    MARA has a uniqueness block with compare fields, so MARA is a subject. MAKT
    supplies the text MARA is compared on, and MARC is assumed clean, so neither
    is a subject.
    """
    names: list[str] = []
    table_name: str = ""
    schema: TableSchema = None

    for table_name, schema in sorted(schemas.items()):
        if schema.uniqueness and schema.uniqueness.compare_fields:
            names.append(table_name)
    return names


def resolve_compare_field(subject: str, entry_field: str) -> tuple[str, str]:
    """Split a compare field into the table it lives in and its column.

    'MAKT.MAKTX' names another table. 'NORMT' names a column of the subject.
    """
    parts: list[str] = []

    parts = entry_field.split(".")
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return subject, parts[0].strip()


def collect_texts(
    frame: pd.DataFrame,
    subject_schema: TableSchema,
    field: str,
    language: str,
) -> tuple[list[str], list[str], int, int]:
    """Read one column and return the keys and the normalised text.

    The key is the SUBJECT's primary key, not the source table's. MAKT is keyed
    on MATNR and SPRAS, but the matcher works on MARA rows, so a MAKT vector is
    filed under its MATNR alone. The language is fixed, so that key is
    unambiguous.

    Rows with no usable text are dropped here. Their absence is meaningful: the
    matcher holds such a record out of deduplication rather than scoring it
    against everything and calling it unique.
    """
    keys: list[str] = []
    texts: list[str] = []
    working: pd.DataFrame = None
    key_fields: list[str] = list(subject_schema.primary_key)
    missing: list[str] = []
    key_field: str = ""
    row_count: int = 0
    empty_count: int = 0
    text: str = ""
    row: Any = None

    for key_field in key_fields:
        if key_field not in frame.columns:
            missing.append(key_field)
    if missing:
        raise ValueError(
            f"cannot key the vectors: {', '.join(missing)} is not a column of the "
            f"source table. The subject's primary key must exist in the table that "
            f"holds its compare field."
        )
    working = frame
    if LANGUAGE_FIELD in working.columns:
        working = working[working[LANGUAGE_FIELD].astype(str).str.strip() == language]
    row_count = int(len(working))
    for row in working.to_dict(orient="records"):
        text = normalise_text(row.get(field))
        if not text:
            empty_count += 1
            continue
        keys.append("|".join(f"{key_field}={row.get(key_field)}" for key_field in key_fields))
        texts.append(text)
    return keys, texts, row_count, empty_count


def encode_texts(texts: list[str], model_name: str, batch_size: int, encoder: Any = None) -> np.ndarray:
    """Turn the texts into unit-length vectors.

    The vectors are stored already at unit length, so the matcher's similarity
    is one multiply-and-add instead of a division on every pair. The number is
    identical either way, calculated once instead of millions of times.

    encoder is passed in by the tests. When it is None the real model loads,
    which needs the network on the first run.
    """
    model: Any = encoder
    vectors: np.ndarray = None
    lengths: np.ndarray = None

    if model is None:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(model_name)
    vectors = np.asarray(model.encode(texts, batch_size=batch_size), dtype=np.float32)
    if vectors.ndim != 2:
        raise ValueError(f"the encoder returned shape {vectors.shape}, expected two dimensions")
    if len(vectors) != len(texts):
        raise ValueError(
            f"the encoder returned {len(vectors)} vectors for {len(texts)} texts"
        )
    lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
    lengths[lengths == 0.0] = 1.0
    return (vectors / lengths).astype(np.float32)


def write_artefact(
    out_path: Path,
    keys: list[str],
    vectors: np.ndarray,
    metadata: dict[str, Any],
) -> None:
    """Write one .npz holding the keys, the vectors and the metadata."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_path,
        keys=np.array(keys, dtype=object),
        vectors=vectors,
        metadata=np.array(json.dumps(metadata, sort_keys=True), dtype=object),
    )


def read_artefact(path: Path) -> dict[str, Any]:
    """Read one .npz back. Used by the matcher and by the tests."""
    payload: Any = None

    payload = np.load(path, allow_pickle=True)
    return {
        "keys": [str(key) for key in payload["keys"].tolist()],
        "vectors": payload["vectors"],
        "metadata": json.loads(str(payload["metadata"].item())),
    }


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
