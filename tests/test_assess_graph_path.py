# ---------------------------------------------------------------------------
# tests/test_assess_graph_path.py
# v1.0 | 10-Aug-2026 | Package 4f. assess() runs the assessment graph, so the
#                      dashboard and the console cannot drift apart again. Also
#                      guards the two defects this step fixed: the scored
#                      dimension list and the per-dimension denominator.
# ---------------------------------------------------------------------------
"""One execution path, checked.

Before Package 4f the dashboard ran three dimensions through a loop and the
graph ran four. The two agreed by coincidence, because both called the same
three agents, and they parted company the moment Uniqueness existed. These
tests hold the paths together.

They are offline. No vector file is built, so the matcher runs on the fuzzy rung
alone, which is enough to prove the wiring: the clusters, the settings and the
denominator all have to arrive whichever rungs ran.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.graph_nodes import dimension_totals
from src.reporting.assessment import (
    LABEL_EVALUATED_DIMENSIONS,
    SCORED_DIMENSIONS,
    assess,
    no_checks_warning,
)

SCHEMA_DIR: str = "config/schema"
RULES_DIR: str = "config/rules"
TABLES: list[str] = ["MARA", "MARC", "MAKT"]
PROFILE_DIR: str = "data/profiles"

PROFILES_AVAILABLE: bool = Path(PROFILE_DIR).exists()
RULES_AVAILABLE: bool = Path(RULES_DIR).exists()


# ---------------------------------------------------------------------------
# The denominator helper, on its own
# ---------------------------------------------------------------------------

def test_an_agent_that_states_its_own_denominator_is_collected():
    results = [
        {"agent": "Completeness Agent", "rules_run": 12, "findings": 40},
        {"agent": "Uniqueness Agent", "dimension": "Uniqueness",
         "findings": 30, "records_assessed": 2440, "records_excluded": 192},
    ]
    assert dimension_totals(results) == {
        "Uniqueness": {"assessed": 2440, "excluded": 192}
    }


def test_an_agent_that_states_nothing_keeps_the_whole_run_denominator():
    # The rule-backed agents check every row of every loaded table, so silence
    # is the right answer for them.
    results = [{"agent": "Validity Agent", "rules_run": 20, "findings": 5}]
    assert dimension_totals(results) == {}


def test_a_denominator_with_no_dimension_name_is_ignored():
    results = [{"agent": "Mystery Agent", "records_assessed": 100}]
    assert dimension_totals(results) == {}


# ---------------------------------------------------------------------------
# The no-checks guard, now shared
# ---------------------------------------------------------------------------

def test_the_no_checks_guard_fires_when_nothing_ran():
    # 100% with zero findings means "checked nothing", and it reads as
    # "perfect data". The dashboard needs this guard as much as the console.
    assert no_checks_warning(0, 0, "data/approved") is not None
    assert no_checks_warning(12, 0, "config/rules") is not None


def test_the_no_checks_guard_stays_silent_when_checks_ran():
    assert no_checks_warning(37, 14, "config/rules") is None


# ---------------------------------------------------------------------------
# The whole path, on a generated dataset
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def assessed(tmp_path_factory):
    """Generate a small labelled dataset and assess it once."""
    from src.data.defect_injector import _persist, inject_defects
    from src.data.generator import generate_dataset
    from src.data.schema import load_schemas
    from src.rules.rule_loader import load_rules

    schemas = load_schemas(SCHEMA_DIR, TABLES)
    rules = load_rules(RULES_DIR)
    baseline = generate_dataset(SCHEMA_DIR, PROFILE_DIR, TABLES, n_materials=400, seed=42)
    frames, labels, manifest, decoys = inject_defects(
        baseline, schemas, rules, scenario="degraded", seed=42
    )
    out_dir = tmp_path_factory.mktemp("dataset")
    _persist(frames, labels, manifest, decoys, out_dir)
    return assess(str(out_dir), SCHEMA_DIR, RULES_DIR, TABLES, "parquet")


pytestmark = pytest.mark.skipif(
    not (PROFILES_AVAILABLE and RULES_AVAILABLE),
    reason="profiles or rules not available",
)


def test_assess_scores_all_four_dimensions(assessed):
    # The defect this test exists for: the graph ran Uniqueness and then scored
    # three dimensions, so the work never reached the screen.
    assert set(assessed.scorecard.by_dimension) == set(SCORED_DIMENSIONS)
    assert "Uniqueness" in assessed.scorecard.by_dimension


def test_assess_returns_the_clusters_the_agent_built(assessed):
    # The screen reads the clusters rather than reassembling them from findings.
    assert isinstance(assessed.clusters, list)
    for cluster in assessed.clusters:
        assert cluster.survivor_id
        assert cluster.size >= 2


def test_the_uniqueness_denominator_is_not_the_whole_run(assessed):
    # Uniqueness is assessed on MARA alone and holds records back. Divided by
    # every row of every table, a real duplicate problem shrinks to nothing.
    score = assessed.scorecard.by_dimension["Uniqueness"]
    assert score.total_records < assessed.total_records
    assert score.total_records == assessed.uniqueness_settings["summary"]["records_assessed"]


def test_held_back_records_are_reported_not_counted_as_clean(assessed):
    score = assessed.scorecard.by_dimension["Uniqueness"]
    summary = assessed.uniqueness_settings["summary"]
    assert score.records_excluded == summary["records_excluded"]


def test_the_resolved_settings_reach_the_caller(assessed):
    resolved = assessed.uniqueness_settings["resolved"]
    assert resolved["blocking_keys"]
    assert resolved["fuzzy_metric"]
    assert resolved["fingerprint"]


def test_the_candidate_pairs_travel_as_a_list_not_only_a_count(assessed):
    # The adjudicator in Package 4g needs the pairs themselves, and so does the
    # screen; a count alone cannot be looked at.
    pairs = assessed.uniqueness_settings["candidate_pairs"]
    assert isinstance(pairs, list)
    assert len(pairs) == assessed.uniqueness_settings["summary"]["candidate_pairs"]


def test_the_deterministic_dimensions_still_score_perfectly(assessed):
    # The path changed; the answers must not. This is the parity check that
    # says moving onto the graph broke nothing.
    for dimension in LABEL_EVALUATED_DIMENSIONS:
        assert assessed.evaluation[dimension].precision == 1.0
        assert assessed.evaluation[dimension].recall == 1.0


def test_the_cluster_level_evaluation_is_produced_on_labelled_data(assessed):
    assert assessed.uniqueness_evaluation is not None
    assert assessed.uniqueness_evaluation.twin_recall.total_twins > 0


def test_the_rule_source_travels_with_the_result(assessed):
    assert assessed.rules_dir == RULES_DIR
    assert assessed.rules_loaded > 0
    assert assessed.rules_run > 0


# ---------------------------------------------------------------------------
# The pair ceiling: hold the block back, do not end the run (Package 4f fix)
# ---------------------------------------------------------------------------

def test_an_oversized_block_holds_records_back_instead_of_raising(tmp_path):
    # The bug this test exists for: the CLEAN baseline dataset crashed and the
    # DEGRADED one did not, because degraded holds 342 records back and that
    # alone kept its largest block under the old hard-coded 5,000,000 limit.
    # A dataset with eighty healthy blocks and one large one deserves an answer
    # about the eighty.
    from src.data.generator import generate_dataset
    from src.data.schema import load_schemas

    schemas = load_schemas(SCHEMA_DIR, TABLES)
    schemas["MARA"].uniqueness.max_block_pairs = 1_000   # about 45 records
    frames = generate_dataset(SCHEMA_DIR, PROFILE_DIR, TABLES, n_materials=600, seed=7)

    from src.agents.uniqueness import HELD_BLOCK_TOO_LARGE, UniquenessAgent
    agent = UniquenessAgent()
    result = agent.run(frames, schemas, [])
    summary = agent.summary()

    assert summary["oversized_blocks"], "the large block should have been reported"
    assert HELD_BLOCK_TOO_LARGE in summary["held_back"]
    # Held-back records leave the denominator rather than counting as clean.
    assert result.records_excluded >= summary["held_back"][HELD_BLOCK_TOO_LARGE]
    # The rest of the dataset was still assessed.
    assert result.records_assessed > 0


def test_the_ceiling_names_the_block_and_the_pair_count(tmp_path):
    from src.data.generator import generate_dataset
    from src.data.schema import load_schemas
    from src.agents.uniqueness import UniquenessAgent

    schemas = load_schemas(SCHEMA_DIR, TABLES)
    schemas["MARA"].uniqueness.max_block_pairs = 1_000
    frames = generate_dataset(SCHEMA_DIR, PROFILE_DIR, TABLES, n_materials=600, seed=7)
    agent = UniquenessAgent()
    agent.run(frames, schemas, [])
    entry = agent.summary()["oversized_blocks"][0]

    assert set(entry["block"]) == {"MTART", "MEINS"}
    assert entry["pairs"] == entry["records"] * (entry["records"] - 1) // 2
    assert entry["pairs"] > entry["ceiling"]


def test_a_block_under_the_ceiling_is_still_compared():
    from src.data.generator import generate_dataset
    from src.data.schema import load_schemas
    from src.agents.uniqueness import HELD_BLOCK_TOO_LARGE, UniquenessAgent

    schemas = load_schemas(SCHEMA_DIR, TABLES)
    frames = generate_dataset(SCHEMA_DIR, PROFILE_DIR, TABLES, n_materials=600, seed=7)
    agent = UniquenessAgent()
    agent.run(frames, schemas, [])

    assert agent.summary()["oversized_blocks"] == []
    assert HELD_BLOCK_TOO_LARGE not in agent.summary()["held_back"]


def test_the_ceiling_is_a_schema_dial_not_a_hidden_constant():
    from src.data.schema import load_schemas
    schemas = load_schemas(SCHEMA_DIR, TABLES)
    assert schemas["MARA"].uniqueness.max_block_pairs == 20_000_000
