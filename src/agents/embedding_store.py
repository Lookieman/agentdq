# ---------------------------------------------------------------------------
# src/agents/embedding_store.py
# v1.0 | 04-Aug-2026 | Package 4d. The embeddings artefact: how it is named, what
#                      it carries, how it is written and how it is read back.
#                      Split out of tools/build_embeddings.py because BOTH the
#                      builder and the Uniqueness agent need this logic, and an
#                      agent must not import from the CLI layer.
# ---------------------------------------------------------------------------
"""One vector file per compare field, and the checks that keep it honest.

A vector is a list of numbers that stands for the meaning of a text. Two texts
that say the same thing in different words give vectors that point in almost the
same direction.

Every artefact carries two independent codes:

    identity code   model, table, field, language, normalisation version.
                    It answers "was this built under the same conditions?"
    content code    the record keys and their normalised text.
                    It answers "was this built from the same data?"

The Uniqueness agent recalculates both before it trusts a file. If either
disagrees, the agent scores with the fuzzy rung alone and records the reason on
every finding, so a partial run can never be mistaken for a full one.

The identity code deliberately EXCLUDES the match bands, the blocking keys and
the field weights. None of those changes one number in a vector. A code that
included them would force a rebuild every time a steward moved a band, and a
rebuild needs the model and the network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.agents.text_normaliser import NORMALISATION_VERSION, normalise_text
from src.data.schema import TableSchema

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




def artefact_path(data_dir: str, source_table: str, field: str) -> Path:
    """Where the vector file for one compare field lives.

    The path holds the dataset, so the baseline vectors, the degraded vectors
    and the real CAL vectors all exist at the same time. One shared folder
    would hold only one set, and a change of dataset would need a rebuild that
    the free tier cannot perform.
    """
    return Path(data_dir) / EMBEDDINGS_DIRNAME / f"{source_table}_{field}.npz"


def load_verified(
    data_dir: str,
    source_table: str,
    field: str,
    keys: list[str],
    texts: list[str],
    model_name: str = DEFAULT_MODEL,
    language: str = "E",
) -> tuple[dict[str, int], np.ndarray, str]:
    """Read a vector file and check it against the data in hand.

    Returns the key-to-row lookup, the vectors, and a reason string. The reason
    is empty when the file was accepted. When it is not empty, the caller must
    score with the fuzzy rung alone and record the reason.

    Three faults are caught here, and each one is reported rather than guessed
    at:

        the file is absent          nobody has built it for this dataset
        the identity code differs   a different model, field or language
        the content code differs    the data moved and the vectors did not
    """
    path: Path = artefact_path(data_dir, source_table, field)
    payload: dict[str, Any] = {}
    expected_identity: str = ""
    expected_content: str = ""
    lookup: dict[str, int] = {}
    position: int = 0
    key: str = ""

    if not path.exists():
        return {}, np.zeros((0, 0), dtype=np.float32), f"no vector file at {path}"
    try:
        payload = read_artefact(path)
    except Exception as error:  # a damaged file must degrade, not crash the run
        return {}, np.zeros((0, 0), dtype=np.float32), f"vector file unreadable: {error}"

    expected_identity = identity_code(model_name, source_table, field, language)
    if payload["metadata"].get("identity_code") != expected_identity:
        return {}, np.zeros((0, 0), dtype=np.float32), (
            "vectors were built under different conditions "
            f"(model, field or language); rebuild {path}"
        )
    expected_content = content_code(keys, texts)
    if payload["metadata"].get("content_code") != expected_content:
        return {}, np.zeros((0, 0), dtype=np.float32), (
            f"vectors are out of date: the data changed since they were built; rebuild {path}"
        )
    for position, key in enumerate(payload["keys"]):
        lookup[key] = position
    return lookup, payload["vectors"], ""
