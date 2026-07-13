# ---------------------------------------------------------------------------
# tests/test_repository_smoke.py
# v1.0 | 13-Jul-2026 | Initial creation. Repository lifecycle (approve /
#                      reject / edit / retire), ledger assertions, illegal
#                      transitions, manual candidates through the same gate,
#                      human-only strength promotion, export_approved in the
#                      importer/rule_loader shape, file persistence round-trip,
#                      session isolation - and the FULL LOOP: suggest ->
#                      artefact -> ingest -> approve -> export -> validate.
# ---------------------------------------------------------------------------
"""No LLM anywhere. The loop test uses fake interpreter/suggester programs, so
Package 2's closing claim - an agent suggests, a human approves, the approved
rule is executable - is proven offline."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.contracts import RuleSpec
from src.rules.repository import (
    FileRepository,
    SessionRepository,
    record_from_suggestion,
)
from tools.run_suggestion import run_suggestion, write_artefact


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _valid_spec(rule_id: str = "IS_MARA_MATKL_NOT_NULL", table: str = "MARA") -> dict:
    return {
        "rule_id": rule_id,
        "name": "MATKL populated",
        "table": table,
        "dama_dimension": "Completeness",
        "archetype": "not_null",
        "severity": "High",
        "description": "Material group must be populated.",
        "fields": ["MATKL"],
        "assertion": {"node": "cmp", "field": "MATKL", "op": "is_not_null"},
        "executable": True,
        "provenance": {"source": "information_steward"},
    }


def _suggestion_payload(rule_id: str = "IS_MARA_MATKL_NOT_NULL") -> dict:
    return {
        "rule_spec": _valid_spec(rule_id),
        "origin": "bank_match",
        "parameter_source": "template_fixed",
        "rationale": "Population is 99.8%; the gap clusters on legacy records.",
        "evidence_citations": ["profile.mara.matkl.populated_pct=99.8"],
        "confidence": {"s_prior": 1.0, "s_support": 0.998, "s_coverage": 1.0,
                       "w_prior": 0.4, "w_support": 0.4, "w_coverage": 0.2,
                       "confidence": 0.9992},
        "description_risk": "low",
        "template_ref": "TPL-IS_MARA_MATKL_NOT_NULL",
    }


def _artefact(tmp_path: Path, payloads: list[dict]) -> Path:
    artefact = {"run": {"run_id": "test123", "at": "2026-07-13T00:00:00+00:00",
                        "table": "MARA", "counts": {"total": len(payloads)}},
                "candidates": payloads}
    target = tmp_path / "suggestions.json"
    target.write_text(json.dumps(artefact), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_ingest_and_approve(tmp_path: Path):
    repo = FileRepository(tmp_path / "repo")
    count = repo.load_candidates(_artefact(tmp_path, [_suggestion_payload()]))
    assert count == 1
    record = repo.drafts()[0]
    # bank_match with s_prior 1.0 -> strong / proven_template (v1 derivation).
    assert record.strength == "strong"
    repo.approve(record.candidate_id, actor="luqman")
    assert repo.drafts() == []
    assert len(repo.approved_rules()) == 1


def test_reject_requires_reason(tmp_path: Path):
    repo = FileRepository(tmp_path / "repo")
    repo.load_candidates(_artefact(tmp_path, [_suggestion_payload()]))
    record = repo.drafts()[0]
    with pytest.raises(ValueError):
        repo.reject(record.candidate_id, actor="luqman", reason="")
    repo.reject(record.candidate_id, actor="luqman", reason="describes current state")
    assert repo.by_state("rejected")[0].decision_reason == "describes current state"


def test_illegal_transitions_blocked(tmp_path: Path):
    repo = FileRepository(tmp_path / "repo")
    repo.load_candidates(_artefact(tmp_path, [_suggestion_payload()]))
    record = repo.drafts()[0]
    repo.approve(record.candidate_id, actor="luqman")
    with pytest.raises(ValueError):
        repo.approve(record.candidate_id, actor="luqman")   # approved -> approved
    repo.retire(record.candidate_id, actor="luqman")
    with pytest.raises(ValueError):
        repo.retire(record.candidate_id, actor="luqman")    # retired is terminal


def test_edit_and_approve_validates_and_diffs(tmp_path: Path):
    repo = FileRepository(tmp_path / "repo")
    repo.load_candidates(_artefact(tmp_path, [_suggestion_payload()]))
    record = repo.drafts()[0]

    # A malformed edit must raise, not enter the store.
    broken = dict(_valid_spec())
    broken["archetype"] = "no_such_archetype"
    with pytest.raises(Exception):
        repo.edit_and_approve(record.candidate_id, broken, actor="luqman")

    edited = dict(_valid_spec())
    edited["severity"] = "Critical"
    repo.edit_and_approve(record.candidate_id, edited, actor="luqman", reason="raise severity")
    # The ledger diff records exactly the changed key.
    ledger_lines = (tmp_path / "repo" / "ledger.jsonl").read_text().strip().splitlines()
    last = json.loads(ledger_lines[-1])
    assert last["event"] == "edited_and_approved"
    assert "severity" in last["diff"] and last["diff"]["severity"]["after"] == "Critical"


def test_manual_candidate_same_gate(tmp_path: Path):
    repo = FileRepository(tmp_path / "repo")
    # Malformed manual rule: rejected at the door.
    with pytest.raises(Exception):
        repo.add_manual_candidate({"rule_id": "X"}, actor="luqman")
    # Valid manual rule: becomes a draft with customer_authored provenance and
    # unverified strength - the same lifecycle, no side door.
    record = repo.add_manual_candidate(_valid_spec("CUST_MARA_MATKL"), actor="luqman",
                                       rationale="site convention")
    assert record.state == "draft"
    assert record.origin == "customer_authored"
    assert record.strength_reason == "unverified"
    assert record.rule_spec["provenance"]["source"] == "customer_authored"


def test_promote_strength_is_audited(tmp_path: Path):
    repo = FileRepository(tmp_path / "repo")
    record = repo.add_manual_candidate(_valid_spec("CUST_X"), actor="luqman")
    repo.approve(record.candidate_id, actor="luqman")
    with pytest.raises(ValueError):
        repo.promote_strength(record.candidate_id, "strong", reason="", actor="luqman")
    repo.promote_strength(record.candidate_id, "strong", reason="regulatory",
                          actor="luqman", note="MiFID II mandates this field")
    assert repo.records[record.candidate_id].strength == "strong"
    ledger = [json.loads(x) for x in
              (tmp_path / "repo" / "ledger.jsonl").read_text().strip().splitlines()]
    events = [e["event"] for e in ledger]
    assert "strength_changed" in events
    changed = [e for e in ledger if e["event"] == "strength_changed"][0]
    assert changed["note"] == "MiFID II mandates this field"


def test_file_persistence_round_trip(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo = FileRepository(repo_dir)
    repo.load_candidates(_artefact(tmp_path, [_suggestion_payload()]))
    record = repo.drafts()[0]
    repo.approve(record.candidate_id, actor="luqman")

    # A fresh instance reloads the snapshot: state survives a restart.
    reloaded = FileRepository(repo_dir)
    assert len(reloaded.approved_rules()) == 1
    assert reloaded.records[record.candidate_id].decided_by == "luqman"


def test_session_isolation():
    left = SessionRepository()
    right = SessionRepository()
    left.add_manual_candidate(_valid_spec("CUST_LEFT"), actor="visitor_a")
    assert left.drafts() and not right.drafts()
    assert left.session_ledger and not right.session_ledger


def test_export_approved_matches_rule_loader_shape(tmp_path: Path):
    repo = FileRepository(tmp_path / "repo")
    repo.load_candidates(_artefact(tmp_path, [
        _suggestion_payload("IS_MARA_MATKL_NOT_NULL"),
    ]))
    record = repo.drafts()[0]
    repo.approve(record.candidate_id, actor="luqman")
    written = repo.export_approved(tmp_path / "approved")
    assert len(written) == 1 and written[0].name == "mara_rules.yaml"

    payload = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    # Exactly the importer's shape: {table, rule_count, rules}.
    assert payload["table"] == "MARA"
    assert payload["rule_count"] == 1
    RuleSpec.model_validate(payload["rules"][0])  # executable by contract


# ---------------------------------------------------------------------------
# The full loop, offline: suggest -> artefact -> ingest -> approve -> export
# ---------------------------------------------------------------------------

class _FakeInterpreter:
    def interpret(self, profile):
        return SimpleNamespace(table_name=profile.get("table", ""),
                               field_characterisations=[], health_summary="", concerns=[])


class _FakeSuggester:
    def __init__(self, candidates):
        self.candidates = candidates

    def suggest(self, profile, interpretation):
        return self.candidates


def test_full_loop_suggest_to_executable_rule(tmp_path: Path):
    # A candidate as the real suggester emits it (dict form via the payload
    # helper stands in for the dataclass; candidate_suggestion_to_dict accepts
    # both).
    artefact = run_suggestion(
        profile={"table": "MARA", "fields": {}},
        interpreter=_FakeInterpreter(),
        suggester=_FakeSuggester([_suggestion_payload()]),
        dataset_label="synthetic-v1", model_label="fake",
    )
    assert artefact["run"]["counts"]["total"] == 1
    target = write_artefact(artefact, tmp_path / "artefacts" / "s.json")

    repo = FileRepository(tmp_path / "repo")
    assert repo.load_candidates(target) == 1
    record = repo.drafts()[0]
    repo.approve(record.candidate_id, actor="luqman", reason="clean evidence")

    written = repo.export_approved(tmp_path / "approved")
    payload = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    spec = RuleSpec.model_validate(payload["rules"][0])
    assert spec.executable is True
    # The loop is closed: suggested -> gated -> approved -> loadable by the
    # execution layer in its native shape.
