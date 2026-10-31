# ---------------------------------------------------------------------------
# tests/test_attribute_veto.py
# v1.0 | 11-Aug-2026 | Package 4g. The attribute veto: extraction, the
#                      both-sides rule, the cap inside the matcher, and the
#                      safety of the injector's twin strategies.
# ---------------------------------------------------------------------------
"""The veto exists to say no cheaply, and only when the evidence disagrees."""

from __future__ import annotations

import pandas as pd

from src.agents.text_normaliser import (
    digit_signature,
    material_signature,
    normalise_text,
    signatures_disagree,
)
from src.agents.uniqueness import UniquenessAgent
from src.data.schema import AttributeVeto, load_schemas

WORDS: list[str] = ["stainless steel", "steel", "brass"]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_digit_groups_come_out_of_mixed_tokens():
    # "m8" holds a digit inside a letter token, and "6203" stands alone. Both
    # must surface, or a changed size code hides inside a word.
    assert digit_signature("hex bolt m8 6203") == frozenset({"8", "6203"})


def test_a_unit_word_twin_keeps_the_same_digit_signature():
    # THE SAFETY CASE: "5 inches" against "5in" is an injected TRUE twin. The
    # digit groups match, so the veto must stay silent and recall must not fall.
    assert digit_signature(normalise_text("5 inches")) == digit_signature(normalise_text("5in"))


def test_longest_material_phrase_wins():
    # "stainless steel" is one material, never also "steel".
    assert material_signature("bolt stainless steel 12", WORDS) == frozenset({"stainless steel"})


def test_a_missing_side_never_vetoes():
    # Absence is not disagreement, for the same reason a record with no
    # description is excluded rather than scored.
    assert signatures_disagree(frozenset({"8"}), frozenset(), frozenset(), frozenset()) == ""
    assert signatures_disagree(frozenset(), frozenset(), frozenset({"steel"}), frozenset()) == ""


def test_disagreement_names_its_reason():
    assert signatures_disagree(frozenset({"6203"}), frozenset({"6204"}),
                               frozenset(), frozenset()) == "digits"
    assert signatures_disagree(frozenset({"12"}), frozenset({"12"}),
                               frozenset({"steel"}), frozenset({"brass"})) == "material"


# ---------------------------------------------------------------------------
# The cap inside the matcher
# ---------------------------------------------------------------------------

def _frames(rows: list[dict]) -> dict[str, pd.DataFrame]:
    mara = []
    makt = []
    for row in rows:
        mara.append({"MATNR": row["MATNR"], "MTART": "HALB", "MEINS": "EA"})
        makt.append({"MATNR": row["MATNR"], "SPRAS": "E", "MAKTX": row["MAKTX"]})
    return {"MARA": pd.DataFrame(mara), "MAKT": pd.DataFrame(makt)}


def _run(rows: list[dict]):
    schemas = load_schemas("config/schema", ["MARA", "MAKT"])
    agent = UniquenessAgent()
    result = agent.run(_frames(rows), schemas, [])
    return agent, result


def test_a_decoy_style_pair_is_capped_out_of_the_band():
    # "Bearing 6203" against "Bearing 6204" scores about 0.93 on the fuzzy
    # rung: inside the duplicate band, a guaranteed false positive. The veto
    # caps it to 0.75, below the review floor, so it costs no model call.
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Bearing 6203"},
        {"MATNR": "B", "MAKTX": "Bearing 6204"},
    ])
    summary = agent.summary()
    assert summary["vetoed_pairs"]["digits"] == 1
    assert summary["score_spread"]["uncertain"] == 0
    assert summary["score_spread"]["duplicate"] == 0
    assert result.findings == []


def test_a_true_twin_with_the_same_digits_still_clusters():
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8 6203"},
        {"MATNR": "B", "MAKTX": "M8 Hex Bolt 6203"},
    ])
    summary = agent.summary()
    assert summary["vetoed_pairs"] == {"digits": 0, "material": 0}
    assert len(result.clusters) == 1


def test_a_material_swap_is_capped():
    # The shared words must be long enough that the raw score clears the cap;
    # a short pair like "Bolt Steel 12" scores 0.69 on its own and needs no
    # veto. This one scores about 0.87 without the veto: uncertain band, a
    # model call. The veto sees steel against brass and caps it first.
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Centrifugal Pump Housing Unit Steel 12"},
        {"MATNR": "B", "MAKTX": "Centrifugal Pump Housing Unit Brass 12"},
    ])
    assert agent.summary()["vetoed_pairs"]["material"] == 1
    assert agent.summary()["score_spread"]["uncertain"] == 0
    assert result.findings == []


def test_the_veto_can_be_switched_off():
    schemas = load_schemas("config/schema", ["MARA", "MAKT"])
    schemas["MARA"].uniqueness.attribute_veto.enabled = False
    agent = UniquenessAgent()
    agent.run(_frames([
        {"MATNR": "A", "MAKTX": "Bearing 6203"},
        {"MATNR": "B", "MAKTX": "Bearing 6204"},
    ]), schemas, [])
    summary = agent.summary()
    assert summary["vetoed_pairs"] == {"digits": 0, "material": 0}
    # With no veto the pair lands where the score puts it: in a band.
    assert summary["score_spread"]["duplicate"] + summary["score_spread"]["uncertain"] == 1


def test_the_dials_are_set_where_the_design_says():
    schemas = load_schemas("config/schema", ["MARA"])
    veto = schemas["MARA"].uniqueness.attribute_veto
    assert veto.enabled is True
    assert veto.cap == 0.75
    assert "stainless steel" in veto.material_words
    assert AttributeVeto().cap == 0.75
