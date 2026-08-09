# ---------------------------------------------------------------------------
# tests/test_uniqueness_config.py
# v1.0 | 04-Aug-2026 | Package 4a. Covers the schema v0.4 uniqueness dials:
#                      weight normalisation, band ordering, the steward-versus-
#                      advisory band arithmetic, the configuration fingerprint,
#                      the loud rejection of the v0.3 singular 'blocking_key',
#                      and a round trip through the real config/schema/mara.yaml.
# ---------------------------------------------------------------------------
"""Offline. No LLM, no network, no embeddings.

The point of these tests is that a WRONG configuration fails at load time with
a message a steward can act on, rather than producing quietly wrong duplicates
much later.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.data.schema import (
    ALLOWED_FUZZY_METRICS,
    COMPARE_LANGUAGE,
    MAX_BAND,
    CompareField,
    UniquenessConfig,
    load_table_schema,
)

MARA_SCHEMA_PATH: str = "config/schema/mara.yaml"


def _config(**overrides) -> UniquenessConfig:
    """A valid MARA-like configuration, with any part overridden."""
    payload: dict = {
        "blocking_keys": ["MTART", "MEINS"],
        "compare_fields": [{"field": "MAKT.MAKTX", "weight": 1.0}],
    }
    payload.update(overrides)
    return UniquenessConfig(**payload)


# ---------------------------------------------------------------------------
# Shape and defaults
# ---------------------------------------------------------------------------

def test_defaults_load_for_a_table_with_no_uniqueness_block():
    # MAKT and MARC carry no uniqueness block; the defaults must be harmless.
    config = UniquenessConfig()
    assert config.blocking_keys == []
    assert config.compare_fields == []
    assert config.scope is None
    assert config.bands.duplicate == 0.92


def test_plain_string_compare_field_is_accepted_as_shorthand():
    config = _config(compare_fields=["MAKT.MAKTX"])
    assert config.compare_fields == [CompareField(field="MAKT.MAKTX", weight=1.0)]


def test_blocking_keys_takes_more_than_one_field():
    config = _config()
    assert config.blocking_keys == ["MTART", "MEINS"]


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

def test_compare_weights_normalise_to_one():
    config = _config(compare_fields=[
        {"field": "MAKT.MAKTX", "weight": 7.0},
        {"field": "MARA.NORMT", "weight": 3.0},
    ])
    shares = config.normalised_compare_weights()
    assert shares == {"MAKT.MAKTX": 0.7, "MARA.NORMT": 0.3}
    assert abs(sum(shares.values()) - 1.0) < 1e-9


def test_declared_weights_are_not_rewritten_by_normalisation():
    # A screen must be able to show what the steward actually typed.
    config = _config(compare_fields=[
        {"field": "MAKT.MAKTX", "weight": 7.0},
        {"field": "MARA.NORMT", "weight": 3.0},
    ])
    config.normalised_compare_weights()
    assert [entry.weight for entry in config.compare_fields] == [7.0, 3.0]


def test_method_weights_normalise_to_one():
    config = _config(methods={"fuzzy": {"weight": 3.0}, "semantic": {"weight": 1.0}})
    assert config.normalised_method_weights() == {"fuzzy": 0.75, "semantic": 0.25}


def test_semantic_can_be_switched_off_leaving_fuzzy_alone():
    # This is the fuzzy-only fallback used when no embeddings artefact exists.
    config = _config(methods={"fuzzy": {"weight": 1.0}, "semantic": {"weight": 0.0}})
    assert config.normalised_method_weights() == {"fuzzy": 1.0, "semantic": 0.0}


def test_a_zero_weight_compare_field_is_rejected():
    with pytest.raises(ValidationError) as error:
        _config(compare_fields=[{"field": "MAKT.MAKTX", "weight": 0.0}])
    assert "remove it from compare_fields" in str(error.value)


def test_switching_both_methods_off_is_rejected():
    with pytest.raises(ValidationError) as error:
        _config(methods={"fuzzy": {"weight": 0.0}, "semantic": {"weight": 0.0}})
    assert "no comparison would" in str(error.value)


def test_an_unknown_fuzzy_metric_is_rejected_by_name():
    with pytest.raises(ValidationError) as error:
        _config(methods={"fuzzy": {"metric": "levenshtein"}})
    assert "unknown fuzzy metric" in str(error.value)
    assert "jaro_winkler" in str(error.value)


def test_every_allowed_metric_is_accepted():
    metric: str = ""
    for metric in ALLOWED_FUZZY_METRICS:
        assert _config(methods={"fuzzy": {"metric": metric}}).methods.fuzzy.metric == metric


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------

def test_bands_out_of_order_are_rejected():
    with pytest.raises(ValidationError) as error:
        _config(bands={"duplicate": 0.5, "review_low": 0.8})
    assert "review_low" in str(error.value)


def test_a_band_above_one_is_rejected():
    with pytest.raises(ValidationError):
        _config(bands={"duplicate": 1.2, "review_low": 0.8})


def test_no_advisory_leaves_the_steward_bands_untouched():
    result = _config().effective_bands()
    assert result["duplicate"] == 0.92
    assert result["review_low"] == 0.8
    assert result["shift"] == 0.0


def test_an_advisory_shift_raises_both_bands_and_shows_the_arithmetic():
    # Problem B: the steward sets the bands, an advisory adjusts them, and all
    # three numbers are recorded so nobody thinks their setting was ignored.
    result = _config().effective_bands(0.05)
    assert result["steward_duplicate"] == 0.92
    assert result["shift"] == 0.05
    assert result["duplicate"] == 0.97
    assert result["review_low"] == 0.85


def test_a_shift_cannot_push_a_band_to_a_perfect_match():
    # A duplicate band of 1.0 would silently switch near-duplicate detection
    # off, so the shift is capped and the bands stay in order.
    result = _config().effective_bands(0.9)
    assert result["duplicate"] == MAX_BAND
    assert result["review_low"] < result["duplicate"]


# ---------------------------------------------------------------------------
# The v0.3 spelling must fail loudly, not silently
# ---------------------------------------------------------------------------

def test_the_old_singular_blocking_key_raises_with_instructions():
    # Left unguarded, pydantic would ignore the unknown key and load a config
    # with NO blocking at all, comparing every record against every other one.
    with pytest.raises(ValidationError) as error:
        UniquenessConfig(blocking_key="MTART", compare_fields=["MAKT.MAKTX"])
    message = str(error.value)
    assert "blocking_keys" in message
    assert "MTART, MEINS" in message


# ---------------------------------------------------------------------------
# Fingerprint (Problem C: results going stale after a settings change)
# ---------------------------------------------------------------------------

def test_the_same_configuration_always_gives_the_same_fingerprint():
    assert _config().fingerprint() == _config().fingerprint()


def test_changing_any_dial_changes_the_fingerprint():
    base = _config().fingerprint()
    assert _config(bands={"duplicate": 0.9, "review_low": 0.8}).fingerprint() != base
    assert _config(blocking_keys=["MTART"]).fingerprint() != base
    assert _config(methods={"fuzzy": {"weight": 0.9}}).fingerprint() != base


def test_the_fingerprint_is_short_enough_to_show_on_a_screen():
    assert len(_config().fingerprint()) == 12


# ---------------------------------------------------------------------------
# The real MARA configuration on disk
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not Path(MARA_SCHEMA_PATH).exists(),
    reason="run from the repository root",
)
def test_the_shipped_mara_schema_loads_and_carries_the_agreed_dials():
    schema = load_table_schema(MARA_SCHEMA_PATH)
    config = schema.uniqueness
    assert config.blocking_keys == ["MTART", "MEINS"]
    assert [entry.field for entry in config.compare_fields] == ["MAKT.MAKTX"]
    # Uniqueness fuzzy method: token_sort_ratio since Package 4f preparation.
    # See config/schema/mara.yaml for why and for the alternatives allowed.
    assert config.methods.fuzzy.metric == "token_sort_ratio"
    assert config.bands.duplicate == 0.92
    assert config.bands.review_low == 0.8


def test_the_compare_language_is_english_and_named():
    # Phase 1 matches English descriptions only. Named so the choice is visible
    # rather than buried as a bare 'E' inside the agent.
    assert COMPARE_LANGUAGE == "E"
