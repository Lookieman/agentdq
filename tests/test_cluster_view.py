# ---------------------------------------------------------------------------
# tests/test_cluster_view.py
# v1.1 | 10-Aug-2026 | Package 4f fix. Adds the oversized-block table and the
#                      block ceiling row.
# v1.0 | 10-Aug-2026 | Package 4f. The display shaping for the Duplicates and
#                      Settings tabs. Offline, and with no Streamlit import, so
#                      the tables a reader sees are testable on their own.
# ---------------------------------------------------------------------------
"""What the screen shows, checked without starting the screen."""

from __future__ import annotations

import pandas as pd
import pytest

from src.contracts import (
    ClusterMember,
    ClusterResolution,
    DuplicateCluster,
    MatchMode,
    SurvivorReason,
)
from src.data.schema import load_schemas
from src.reporting.cluster_view import (
    NO_TEXT,
    candidate_pairs_frame,
    cluster_members_frame,
    cluster_overview_frame,
    compare_field_texts,
    held_back_frame,
    match_mode_note,
    oversized_blocks_frame,
    score_spread_frame,
    settings_rows,
)

SCHEMA_DIR: str = "config/schema"


def _cluster(
    cluster_id: str = "CL-0001",
    resolution: ClusterResolution = ClusterResolution.AUTOMATIC,
    mode: MatchMode = MatchMode.FULL,
) -> DuplicateCluster:
    return DuplicateCluster(
        cluster_id=cluster_id,
        table="MARA",
        survivor_id="MATNR=100001",
        survivor_reason=SurvivorReason.MOST_COMPLETE,
        resolution=resolution,
        members=[
            ClusterMember(record_id="MATNR=100001", score=1.0, is_survivor=True,
                          populated_mandatory=8, populated_total=8),
            ClusterMember(record_id="MATNR=900000001", score=0.97,
                          populated_mandatory=6, populated_total=8),
            ClusterMember(record_id="MATNR=900000002", score=0.85, below_band=True,
                          populated_mandatory=5, populated_total=8),
        ],
        weakest_link=0.31,
        mode=mode,
        blocking_values={"MTART": "FERT", "MEINS": "EA"},
    )


# ---------------------------------------------------------------------------
# The cluster list
# ---------------------------------------------------------------------------

def test_the_overview_has_one_row_per_cluster():
    frame = cluster_overview_frame([_cluster("CL-0001"), _cluster("CL-0002")])
    assert len(frame) == 2
    assert list(frame["Cluster"]) == ["CL-0001", "CL-0002"]


def test_the_weakest_link_is_shown_beside_the_size():
    # A cluster of three whose weakest pair scores 0.31 is held together by a
    # chain. It looks as tight as a real cluster until that number is read, so
    # it must be on the list a reader scans first.
    frame = cluster_overview_frame([_cluster()])
    assert frame.loc[0, "Records"] == 3
    assert frame.loc[0, "Weakest link"] == 0.31


def test_the_block_reads_as_a_sentence_not_a_dictionary():
    frame = cluster_overview_frame([_cluster()])
    assert frame.loc[0, "Block"] == "MEINS=EA, MTART=FERT"


def test_no_clusters_gives_an_empty_frame_not_an_error():
    assert cluster_overview_frame([]).empty


# ---------------------------------------------------------------------------
# The members of one cluster
# ---------------------------------------------------------------------------

def test_the_survivor_is_listed_first():
    frame = cluster_members_frame(_cluster())
    assert frame.loc[0, "Record"] == "MATNR=100001"
    assert frame.loc[0, "Keep"] == "yes"


def test_the_other_members_follow_in_score_order():
    frame = cluster_members_frame(_cluster())
    assert list(frame["Record"])[1:] == ["MATNR=900000001", "MATNR=900000002"]


def test_a_chained_member_is_marked_below_band():
    # This is the member a steward should look at hardest: it never matched the
    # survivor directly and joined through another record.
    frame = cluster_members_frame(_cluster())
    assert frame.loc[2, "Below band"] == "yes"


def test_the_description_is_shown_beside_each_member():
    texts = {"MAKT.MAKTX": {
        "MATNR=100001": "Hex Bolt M8 Steel",
        "MATNR=900000001": "HEX BOLT M8 STEEL",
    }}
    frame = cluster_members_frame(_cluster(), texts)
    assert frame.loc[0, "MAKT.MAKTX"] == "Hex Bolt M8 Steel"
    # The third member has no text, and the gap must be visible rather than blank.
    assert frame.loc[2, "MAKT.MAKTX"] == NO_TEXT


# ---------------------------------------------------------------------------
# The uncertain band
# ---------------------------------------------------------------------------

def test_candidate_pairs_show_both_descriptions_side_by_side():
    pairs = [{
        "table": "MARA", "left_id": "MATNR=100010", "right_id": "MATNR=100011",
        "score": 0.88, "blocking_values": {"MTART": "FERT"},
    }]
    texts = {"MAKT.MAKTX": {
        "MATNR=100010": "Bearing 6203",
        "MATNR=100011": "Bearing 6204",
    }}
    frame = candidate_pairs_frame(pairs, texts)
    assert frame.loc[0, "A: MAKT.MAKTX"] == "Bearing 6203"
    assert frame.loc[0, "B: MAKT.MAKTX"] == "Bearing 6204"
    assert frame.loc[0, "Score"] == 0.88


def test_an_empty_uncertain_band_gives_an_empty_frame():
    assert candidate_pairs_frame([]).empty


# ---------------------------------------------------------------------------
# The match mode banner
# ---------------------------------------------------------------------------

def test_a_full_run_reports_ok():
    level, message = match_mode_note({"mode": "full", "mode_reason": ""})
    assert level == "ok"
    assert "meaning" in message


def test_a_fuzzy_only_run_warns_and_names_the_reason():
    # A partial run that looks like a full one is the failure this note exists
    # to prevent, so the reason has to reach the reader.
    level, message = match_mode_note(
        {"mode": "fuzzy_only", "mode_reason": "no vector file was found"}
    )
    assert level == "warn"
    assert "no vector file was found" in message


def test_a_missing_mode_warns_rather_than_staying_silent():
    level, _ = match_mode_note({})
    assert level == "warn"


# ---------------------------------------------------------------------------
# The settings table
# ---------------------------------------------------------------------------

def _resolved() -> dict:
    return {
        "bands": {"steward_duplicate": 0.92, "steward_review_low": 0.80,
                  "shift": 0.05, "duplicate": 0.97, "review_low": 0.85},
        "compare_weights": {"MAKT.MAKTX": 1.0},
        "method_weights": {"fuzzy": 0.5, "semantic": 0.5},
        "fuzzy_metric": "token_sort_ratio",
        "semantic_model": "all-MiniLM-L6-v2",
        "blocking_keys": ["MTART", "MEINS"],
        "fingerprint": "abc123def456",
    }


def test_the_settings_show_what_was_set_and_what_is_in_force():
    # A steward who set 0.92 and sees 0.97 must be able to see WHY, or they
    # will conclude their setting was ignored.
    rows = settings_rows(_resolved(), {"records_assessed": 2440, "held_back_total": 192})
    frame = pd.DataFrame(rows)
    band_row = frame[frame["Setting"] == "Duplicate band"].iloc[0]
    assert band_row["In force"] == "0.97"
    assert "0.92" in band_row["What it means"]
    assert "0.05" in band_row["What it means"]


def test_the_fuzzy_method_is_named_on_screen():
    rows = settings_rows(_resolved(), {})
    frame = pd.DataFrame(rows)
    assert frame[frame["Setting"] == "Fuzzy method"].iloc[0]["In force"] == "token_sort_ratio"


def test_the_records_compared_and_held_back_are_both_shown():
    # A held-back record is not a clean record. It was never compared, so the
    # count belongs beside the denominator.
    rows = settings_rows(_resolved(), {"records_assessed": 2440, "held_back_total": 192})
    frame = pd.DataFrame(rows)
    assert frame[frame["Setting"] == "Records compared"].iloc[0]["In force"] == "2440"
    assert frame[frame["Setting"] == "Records held back"].iloc[0]["In force"] == "192"


def test_empty_settings_do_not_raise():
    assert settings_rows({}, {}) != []


# ---------------------------------------------------------------------------
# The small frames
# ---------------------------------------------------------------------------

def test_the_score_spread_explains_each_outcome():
    frame = score_spread_frame({"score_spread": {"duplicate": 12, "uncertain": 5, "below": 900}})
    assert list(frame["Outcome"]) == ["duplicate", "uncertain", "below"]
    assert frame.loc[1, "Pairs"] == 5
    assert "adjudicator" in frame.loc[1, "What it means"]


def test_held_back_records_are_listed_by_reason():
    frame = held_back_frame({"held_back": {"no usable description": 12, "missing block": 3}})
    assert set(frame["Reason"]) == {"no usable description", "missing block"}
    assert int(frame["Records"].sum()) == 15


# ---------------------------------------------------------------------------
# The descriptions, read from real frames
# ---------------------------------------------------------------------------

def test_the_compare_text_is_the_original_not_the_normalised_form():
    # The matcher scores normalised text. A steward judges the text as it
    # stands in the table, so the screen must show that.
    schemas = load_schemas(SCHEMA_DIR, ["MARA", "MAKT"])
    frames = {
        "MARA": pd.DataFrame([{"MATNR": "100001"}, {"MATNR": "100002"}]),
        "MAKT": pd.DataFrame([
            {"MATNR": "100001", "SPRAS": "E", "MAKTX": "Hex Bolt  M8"},
            {"MATNR": "100001", "SPRAS": "D", "MAKTX": "Sechskantschraube M8"},
            {"MATNR": "100002", "SPRAS": "E", "MAKTX": ""},
        ]),
    }
    texts = compare_field_texts(frames, schemas, "MARA")
    assert texts["MAKT.MAKTX"]["MATNR=100001"] == "Hex Bolt  M8"
    # The German row is filtered out by the language the matcher uses.
    assert "MATNR=100002" not in texts["MAKT.MAKTX"]


def test_a_missing_source_table_is_skipped_rather_than_raising():
    schemas = load_schemas(SCHEMA_DIR, ["MARA", "MAKT"])
    texts = compare_field_texts({"MARA": pd.DataFrame([{"MATNR": "100001"}])}, schemas, "MARA")
    assert texts == {}


# ---------------------------------------------------------------------------
# Blocks that were too large to compare (Package 4f fix)
# ---------------------------------------------------------------------------

def test_an_oversized_block_is_named_not_merely_counted():
    # "Some records were not compared" is useless. A steward needs to know
    # WHICH block went unchecked before the score means anything.
    frame = oversized_blocks_frame({"oversized_blocks": [
        {"block": {"MTART": "HALB", "MEINS": "ST"},
         "records": 3180, "pairs": 5054610, "ceiling": 5000000},
    ]})
    assert frame.loc[0, "Block"] == "MEINS=ST, MTART=HALB"
    assert frame.loc[0, "Records"] == 3180
    assert frame.loc[0, "Pairs needed"] == "5,054,610"


def test_no_oversized_block_gives_an_empty_frame():
    assert oversized_blocks_frame({"oversized_blocks": []}).empty
    assert oversized_blocks_frame({}).empty


def test_the_block_ceiling_is_shown_in_the_settings():
    rows = settings_rows(dict(_resolved(), max_block_pairs=20000000), {})
    frame = pd.DataFrame(rows)
    assert frame[frame["Setting"] == "Block size ceiling"].iloc[0]["In force"] == "20,000,000 pairs"
