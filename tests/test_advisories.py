# ---------------------------------------------------------------------------
# tests/test_advisories.py
# v1.0 | 04-Aug-2026 | Package 4b. Covers the structured advisory: its six keys,
#                      the loud rejection of an unknown or malformed one, the
#                      steward-versus-advisory band arithmetic, largest-shift
#                      combination, and record exclusion driven by validity
#                      findings. Also proves the two-compare-field case that the
#                      shipped MARA settings cannot reach.
# ---------------------------------------------------------------------------
"""Offline. No LLM, no network, no data files.

The theme is that advice which cannot be acted on must FAIL rather than be
quietly dropped. A silently ignored advisory is the worst outcome available:
the upstream agent would believe its advice was taken, the report would list it
as delivered, and nothing would ever show the gap.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agents.uniqueness_settings import (
    ADVISORY_KEYS,
    build_advisory,
    describe_advisory,
    excluded_record_keys,
    resolve_settings,
    summarise_settings,
)
from src.contracts import AdvisoryAction, Dimension
from src.data.schema import UniquenessConfig

MARA_CONFIG = UniquenessConfig(
    blocking_keys=["MTART", "MEINS"],
    compare_fields=[{"field": "MAKT.MAKTX", "weight": 1.0}],
)

TWO_FIELD_CONFIG = UniquenessConfig(
    blocking_keys=["MTART", "MEINS"],
    compare_fields=[
        {"field": "MAKT.MAKTX", "weight": 0.7},
        {"field": "MARA.NORMT", "weight": 0.3},
    ],
)


def _threshold(shift: float = 0.05, table: str = "MAKT", field: str = "MAKTX") -> dict:
    return build_advisory(
        action=AdvisoryAction.RAISE_THRESHOLD,
        source="Completeness",
        table=table,
        field=field,
        value=shift,
        why="only 50.0% populated, so matching on it is unreliable",
    )


def _exclusion(table: str = "MAKT", field: str = "MAKTX") -> dict:
    return build_advisory(
        action=AdvisoryAction.EXCLUDE_RECORDS,
        source="Validity",
        table=table,
        field=field,
        value=None,
        why="3 record(s) hold a description that failed a validity check",
    )


def _validity_finding(record_id: str, table: str = "MAKT", field: str = "MAKTX"):
    return SimpleNamespace(
        dimension=Dimension.VALIDITY, table=table, field=field, record_id=record_id
    )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_an_advisory_always_carries_all_six_keys():
    advisory = _threshold()
    assert set(advisory) == set(ADVISORY_KEYS)


def test_an_action_carrying_no_number_still_has_the_value_key():
    # A missing key and a key set to None are different things. Suppression
    # carries no number, but the key is present so no reader has to guess.
    assert _exclusion()["value"] is None


def test_a_person_readable_line_is_produced_for_each_action():
    assert "raise the match bands" in describe_advisory(_threshold())
    assert "hold back records" in describe_advisory(_exclusion())


# ---------------------------------------------------------------------------
# Advice that cannot be acted on must fail
# ---------------------------------------------------------------------------

def test_an_unknown_action_raises_rather_than_being_ignored():
    bad = dict(_threshold())
    bad["action"] = "delete_everything"
    with pytest.raises(ValueError) as error:
        resolve_settings(MARA_CONFIG, [bad])
    assert "unknown advisory action" in str(error.value)
    assert "raise_threshold" in str(error.value)


def test_a_missing_key_raises_and_names_the_key():
    bad = dict(_threshold())
    del bad["why"]
    with pytest.raises(ValueError) as error:
        resolve_settings(MARA_CONFIG, [bad])
    assert "missing why" in str(error.value)


def test_a_threshold_advisory_with_no_number_raises():
    bad = dict(_threshold())
    bad["value"] = None
    with pytest.raises(ValueError) as error:
        resolve_settings(MARA_CONFIG, [bad])
    assert "must carry a value" in str(error.value)


# ---------------------------------------------------------------------------
# Bands: steward first, advisory second
# ---------------------------------------------------------------------------

def test_no_advisories_leaves_the_steward_settings_alone():
    settings = resolve_settings(MARA_CONFIG, [])
    assert settings["bands"]["duplicate"] == 0.92
    assert settings["bands"]["shift"] == 0.0
    assert settings["advisories_applied"] == []


def test_one_threshold_advisory_raises_the_bands_and_shows_the_arithmetic():
    settings = resolve_settings(MARA_CONFIG, [_threshold(0.05)])
    bands = settings["bands"]
    assert bands["steward_duplicate"] == 0.92
    assert bands["shift"] == 0.05
    assert bands["duplicate"] == 0.97
    assert bands["review_low"] == 0.85


def test_two_threshold_advisories_take_the_largest_shift_not_the_sum():
    # Both advisories say the same thing - the text signals are weak. Adding
    # them would count one problem twice and reach the cap quickly.
    settings = resolve_settings(
        MARA_CONFIG,
        [_threshold(0.05), _threshold(0.03, table="MARA", field="NORMT")],
    )
    assert settings["bands"]["shift"] == 0.05
    assert len(settings["advisories_applied"]) == 2


def test_the_settings_code_travels_with_the_resolved_settings():
    settings = resolve_settings(MARA_CONFIG, [])
    assert settings["fingerprint"] == MARA_CONFIG.fingerprint()
    assert len(settings["fingerprint"]) == 12


# ---------------------------------------------------------------------------
# Record exclusion
# ---------------------------------------------------------------------------

def test_an_exclusion_advisory_registers_the_signal_that_went_bad():
    settings = resolve_settings(MARA_CONFIG, [_exclusion()])
    assert settings["exclusion_targets"] == [{"table": "MAKT", "field": "MAKTX"}]
    # It changes the DATA, not the settings, so the bands are untouched.
    assert settings["bands"]["duplicate"] == 0.92


def test_the_record_keys_come_from_the_findings_not_the_advisory():
    # The advisory names the signal; the findings name the records. A list of
    # thousands of keys has no business travelling in a state channel.
    settings = resolve_settings(MARA_CONFIG, [_exclusion()])
    findings = [
        _validity_finding("MATNR=000000000000001"),
        _validity_finding("MATNR=000000000000002"),
    ]
    excluded = excluded_record_keys(settings, findings)
    assert excluded == {
        "MAKT": {"MATNR=000000000000001", "MATNR=000000000000002"}
    }


def test_findings_on_other_fields_do_not_exclude_anything():
    settings = resolve_settings(MARA_CONFIG, [_exclusion()])
    findings = [_validity_finding("MATNR=1", table="MARA", field="MEINS")]
    assert excluded_record_keys(settings, findings) == {}


def test_findings_from_other_dimensions_do_not_exclude_anything():
    # Only a VALIDITY finding says the text itself is untrustworthy. A
    # completeness finding says the text is absent, which the matcher skips
    # anyway because there is nothing to compare.
    settings = resolve_settings(MARA_CONFIG, [_exclusion()])
    finding = SimpleNamespace(
        dimension=Dimension.COMPLETENESS, table="MAKT", field="MAKTX", record_id="MATNR=1"
    )
    assert excluded_record_keys(settings, [finding]) == {}


def test_no_exclusion_advisory_means_no_records_are_held_back():
    settings = resolve_settings(MARA_CONFIG, [_threshold()])
    findings = [_validity_finding("MATNR=1")]
    assert excluded_record_keys(settings, findings) == {}


def test_placeholder_descriptions_are_the_case_this_prevents():
    """The failure record exclusion exists to stop.

    Twenty materials all described "TEST" normalise to the same text and score a
    perfect match against each other. Left in, they would form ONE cluster of
    genuinely different materials, and because the match looks perfect the
    survivorship rules would merge them without asking anybody.
    """
    settings = resolve_settings(MARA_CONFIG, [_exclusion()])
    findings = [_validity_finding(f"MATNR={index:018d}") for index in range(1, 21)]
    excluded = excluded_record_keys(settings, findings)
    assert len(excluded["MAKT"]) == 20


# ---------------------------------------------------------------------------
# Compare fields
# ---------------------------------------------------------------------------

def test_the_shipped_mara_settings_carry_one_compare_field():
    # Worth stating in a test: MARA compares ONE field, which is why advice
    # about a bad signal must exclude records rather than drop the field. There
    # would be nothing left to compare.
    settings = resolve_settings(MARA_CONFIG, [])
    assert len(settings["compare_fields"]) == 1
    assert settings["compare_weights"] == {"MAKT.MAKTX": 1.0}


def test_two_compare_fields_share_the_score_in_proportion():
    settings = resolve_settings(TWO_FIELD_CONFIG, [])
    assert settings["compare_weights"] == {"MAKT.MAKTX": 0.7, "MARA.NORMT": 0.3}


def test_method_weights_travel_with_the_settings():
    settings = resolve_settings(MARA_CONFIG, [])
    assert settings["method_weights"] == {"fuzzy": 0.5, "semantic": 0.5}


# ---------------------------------------------------------------------------
# The readable summary
# ---------------------------------------------------------------------------

def test_the_summary_says_nothing_changed_when_nothing_did():
    settings = resolve_settings(MARA_CONFIG, [])
    assert "no advisories" in summarise_settings(settings, {})


def test_the_summary_reports_both_the_band_move_and_the_held_back_count():
    settings = resolve_settings(MARA_CONFIG, [_threshold(0.05), _exclusion()])
    summary = summarise_settings(settings, {"MAKT": {"MATNR=1", "MATNR=2"}})
    assert "0.97" in summary
    assert "2 record(s) held back" in summary
