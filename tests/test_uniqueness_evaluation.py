# ---------------------------------------------------------------------------
# tests/test_uniqueness_evaluation.py
# v1.0 | 04-Aug-2026 | Package 4e. Covers the uniqueness evaluation: twin recall
#                      (with hidden-by-other-defect counted), decoy error rate,
#                      unlabelled joins, and the score spread by strategy.
#                      Also covers the harder perturbations and the decoy
#                      writer. Fully offline.
# ---------------------------------------------------------------------------
"""Offline. No model, no network.

The tests are grouped by the three things Package 4e delivered:

    1. Harder near-copies       the injector's new strategies
    2. Decoy pairs              two DIFFERENT materials, confusable text
    3. Cluster measurement      three numbers, not one, and honest about it
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from src.contracts import (
    ClusterMember,
    ClusterResolution,
    DefectLabel,
    Dimension,
    DuplicateCluster,
    Finding,
    MatchMode,
    Severity,
    SurvivorReason,
)
from src.data.defect_injector import (
    _DECOY_KINDS,
    _PERTURBATIONS,
    _apply_abbrev,
    _apply_unit_symbol,
    _apply_unit_word,
    _apply_word_order,
    _perturb,
)
from src.reporting.uniqueness_eval import (
    _count_unlabelled_joins,
    _score_decoys,
    _score_twins,
    evaluate_uniqueness,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _twin_label(twin: str, source: str, strategy: str) -> DefectLabel:
    return DefectLabel(
        defect_id=f"UNIQ_{twin}", table="MARA", record_key=f"MATNR={twin}",
        dimension=Dimension.UNIQUENESS, field="MAKTX",
        original_value=source, corrupted_value=twin,
        detail={"duplicate_of": source, "strategy": strategy},
    )


def _cluster(cluster_id: str, records: list[tuple[str, float]]) -> DuplicateCluster:
    members: list[ClusterMember] = []
    survivor_key: str = ""
    key: str = ""
    score: float = 0.0

    survivor_key, _ = records[0]
    for key, score in records:
        members.append(ClusterMember(
            record_id=f"MATNR={key}",
            score=score,
            is_survivor=(key == survivor_key),
        ))
    return DuplicateCluster(
        cluster_id=cluster_id, table="MARA",
        survivor_id=f"MATNR={survivor_key}",
        survivor_reason=SurvivorReason.MOST_COMPLETE,
        resolution=ClusterResolution.AUTOMATIC,
        members=members, mode=MatchMode.FULL,
    )


def _finding(record_id: str, score: float) -> Finding:
    return Finding(
        dimension=Dimension.UNIQUENESS, table="MARA",
        record_id=record_id, field=None, rule_id="UNIQ_MARA_DUPLICATE",
        issue=f"duplicate at {score}",
        severity=Severity.HIGH,
        metadata={"score": score, "cluster_id": "CL-0001"},
    )


# ---------------------------------------------------------------------------
# The harder near-copies
# ---------------------------------------------------------------------------

def test_every_strategy_is_registered_in_the_perturbations_list():
    # Both directions matter: a strategy in the code but not the list would
    # never run; a name in the list but not the code would crash on selection.
    expected = {"upper", "trail_space", "punct", "swap",
                "abbrev", "word_order", "unit_word", "unit_symbol"}
    assert set(_PERTURBATIONS) == expected


def test_abbrev_shortens_a_long_form():
    result = _apply_abbrev("Pump Precision Stainless Steel 123", np.random.default_rng(0))
    assert "ss" in result.lower()
    assert result != "Pump Precision Stainless Steel 123"


def test_abbrev_expands_a_short_form_when_no_long_form_is_present():
    result = _apply_abbrev("Bearing SS 42", np.random.default_rng(0))
    assert "stainless steel" in result.lower()


def test_word_order_moves_a_word_but_keeps_the_size_at_the_end():
    result = _apply_word_order("Pump Precision Steel 123", np.random.default_rng(0))
    assert result.endswith("123")
    assert result != "Pump Precision Steel 123"


def test_unit_word_shortens_inches_to_in():
    result = _apply_unit_word("Shaft Steel 5 inches", np.random.default_rng(0))
    assert "5 in" in result and "inches" not in result


def test_unit_symbol_uses_the_double_quote():
    result = _apply_unit_symbol("Shaft Steel 5 inches", np.random.default_rng(0))
    assert '"' in result


def test_perturb_reports_which_strategy_it_used():
    # The strategy name is what the score-spread report groups by. If it were
    # lost between the injector and the label, the report would collapse to
    # "unknown".
    text, strategy = _perturb("Pump Precision Steel 123", np.random.default_rng(0))
    assert strategy in _PERTURBATIONS
    assert text != "" or strategy == "trail_space"


def test_perturb_leaves_an_empty_string_alone():
    text, strategy = _perturb("", np.random.default_rng(0))
    assert text == ""
    assert strategy in _PERTURBATIONS


# ---------------------------------------------------------------------------
# The decoy kinds
# ---------------------------------------------------------------------------

def test_every_decoy_kind_produces_two_visibly_different_texts():
    # A decoy that reads the same both sides is a duplicate, not a decoy.
    for kind, left, right in _DECOY_KINDS:
        assert left != right, f"{kind}: left and right are identical"
        assert left.strip() != ""
        assert right.strip() != ""


def test_the_decoy_kinds_cover_the_six_agreed_categories():
    kinds = {kind for kind, _, _ in _DECOY_KINDS}
    assert kinds == {
        "part_number", "size_number", "size_code",
        "grade_word", "grade_code", "direction",
    }


# ---------------------------------------------------------------------------
# Twin recall
# ---------------------------------------------------------------------------

def test_a_twin_that_joins_its_source_counts_as_recall():
    labels = [_twin_label("900000001", "100001", "upper")]
    clusters = [_cluster("CL-0001", [("100001", 1.0), ("900000001", 1.0)])]
    result = _score_twins(labels, clusters)
    assert result.matched == 1
    assert result.recall_pct == 100.0


def test_a_twin_in_a_different_cluster_from_its_source_is_a_miss():
    labels = [_twin_label("900000001", "100001", "upper")]
    clusters = [
        _cluster("CL-0001", [("100001", 1.0), ("100002", 0.94)]),
        _cluster("CL-0002", [("900000001", 1.0), ("100003", 0.94)]),
    ]
    result = _score_twins(labels, clusters)
    assert result.matched == 0
    assert result.recall_pct == 0.0


def test_recall_is_broken_out_by_the_strategy_that_made_the_twin():
    # This is what Package 5 will read to decide where the bands belong.
    labels = [
        _twin_label("900000001", "100001", "upper"),
        _twin_label("900000002", "100002", "abbrev"),
        _twin_label("900000003", "100003", "abbrev"),
    ]
    clusters = [
        _cluster("CL-0001", [("100001", 1.0), ("900000001", 1.0)]),
        _cluster("CL-0002", [("100002", 1.0), ("900000002", 0.85)]),
    ]
    result = _score_twins(labels, clusters)
    assert result.by_strategy["upper"] == {"total": 1, "matched": 1, "hidden": 0}
    assert result.by_strategy["abbrev"] == {"total": 2, "matched": 1, "hidden": 0}


def test_a_twin_whose_blocking_key_was_later_corrupted_is_counted_as_hidden():
    # This is the case the exclusion rule from Package 4b creates: a validity
    # injector may corrupt MTART or MEINS on the twin after uniqueness ran, so
    # the twin lands in the wrong block and no matcher could have caught it.
    # Reporting it separately is honest.
    labels = [
        _twin_label("900000001", "100001", "upper"),
        _twin_label("900000002", "100002", "upper"),
    ]
    clusters = [_cluster("CL-0001", [("100001", 1.0), ("900000001", 1.0)])]
    frames = {"MARA": pd.DataFrame([
        {"MATNR": "100001", "MTART": "HALB", "MEINS": "EA"},
        {"MATNR": "900000001", "MTART": "HALB", "MEINS": "EA"},
        {"MATNR": "100002", "MTART": "HALB", "MEINS": "EA"},
        {"MATNR": "900000002", "MTART": "ZZ",   "MEINS": "EA"},  # broken block
    ])}
    result = _score_twins(labels, clusters, frames=frames, blocking_keys=["MTART", "MEINS"])
    assert result.total_twins == 2
    assert result.hidden_by_other_defect == 1
    assert result.matchable == 1
    assert result.matched == 1
    assert result.recall_pct == 50.0
    assert result.recall_on_matchable_pct == 100.0


# ---------------------------------------------------------------------------
# Decoy error rate: the headline precision
# ---------------------------------------------------------------------------

def test_a_decoy_the_agent_joined_counts_as_a_wrong_join():
    decoys = [{"kind": "part_number", "left_matnr": "100001",
               "right_matnr": "100002",
               "left_maktx": "Bearing 6203 Deep Groove",
               "right_maktx": "Bearing 6204 Deep Groove",
               "block": {}}]
    clusters = [_cluster("CL-0001", [("100001", 1.0), ("100002", 0.96)])]
    result = _score_decoys(decoys, clusters)
    assert result.wrongly_joined == 1
    assert result.error_rate_pct == 100.0


def test_a_decoy_the_agent_left_apart_is_correct():
    decoys = [{"kind": "part_number", "left_matnr": "100001",
               "right_matnr": "100002",
               "left_maktx": "Bearing 6203", "right_maktx": "Bearing 6204",
               "block": {}}]
    clusters = []
    result = _score_decoys(decoys, clusters)
    assert result.wrongly_joined == 0
    assert result.error_rate_pct == 0.0


def test_the_error_rate_is_broken_out_by_kind():
    decoys = [
        {"kind": "part_number", "left_matnr": "100001",
         "right_matnr": "100002", "left_maktx": "a", "right_maktx": "b", "block": {}},
        {"kind": "direction",   "left_matnr": "100003",
         "right_matnr": "100004", "left_maktx": "a", "right_maktx": "b", "block": {}},
    ]
    clusters = [_cluster("CL-0001", [("100001", 1.0), ("100002", 0.96)])]
    result = _score_decoys(decoys, clusters)
    assert result.by_kind["part_number"] == {"total": 1, "wrongly_joined": 1}
    assert result.by_kind["direction"] == {"total": 1, "wrongly_joined": 0}


# ---------------------------------------------------------------------------
# Unlabelled joins: a count, honestly named
# ---------------------------------------------------------------------------

def test_a_pair_the_agent_joined_that_no_label_knows_about_is_unlabelled():
    labels = [_twin_label("900000001", "100001", "upper")]
    decoys: list[dict] = []
    # Cluster 1 is the labelled twin pair. Cluster 2 is an unlabelled pair.
    clusters = [
        _cluster("CL-0001", [("100001", 1.0), ("900000001", 1.0)]),
        _cluster("CL-0002", [("100050", 1.0), ("100051", 0.95)]),
    ]
    assert _count_unlabelled_joins(labels, decoys, clusters) == 1


def test_a_decoy_pair_the_agent_joined_is_labelled_not_unlabelled():
    # A decoy is a known pair. Wrongly joining it is a decoy error, not an
    # unlabelled join. Reporting it in both places would double-count.
    labels: list[DefectLabel] = []
    decoys = [{"kind": "part_number", "left_matnr": "100001",
               "right_matnr": "100002",
               "left_maktx": "a", "right_maktx": "b", "block": {}}]
    clusters = [_cluster("CL-0001", [("100001", 1.0), ("100002", 0.96)])]
    assert _count_unlabelled_joins(labels, decoys, clusters) == 0


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

def test_end_to_end_evaluation_writes_all_three_numbers(tmp_path):
    labels_df = pd.DataFrame([
        json.loads(_twin_label("900000001", "100001", "upper").model_dump_json()),
        json.loads(_twin_label("900000002", "100002", "abbrev").model_dump_json()),
    ])
    labels_df.to_parquet(tmp_path / "ground_truth.parquet", index=False)
    (tmp_path / "decoys.json").write_text(json.dumps([
        {"kind": "part_number", "left_matnr": "100010",
         "right_matnr": "100011",
         "left_maktx": "Bearing 6203", "right_maktx": "Bearing 6204",
         "block": {}}
    ]))

    clusters = [
        _cluster("CL-0001", [("100001", 1.0), ("900000001", 1.0)]),   # twin caught
        _cluster("CL-0002", [("100010", 1.0), ("100011", 0.98)]),      # decoy wrong
    ]
    findings = [
        _finding("MATNR=900000001", 1.0),
        _finding("MATNR=100011", 0.98),
    ]

    evaluation = evaluate_uniqueness(
        clusters=clusters, findings=findings,
        labels_path=tmp_path / "ground_truth.parquet",
        decoys_path=tmp_path / "decoys.json",
        score_spread={"duplicate": 2, "uncertain": 0, "below": 100},
    )
    assert evaluation.twin_recall.matched == 1
    assert evaluation.twin_recall.recall_pct == 50.0
    assert evaluation.decoy_result.wrongly_joined == 1
    assert evaluation.decoy_result.error_rate_pct == 100.0
    assert evaluation.unlabelled_joins == 0


def test_a_missing_decoys_file_is_treated_as_no_decoys(tmp_path):
    # Not every dataset carries decoys (the real CAL extract, for instance).
    labels_df = pd.DataFrame([json.loads(
        _twin_label("900000001", "100001", "upper").model_dump_json())])
    labels_df.to_parquet(tmp_path / "ground_truth.parquet", index=False)
    evaluation = evaluate_uniqueness(
        clusters=[], findings=[],
        labels_path=tmp_path / "ground_truth.parquet",
        decoys_path=tmp_path / "decoys.json",
    )
    assert evaluation.decoy_result.total_decoys == 0
    assert evaluation.decoy_result.error_rate_pct == 0.0
