# ---------------------------------------------------------------------------
# tests/test_uniqueness_agent.py
# v1.0 | 04-Aug-2026 | Package 4d. The matcher: blocking, scoring, chaining,
#                      survivorship, the records held back, and the fuzzy-only
#                      fallback. Fully offline - the vector file used here is
#                      written with a small fake encoder, so no model loads.
# ---------------------------------------------------------------------------
"""Offline. No model, no network.

The tests are grouped by the five stages the agent runs: hold back, block,
score, cluster, survive. Two of them exist because the code was WRONG before
they were written:

    test_a_perfect_fuzzy_match_reaches_the_band_with_no_vectors
        In fuzzy-only mode the semantic share of the weight was not returned to
        the fuzzy rung, so a perfect match scored 0.5 and NO pair could ever
        reach the duplicate band. The agent reported that nothing was a
        duplicate, and said nothing about why.

    test_a_validity_finding_on_another_table_still_holds_the_record_back
        A MAKT finding names its record 'MATNR=...|SPRAS=E'. A MARA record is
        keyed 'MATNR=...'. The two never matched, so every exclusion driven by a
        bad description was lost in silence.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.agents.embedding_store import (
    DEFAULT_MODEL,
    collect_texts,
    content_code,
    identity_code,
    write_artefact,
)
from src.agents.uniqueness import (  # v1.1
    DEFAULT_MAX_BLOCK_PAIRS,
    HELD_BLOCK_TOO_LARGE,
    UniquenessAgent,
)
from src.agents.uniqueness_settings import build_advisory
from src.contracts import (
    AdvisoryAction,
    ClusterResolution,
    Comparison,
    Dimension,
    Finding,
    MatchMode,
    Operator,
    Severity,
    SurvivorReason,
)
from src.data.schema import FieldSpec, TableSchema, UniquenessConfig

MARA_FIELDS: list[tuple[str, bool]] = [
    ("MATNR", True), ("MTART", True), ("MEINS", True),
    ("MATKL", False), ("NTGEW", False), ("NORMT", False),
]


def _schema(bands: dict = None, scope=None) -> TableSchema:
    payload: dict = {
        "blocking_keys": ["MTART", "MEINS"],
        "compare_fields": [{"field": "MAKT.MAKTX", "weight": 1.0}],
    }
    if bands:
        payload["bands"] = bands
    if scope is not None:
        payload["scope"] = scope
    return TableSchema(
        table="MARA",
        primary_key=["MATNR"],
        uniqueness=UniquenessConfig(**payload),
        fields={
            name: FieldSpec(name=name, description=name, mandatory=required)
            for name, required in MARA_FIELDS
        },
    )


def _makt_schema() -> TableSchema:
    return TableSchema(
        table="MAKT",
        primary_key=["MATNR", "SPRAS"],
        fields={
            name: FieldSpec(name=name, description=name, mandatory=True)
            for name in ("MATNR", "SPRAS", "MAKTX")
        },
    )


def _frames(rows: list[dict]) -> dict[str, pd.DataFrame]:
    """rows carry MATNR, MTART, MEINS, MAKTX and any optional fields."""
    mara: list[dict] = []
    makt: list[dict] = []
    row: dict = {}

    for row in rows:
        mara.append({
            "MATNR": row["MATNR"],
            "MTART": row.get("MTART", "HALB"),
            "MEINS": row.get("MEINS", "EA"),
            "MATKL": row.get("MATKL", "001"),
            "NTGEW": row.get("NTGEW", "1,5"),
            "NORMT": row.get("NORMT", "DIN933"),
        })
        makt.append({
            "MATNR": row["MATNR"],
            "SPRAS": row.get("SPRAS", "E"),
            "MAKTX": row.get("MAKTX"),
        })
    return {"MARA": pd.DataFrame(mara), "MAKT": pd.DataFrame(makt)}


def _run(rows: list[dict], schema: TableSchema = None, **kwargs):
    agent = UniquenessAgent(**kwargs)
    result = agent.run(_frames(rows), {"MARA": schema or _schema(), "MAKT": _makt_schema()}, [])
    return agent, result


# ---------------------------------------------------------------------------
# Stage 2: blocking
# ---------------------------------------------------------------------------

def test_records_of_a_different_material_type_are_never_compared():
    agent, result = _run([
        {"MATNR": "A", "MTART": "HALB", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MTART": "FERT", "MAKTX": "Hex Bolt M8"},
    ])
    assert result.clusters == []
    assert agent.summary()["score_spread"] == {"duplicate": 0, "uncertain": 0, "below": 0}


def test_records_with_a_different_unit_of_measure_are_never_compared():
    # This is why MEINS is a blocking key. An each-priced item and a kilo-priced
    # item are different materials, whatever their descriptions say.
    agent, result = _run([
        {"MATNR": "A", "MEINS": "EA", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MEINS": "KG", "MAKTX": "Hex Bolt M8"},
    ])
    assert result.clusters == []


def test_a_block_of_one_record_is_left_alone():
    agent, result = _run([{"MATNR": "A", "MAKTX": "Hex Bolt M8"}])
    assert result.clusters == []
    assert result.records_assessed == 1


# ---------------------------------------------------------------------------
# Stage 3: scoring
# ---------------------------------------------------------------------------

def test_a_perfect_fuzzy_match_reaches_the_band_with_no_vectors():
    # THE BUG THIS TEST EXISTS FOR: the semantic share of the weight must return
    # to the fuzzy rung when no vectors are available. Without that a perfect
    # match scores 0.5, no pair reaches 0.92, and the agent quietly reports that
    # nothing at all is a duplicate.
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "HEX BOLT M8"},
    ])
    assert agent.mode is MatchMode.FUZZY_ONLY
    assert len(result.clusters) == 1
    assert result.clusters[0].members[0].score == 1.0


def test_the_mode_and_its_reason_are_recorded_on_every_finding():
    # A partial run must never look like a full one.
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "HEX BOLT M8"},
    ])
    assert result.findings[0].metadata["mode"] == "fuzzy_only"
    assert agent.summary()["mode_reason"] != ""


def test_the_spread_of_scores_is_reported():
    # The bands were stated, not measured. The spread is how we will find out
    # whether they are right, in Package 4e.
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "HEX BOLT M8"},
        {"MATNR": "C", "MAKTX": "Washer Flat Zinc"},
    ])
    spread = agent.summary()["score_spread"]
    assert spread["duplicate"] == 1
    assert spread["below"] == 2


# ---------------------------------------------------------------------------
# Stage 4: clustering, and the uncertain band
# ---------------------------------------------------------------------------

def test_an_uncertain_pair_is_held_as_a_candidate_and_not_clustered():
    # Nothing has judged this pair yet, so the agent must not assert that the
    # two records are the same. The adjudicator decides in Package 4g.
    agent, result = _run(
        [
            {"MATNR": "A", "MAKTX": "Hex Bolt M8 Stainless Steel"},
            {"MATNR": "B", "MAKTX": "Bolt M8 Stainless"},
        ],
        schema=_schema(bands={"duplicate": 0.92, "review_low": 0.70}),
    )
    assert result.clusters == []
    assert len(agent.candidate_pairs) == 1
    assert agent.candidate_pairs[0]["left_id"] == "MATNR=A"


def test_records_chain_into_one_cluster_through_a_common_record():
    # A matches B, B matches C, A does not match C. All three form one group.
    agent, result = _run(
        [
            {"MATNR": "A", "MAKTX": "Hex Bolt M8 Stainless Steel"},
            {"MATNR": "B", "MAKTX": "Hex Bolt M8 Stainless"},
            {"MATNR": "C", "MAKTX": "Bolt M8 Stainless"},
        ],
        schema=_schema(bands={"duplicate": 0.85, "review_low": 0.70}),
    )
    assert len(result.clusters) == 1
    assert {member.record_id for member in result.clusters[0].members} == {
        "MATNR=A", "MATNR=B", "MATNR=C"
    }


def test_the_weakest_link_shows_a_thin_chain_the_member_scores_hide():
    # A and C score 0.798 against each other and are in the same cluster only
    # because B links them. Without weakest_link a steward would see two decent
    # scores against the survivor and approve a merge of records that do not
    # belong together.
    agent, result = _run(
        [
            {"MATNR": "A", "MAKTX": "Hex Bolt M8 Stainless Steel", "NTGEW": "1,5"},
            {"MATNR": "B", "MAKTX": "Hex Bolt M8 Stainless", "NTGEW": None},
            {"MATNR": "C", "MAKTX": "Bolt M8 Stainless", "NTGEW": None, "NORMT": None},
        ],
        schema=_schema(bands={"duplicate": 0.85, "review_low": 0.70}),
    )
    cluster = result.clusters[0]
    assert cluster.weakest_link < 0.85
    assert cluster.survivor_id == "MATNR=A"


def test_a_member_that_joined_through_a_chain_is_flagged():
    agent, result = _run(
        [
            {"MATNR": "A", "MAKTX": "Hex Bolt M8 Stainless Steel", "NTGEW": "1,5"},
            {"MATNR": "B", "MAKTX": "Hex Bolt M8 Stainless", "NTGEW": None},
            {"MATNR": "C", "MAKTX": "Bolt M8 Stainless", "NTGEW": None, "NORMT": None},
        ],
        schema=_schema(bands={"duplicate": 0.85, "review_low": 0.70}),
    )
    flagged = [m.record_id for m in result.clusters[0].members if m.below_band]
    assert flagged == ["MATNR=C"]


# ---------------------------------------------------------------------------
# Stage 5: who survives
# ---------------------------------------------------------------------------

def test_identical_descriptions_resolve_without_a_person():
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "Hex - Bolt  M8"},
    ])
    cluster = result.clusters[0]
    assert cluster.survivor_reason is SurvivorReason.IDENTICAL
    assert cluster.resolution is ClusterResolution.AUTOMATIC


def test_the_most_complete_record_survives():
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8 Steel", "NTGEW": None, "NORMT": None},
        {"MATNR": "B", "MAKTX": "Hex Bolt M8 Steal", "NTGEW": "1,5", "NORMT": "DIN933"},
    ])
    cluster = result.clusters[0]
    assert cluster.survivor_id == "MATNR=B"
    assert cluster.survivor_reason is SurvivorReason.MOST_COMPLETE
    assert cluster.resolution is ClusterResolution.AUTOMATIC


def test_mandatory_fields_outrank_optional_ones():
    # A record full of optional detail is not more useful than one carrying the
    # fields the business requires.
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8 Steel", "MATKL": None, "NTGEW": "1,5", "NORMT": "DIN933"},
        {"MATNR": "B", "MAKTX": "Hex Bolt M8 Steal", "MATKL": "001", "NTGEW": None, "NORMT": None},
    ])
    members = {m.record_id: m for m in result.clusters[0].members}
    assert members["MATNR=A"].populated_total > members["MATNR=B"].populated_total
    assert members["MATNR=B"].populated_mandatory == members["MATNR=A"].populated_mandatory


def test_a_tie_on_completeness_goes_to_a_steward():
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8 Steel"},
        {"MATNR": "B", "MAKTX": "Hex Bolt M8 Steal"},
    ])
    cluster = result.clusters[0]
    assert cluster.resolution is ClusterResolution.NEEDS_STEWARD
    assert cluster.survivor_reason is SurvivorReason.NONE


def test_the_finding_says_a_steward_must_choose_when_it_is_a_tie():
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8 Steel"},
        {"MATNR": "B", "MAKTX": "Hex Bolt M8 Steal"},
    ])
    assert "a steward must choose" in result.findings[0].issue


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

def test_one_finding_is_raised_for_each_record_that_would_leave():
    # A cluster of three raises two findings. The survivor stays, so it raises
    # none: it is not the record with a problem.
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "HEX BOLT M8"},
        {"MATNR": "C", "MAKTX": "Hex - Bolt  M8"},
    ])
    assert len(result.clusters) == 1
    assert len(result.findings) == 2
    survivor = result.clusters[0].survivor_id
    assert survivor not in {finding.record_id for finding in result.findings}


def test_a_finding_carries_its_cluster_and_the_bands_that_produced_it():
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "HEX BOLT M8"},
    ])
    finding = result.findings[0]
    assert finding.dimension is Dimension.UNIQUENESS
    assert finding.severity is Severity.HIGH
    assert finding.rule_id == "UNIQ_MARA_DUPLICATE"
    assert finding.metadata["cluster_id"] == "CL-0001"
    assert finding.metadata["bands"]["duplicate"] == 0.92
    assert finding.metadata["settings_code"] != ""


# ---------------------------------------------------------------------------
# Stage 1: what takes no part
# ---------------------------------------------------------------------------

def test_a_record_with_no_description_is_held_back_not_called_unique():
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": None},
        {"MATNR": "C", "MAKTX": "   "},
    ])
    assert agent.summary()["held_back"] == {"no_description": 2}
    assert result.records_assessed == 1
    assert result.records_excluded == 2


def test_a_record_with_a_missing_blocking_key_is_held_back():
    # A "missing" block would gather unrelated materials into one group, which
    # is the very false cluster the design works to avoid.
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "Hex Bolt M8", "MTART": None},
    ])
    assert agent.summary()["held_back"] == {"no_blocking_key": 1}
    assert result.clusters == []


def test_a_validity_finding_on_another_table_still_holds_the_record_back():
    # THE BUG THIS TEST EXISTS FOR: a MAKT finding names its record
    # 'MATNR=B|SPRAS=E' while a MARA record is keyed 'MATNR=B'. Before the fix
    # the two never matched and the exclusion was lost without a word.
    advisory = build_advisory(
        action=AdvisoryAction.EXCLUDE_RECORDS, source="Validity",
        table="MAKT", field="MAKTX", why="placeholder description",
    )
    finding = Finding(
        dimension=Dimension.VALIDITY, table="MAKT",
        record_id="MATNR=B|SPRAS=E", field="MAKTX", issue="placeholder",
    )
    agent, result = _run(
        [
            {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
            {"MATNR": "B", "MAKTX": "Hex Bolt M8"},
        ],
        advisories=[advisory], prior_findings=[finding],
    )
    assert agent.held_back == {"MATNR=B": "validity_failed"}
    assert result.clusters == []


def test_a_validity_finding_on_a_blocking_key_holds_the_record_back():
    # A wrong MTART is not a weak signal. It puts the record in a block where
    # its true duplicate cannot be, so the record would be reported as unique
    # although nothing ever compared it to anything.
    advisory = build_advisory(
        action=AdvisoryAction.EXCLUDE_RECORDS, source="Validity",
        table="MARA", field="MTART", why="value is not in the allowed list",
    )
    finding = Finding(
        dimension=Dimension.VALIDITY, table="MARA",
        record_id="MATNR=B", field="MTART", issue="ZZ is not a material type",
    )
    agent, result = _run(
        [
            {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
            {"MATNR": "B", "MAKTX": "Hex Bolt M8", "MTART": "ZZ"},
        ],
        advisories=[advisory], prior_findings=[finding],
    )
    assert agent.held_back == {"MATNR=B": "validity_failed"}


# ---------------------------------------------------------------------------
# Settings the steward and the advisories control
# ---------------------------------------------------------------------------

def test_a_threshold_advisory_makes_the_agent_demand_more_evidence():
    rows = [
        {"MATNR": "A", "MAKTX": "Hex Bolt M8 Steel"},
        {"MATNR": "B", "MAKTX": "Hex Bolt M8 Steal"},
    ]
    without = _run(rows)[1]
    assert len(without.clusters) == 1

    advisory = build_advisory(
        action=AdvisoryAction.RAISE_THRESHOLD, source="Completeness",
        table="MAKT", field="MAKTX", value=0.06, why="only 50.0% populated",
    )
    with_advice = _run(rows, advisories=[advisory])[1]
    assert with_advice.clusters == []


def test_a_scope_filter_limits_which_records_are_compared():
    # scope reuses the rule predicate IR, so no new expression language exists.
    schema = _schema(scope=Comparison(node="cmp", field="MTART", op=Operator.IN, value=["FERT"]))
    agent, result = _run(
        [
            {"MATNR": "A", "MTART": "HALB", "MAKTX": "Hex Bolt M8"},
            {"MATNR": "B", "MTART": "HALB", "MAKTX": "Hex Bolt M8"},
        ],
        schema=schema,
    )
    assert result.records_assessed == 0
    assert result.clusters == []


# ---------------------------------------------------------------------------
# The vector file
# ---------------------------------------------------------------------------

def _write_vectors(tmp_path, rows, width=8):
    """Write a real artefact using a fake encoder, so no model loads."""
    frames = _frames(rows)
    keys, texts, _, _ = collect_texts(frames["MAKT"], _schema(), "MAKTX", "E")
    vectors = np.asarray(
        [[float((sum(bytearray(t.encode())) + i) % 53) + 1.0 for i in range(width)] for t in texts],
        dtype=np.float32,
    )
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    write_artefact(
        tmp_path / "embeddings" / "MAKT_MAKTX.npz", keys, vectors.astype(np.float32),
        {
            "identity_code": identity_code(DEFAULT_MODEL, "MAKT", "MAKTX", "E"),
            "content_code": content_code(keys, texts),
        },
    )
    return frames


def test_a_valid_vector_file_switches_the_run_to_full_mode(tmp_path):
    rows = [
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "HEX BOLT M8"},
    ]
    _write_vectors(tmp_path, rows)
    agent, result = _run(rows, data_dir=str(tmp_path))
    assert agent.mode is MatchMode.FULL
    assert result.findings[0].metadata["mode"] == "full"


def test_a_vector_file_built_from_different_data_is_refused(tmp_path):
    _write_vectors(tmp_path, [
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "HEX BOLT M8"},
    ])
    # The data moved and the vectors did not.
    agent, result = _run(
        [
            {"MATNR": "A", "MAKTX": "Washer Flat"},
            {"MATNR": "B", "MAKTX": "WASHER FLAT"},
        ],
        data_dir=str(tmp_path),
    )
    assert agent.mode is MatchMode.FUZZY_ONLY
    assert "out of date" in agent.mode_reason


def test_a_missing_vector_file_degrades_rather_than_failing(tmp_path):
    agent, result = _run(
        [
            {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
            {"MATNR": "B", "MAKTX": "HEX BOLT M8"},
        ],
        data_dir=str(tmp_path),
    )
    assert agent.mode is MatchMode.FUZZY_ONLY
    assert "no vector file" in agent.mode_reason
    assert len(result.clusters) == 1


# ---------------------------------------------------------------------------
# The guard on block size
# ---------------------------------------------------------------------------

def test_an_oversized_block_is_held_back_and_named(monkeypatch):  # v1.1
    # v1.1 changed the behaviour deliberately. Raising ended the WHOLE
    # assessment over one block, so a dataset with eighty healthy blocks and
    # one large one got no answer at all. The block is now held back, which is
    # the same treatment a record with no description already gets.
    schema = _schema()
    schema.uniqueness.max_block_pairs = 2
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "Hex Bolt M9"},
        {"MATNR": "C", "MAKTX": "Hex Bolt M7"},
    ], schema=schema)
    summary = agent.summary()

    assert summary["oversized_blocks"], "the block should be reported, not silently skipped"
    assert summary["oversized_blocks"][0]["records"] == 3
    assert summary["oversized_blocks"][0]["block"]["MTART"] == "HALB"
    assert summary["held_back"][HELD_BLOCK_TOO_LARGE] == 3


def test_a_held_back_block_leaves_the_denominator(monkeypatch):  # v1.1
    # The whole point. A block nobody compared must not read as a clean one.
    schema = _schema()
    schema.uniqueness.max_block_pairs = 2
    agent, result = _run([
        {"MATNR": "A", "MAKTX": "Hex Bolt M8"},
        {"MATNR": "B", "MAKTX": "Hex Bolt M9"},
        {"MATNR": "C", "MAKTX": "Hex Bolt M7"},
    ], schema=schema)

    assert result.records_assessed == 0
    assert result.records_excluded == 3
    assert result.findings == []


def test_the_ceiling_default_is_set_where_the_design_says():  # v1.1
    assert DEFAULT_MAX_BLOCK_PAIRS == 20_000_000
