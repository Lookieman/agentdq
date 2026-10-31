# ---------------------------------------------------------------------------
# tests/test_adjudicator.py
# v1.0 | 11-Aug-2026 | Package 4g. The adjudicator with a fake program, so the
#                      tests are offline: the cap, the cache, the artefact, and
#                      the deterministic application of verdicts. Includes the
#                      requested test case: a verdicts file made under OTHER
#                      settings applies nothing and says so.
# ---------------------------------------------------------------------------
"""The model is faked; everything around it is real."""

from __future__ import annotations

import pandas as pd

from src.agents.adjudicator import (
    Adjudicator,
    apply_verdicts,
    load_verdicts,
    pair_key,
    save_verdicts,
)
from src.contracts import ClusterResolution, VerdictDecision
from src.data.schema import load_schemas


class _FakeAnswer:
    def __init__(self, decision: str, confidence: float, reasoning: str):
        self.decision = decision
        self.confidence = confidence
        self.reasoning = reasoning


class _FakeProgram:
    """Answers by script: a pair whose texts share a first word is a duplicate."""

    def __init__(self):
        self.calls: int = 0

    def __call__(self, description_a: str, description_b: str, **kwargs):
        self.calls += 1
        same = description_a.split()[:1] == description_b.split()[:1]
        if same:
            return _FakeAnswer("duplicate", 0.9, "The two describe one part.")
        return _FakeAnswer("not_duplicate", 0.85, "The part numbers differ.")


def _pairs() -> list[dict]:
    return [
        {"table": "MARA", "left_id": "MATNR=A", "right_id": "MATNR=B",
         "score": 0.88, "blocking_values": {"MTART": "HALB", "MEINS": "EA"}},
        {"table": "MARA", "left_id": "MATNR=C", "right_id": "MATNR=D",
         "score": 0.91, "blocking_values": {"MTART": "HALB", "MEINS": "EA"}},
        {"table": "MARA", "left_id": "MATNR=E", "right_id": "MATNR=F",
         "score": 0.81, "blocking_values": {"MTART": "HALB", "MEINS": "EA"}},
    ]


TEXTS: dict[str, str] = {
    "MATNR=A": "hex bolt m8", "MATNR=B": "hex bolt m8 steel",
    "MATNR=C": "bearing 6203", "MATNR=D": "washer 6204",
    "MATNR=E": "pump a100", "MATNR=F": "pump a100 refurbished",
}


def _judge(cap: int = 500, artefact=None):
    program = _FakeProgram()
    adjudicator = Adjudicator(program=program, model_label="fake/model", cap=cap)
    result = adjudicator.judge(
        pairs=_pairs(), texts=TEXTS, fingerprint="fp-1",
        block_context={}, artefact=artefact,
    )
    return adjudicator, program, result


# ---------------------------------------------------------------------------
# judge(): the cap and the cache
# ---------------------------------------------------------------------------

def test_every_open_pair_is_judged_under_the_cap():
    adjudicator, program, artefact = _judge()
    assert program.calls == 3
    assert artefact["meta"]["judged_total"] == 3
    assert artefact["meta"]["fingerprint"] == "fp-1"


def test_the_cap_spends_on_the_highest_scores_first():
    # A pair near the duplicate band is the most likely real duplicate, so a
    # capped run judges it first. Scores here: 0.91, 0.88, 0.81.
    adjudicator, program, artefact = _judge(cap=1)
    assert program.calls == 1
    judged = list(artefact["verdicts"].values())
    assert judged[0]["left_id"] == "MATNR=C"


def test_a_judged_pair_costs_nothing_on_a_re_run():
    # The artefact is the cache: an interrupted run continues, and a finished
    # run re-runs for free.
    _, first_program, artefact = _judge()
    _, second_program, artefact = _judge(artefact=artefact)
    assert first_program.calls == 3
    assert second_program.calls == 0


def test_the_artefact_survives_a_save_and_load(tmp_path):
    _, _, artefact = _judge()
    save_verdicts(tmp_path, artefact)
    loaded = load_verdicts(tmp_path)
    assert loaded["meta"]["judged_total"] == 3
    assert loaded["verdicts"] == artefact["verdicts"]


def test_a_missing_artefact_is_an_empty_one(tmp_path):
    assert load_verdicts(tmp_path) == {"meta": {}, "verdicts": {}}


# ---------------------------------------------------------------------------
# apply_verdicts(): deterministic, offline
# ---------------------------------------------------------------------------

def _apply(fingerprint: str = "fp-1"):
    _, _, artefact = _judge()
    schemas = load_schemas("config/schema", ["MARA"])
    frame = pd.DataFrame([
        {"MATNR": "A", "MTART": "HALB", "MEINS": "EA"},
        {"MATNR": "B", "MTART": "HALB", "MEINS": "EA", "MATKL": "001"},
        {"MATNR": "C", "MTART": "HALB", "MEINS": "EA"},
        {"MATNR": "D", "MTART": "HALB", "MEINS": "EA"},
        {"MATNR": "E", "MTART": "HALB", "MEINS": "EA"},
        {"MATNR": "F", "MTART": "HALB", "MEINS": "EA"},
    ])
    return apply_verdicts(
        candidate_pairs=_pairs(), verdict_artefact=artefact,
        fingerprint=fingerprint, texts=TEXTS,
        frame=frame, schema=schemas["MARA"], match_mode="full",
    )


def test_a_confirmed_pair_becomes_a_cluster_a_person_still_decides():
    applied = _apply()
    assert applied["confirmed"] == 2   # A/B and E/F share a first word
    assert applied["rejected"] == 1    # C/D differ
    for cluster in applied["clusters"]:
        assert cluster.resolution is ClusterResolution.NEEDS_STEWARD
        assert cluster.cluster_id.startswith("CL-LLM-")


def test_the_finding_carries_the_models_confidence_not_certainty():
    applied = _apply()
    for finding in applied["findings"]:
        assert finding.confidence == 0.9
        assert finding.metadata["source"] == "llm_adjudicator"


def test_a_rejected_pair_leaves_the_open_list_and_is_kept():
    applied = _apply()
    open_ids = {(pair["left_id"], pair["right_id"]) for pair in applied["open_pairs"]}
    assert ("MATNR=C", "MATNR=D") not in open_ids
    decisions = {(v.left_id, v.right_id): v.decision for v in applied["verdicts"]}
    assert decisions[("MATNR=C", "MATNR=D")] is VerdictDecision.NOT_DUPLICATE


def test_the_more_complete_record_is_the_survivor():
    # B carries one more populated field than A, so B is the record to keep.
    applied = _apply()
    by_pair = {frozenset({c.members[0].record_id, c.members[1].record_id}): c
               for c in applied["clusters"]}
    cluster = by_pair[frozenset({"MATNR=A", "MATNR=B"})]
    assert cluster.survivor_id == "MATNR=B"


def test_stale_verdicts_apply_nothing_and_say_so():
    # THE REQUESTED TEST CASE (4g Q3): a verdicts file made under OTHER
    # settings must not touch the run. Nothing applies, every pair stays
    # open, and the stale flag is raised so the screen can say why.
    applied = _apply(fingerprint="fp-OTHER")
    assert applied["stale"] is True
    assert applied["confirmed"] == 0
    assert applied["rejected"] == 0
    assert applied["clusters"] == []
    assert applied["findings"] == []
    assert len(applied["open_pairs"]) == 3


def test_pair_key_ignores_member_order():
    assert pair_key("MATNR=A", "MATNR=B", "x", "y") == pair_key("MATNR=B", "MATNR=A", "y", "x")
