# ---------------------------------------------------------------------------
# tests/test_embeddings.py
# v1.0 | 04-Aug-2026 | Package 4c. Covers the shared text normaliser and the
#                      embeddings builder. Fully offline: the encoder is passed
#                      in, so the real model never loads and no network is used.
#                      The tests check the SHAPE of an artefact and its round
#                      trip, never the numbers, because encoding is not
#                      bit-identical across platforms.
# ---------------------------------------------------------------------------
"""Offline. No model, no network, no data files.

Two ideas are under test:

    1. Both scoring rungs must see the SAME text. The normaliser is the one
       place that decides what that text is.
    2. A vector file must say what it was built FROM and what it was built
       UNDER, so the matcher can refuse stale or foreign vectors.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.agents.text_normaliser import (
    NORMALISATION_VERSION,
    is_matchable,
    normalise_text,
)
from src.data.schema import CompareField, TableSchema, UniquenessConfig
from tools import build_embeddings as builder

VECTOR_WIDTH: int = 8


class FakeEncoder:
    """Returns fixed numbers. No model, no network.

    Each text gets a vector whose values come from the characters of the text,
    so two identical texts give identical vectors and two different texts give
    different ones. That is all the builder needs from an encoder.
    """

    def __init__(self, width: int = VECTOR_WIDTH):
        self.width = width
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        rows: list[list[float]] = []
        row: list[float] = []
        text: str = ""
        seed: int = 0
        position: int = 0

        self.calls.append(list(texts))
        for text in texts:
            seed = sum(bytearray(text.encode()))
            row = []
            for position in range(self.width):
                row.append(float((seed + position) % 97) + 1.0)
            rows.append(row)
        return np.asarray(rows, dtype=np.float32)


def _mara_schema() -> TableSchema:
    return TableSchema(
        table="MARA",
        primary_key=["MATNR"],
        uniqueness=UniquenessConfig(
            blocking_keys=["MTART", "MEINS"],
            compare_fields=[CompareField(field="MAKT.MAKTX", weight=1.0)],
        ),
        fields={},
    )


def _makt_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "MATNR": ["000000000000001", "000000000000002", "000000000000003", "000000000000001"],
        "SPRAS": ["E", "E", "E", "D"],
        "MAKTX": ["Hex Bolt M8", "Hex - Bolt  M8", None, "Sechskantschraube"],
    })


# ---------------------------------------------------------------------------
# The normaliser
# ---------------------------------------------------------------------------

def test_case_and_spacing_and_punctuation_all_collapse():
    # These three are exactly what the injector's mild changes produce. All
    # three must return to the source text, so they score a perfect match and
    # test the automatic survivorship case.
    assert normalise_text("Hex Bolt") == "hex bolt"
    assert normalise_text("HEX BOLT") == "hex bolt"
    assert normalise_text("Hex Bolt  ") == "hex bolt"
    assert normalise_text("Hex - Bolt") == "hex bolt"


def test_punctuation_becomes_a_space_and_not_nothing():
    # "hexbolt" would hide a word boundary that both rungs need.
    assert normalise_text("Hex-Bolt") == "hex bolt"


def test_a_swapped_character_still_differs():
    # The injector's swap must remain visible, or the fuzzy rung has nothing
    # to find.
    assert normalise_text("Hxe Bolt") != normalise_text("Hex Bolt")


def test_absent_values_return_an_empty_string():
    assert normalise_text(None) == ""
    assert normalise_text("") == ""
    assert normalise_text("   ") == ""
    assert normalise_text(float("nan")) == ""
    assert normalise_text("nan") == ""


def test_accents_are_removed_so_two_spellings_agree():
    assert normalise_text("Ventil fur Pumpe") == normalise_text("VENTIL FUR PUMPE")
    assert normalise_text("Ventil f\u00fcr Pumpe") == "ventil fur pumpe"


def test_a_placeholder_description_is_not_matchable():
    # A record with no usable text takes no part in deduplication.
    assert is_matchable("Hex Bolt M8") is True
    assert is_matchable("...") is False
    assert is_matchable(None) is False


def test_the_normalisation_version_is_recorded():
    # The version goes into an artefact's identity code, so a change to the
    # rules above marks every existing artefact as out of date.
    assert NORMALISATION_VERSION == "1.0"


# ---------------------------------------------------------------------------
# Identity code and content code
# ---------------------------------------------------------------------------

def test_the_identity_code_changes_with_the_model():
    first = builder.identity_code("all-MiniLM-L6-v2", "MAKT", "MAKTX", "E")
    second = builder.identity_code("some-other-model", "MAKT", "MAKTX", "E")
    assert first != second
    assert len(first) == 12


def test_the_identity_code_changes_with_the_language_and_the_field():
    base = builder.identity_code("all-MiniLM-L6-v2", "MAKT", "MAKTX", "E")
    assert builder.identity_code("all-MiniLM-L6-v2", "MAKT", "MAKTX", "D") != base
    assert builder.identity_code("all-MiniLM-L6-v2", "MAKT", "NORMT", "E") != base


def test_the_content_code_changes_when_the_text_changes():
    base = builder.content_code(["a", "b"], ["hex bolt", "hex nut"])
    assert builder.content_code(["a", "b"], ["hex bolt", "hex screw"]) != base
    assert builder.content_code(["a", "c"], ["hex bolt", "hex nut"]) != base


def test_the_content_code_ignores_the_order_of_the_rows():
    # A frame read in a different order holds the same data. Rebuilding for
    # that reason alone would waste minutes and the network.
    first = builder.content_code(["a", "b"], ["hex bolt", "hex nut"])
    second = builder.content_code(["b", "a"], ["hex nut", "hex bolt"])
    assert first == second


# ---------------------------------------------------------------------------
# Reading the source column
# ---------------------------------------------------------------------------

def test_only_the_chosen_language_is_read():
    keys, texts, rows, empty = builder.collect_texts(
        _makt_frame(), _mara_schema(), "MAKTX", "E"
    )
    assert rows == 3
    assert "sechskantschraube" not in texts


def test_the_key_is_the_subjects_key_not_the_source_tables():
    # MAKT is keyed on MATNR and SPRAS. The matcher works on MARA rows, so a
    # MAKT vector is filed under its MATNR alone.
    keys, texts, rows, empty = builder.collect_texts(
        _makt_frame(), _mara_schema(), "MAKTX", "E"
    )
    assert keys == ["MATNR=000000000000001", "MATNR=000000000000002"]


def test_rows_without_usable_text_are_dropped_and_counted():
    keys, texts, rows, empty = builder.collect_texts(
        _makt_frame(), _mara_schema(), "MAKTX", "E"
    )
    assert empty == 1
    assert len(keys) == 2


def test_a_missing_key_column_raises_with_an_explanation():
    frame = _makt_frame().drop(columns=["MATNR"])
    with pytest.raises(ValueError) as error:
        builder.collect_texts(frame, _mara_schema(), "MAKTX", "E")
    assert "MATNR" in str(error.value)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def test_stored_vectors_are_unit_length():
    # Stored at unit length, the matcher's similarity is one multiply-and-add
    # instead of a division on every pair.
    vectors = builder.encode_texts(["hex bolt", "hex nut"], "fake", 32, FakeEncoder())
    lengths = np.linalg.norm(vectors, axis=1)
    assert np.allclose(lengths, 1.0, atol=1e-5)


def test_an_encoder_returning_the_wrong_count_raises():
    class ShortEncoder:
        def encode(self, texts, batch_size=32):
            return np.zeros((1, VECTOR_WIDTH), dtype=np.float32)

    with pytest.raises(ValueError) as error:
        builder.encode_texts(["a", "b"], "fake", 32, ShortEncoder())
    assert "1 vectors for 2 texts" in str(error.value)


def test_the_encoder_receives_the_normalised_text():
    encoder = FakeEncoder()
    builder.build_one(
        subject="MARA", subject_schema=_mara_schema(), source_table="MAKT",
        field="MAKTX", frame=_makt_frame(), out_dir=Path("/tmp/agentdq-test-enc"),
        model_name="fake", batch_size=32, language="E", encoder=encoder,
    )
    assert encoder.calls[0] == ["hex bolt m8", "hex bolt m8"]


# ---------------------------------------------------------------------------
# The artefact
# ---------------------------------------------------------------------------

def test_an_artefact_round_trips(tmp_path):
    summary = builder.build_one(
        subject="MARA", subject_schema=_mara_schema(), source_table="MAKT",
        field="MAKTX", frame=_makt_frame(), out_dir=tmp_path,
        model_name="fake-model", batch_size=32, language="E", encoder=FakeEncoder(),
    )
    payload = builder.read_artefact(Path(summary["path"]))

    assert payload["keys"] == ["MATNR=000000000000001", "MATNR=000000000000002"]
    assert payload["vectors"].shape == (2, VECTOR_WIDTH)
    assert payload["metadata"]["source_table"] == "MAKT"
    assert payload["metadata"]["subject_table"] == "MARA"
    assert payload["metadata"]["language"] == "E"
    assert payload["metadata"]["unit_length"] is True
    assert payload["metadata"]["rows_without_text"] == 1


def test_the_artefact_is_named_for_its_table_and_field(tmp_path):
    summary = builder.build_one(
        subject="MARA", subject_schema=_mara_schema(), source_table="MAKT",
        field="MAKTX", frame=_makt_frame(), out_dir=tmp_path,
        model_name="fake", batch_size=32, language="E", encoder=FakeEncoder(),
    )
    assert Path(summary["path"]).name == "MAKT_MAKTX.npz"


def test_a_column_with_no_usable_text_raises_rather_than_writing_an_empty_file(tmp_path):
    frame = _makt_frame().copy()
    frame["MAKTX"] = None
    with pytest.raises(ValueError) as error:
        builder.build_one(
            subject="MARA", subject_schema=_mara_schema(), source_table="MAKT",
            field="MAKTX", frame=frame, out_dir=tmp_path,
            model_name="fake", batch_size=32, language="E", encoder=FakeEncoder(),
        )
    assert "no usable text" in str(error.value)


# ---------------------------------------------------------------------------
# Choosing what to build
# ---------------------------------------------------------------------------

def test_only_tables_with_compare_fields_are_subjects():
    schemas = {
        "MARA": _mara_schema(),
        "MAKT": TableSchema(table="MAKT", primary_key=["MATNR", "SPRAS"], fields={}),
        "MARC": TableSchema(table="MARC", primary_key=["MATNR", "WERKS"], fields={}),
    }
    assert builder.subject_tables(schemas) == ["MARA"]


def test_a_compare_field_splits_into_a_table_and_a_column():
    assert builder.resolve_compare_field("MARA", "MAKT.MAKTX") == ("MAKT", "MAKTX")
    assert builder.resolve_compare_field("MARA", "NORMT") == ("MARA", "NORMT")
