# ---------------------------------------------------------------------------
# src/rules/repository.py
# v1.0 | 13-Jul-2026 | Initial creation. The approved-rule store and its
#                      lifecycle (draft -> approved | rejected -> retired).
#                      One verb interface, two backends: FileRepository (YAML
#                      snapshot + append-only JSONL ledger) for real runs, and
#                      SessionRepository (in-memory) for the interactive public
#                      demo. Includes add_manual_candidate() for customer-
#                      authored rules and export_approved() so the existing
#                      rule_loader consumes approved rules unchanged.
# ---------------------------------------------------------------------------
"""The rules repository - where candidates become (or fail to become) rules.

Design contract (design doc 3.3-3.5):

  * Lifecycle: draft -> approved | rejected; approved -> retired. Every
    transition writes a ledger entry (who, when, why, and a spec diff for
    edits). The ledger is append-only by construction.
  * The UI renders and invokes; it NEVER decides. All state changes go through
    the verbs on this class. That rule is what makes the Phase 2 re-plumbing
    and the interactive demo cheap.
  * Customer-authored rules enter through the SAME gate: add_manual_candidate()
    validates against contracts and creates a draft with
    provenance.source=customer_authored and strength 'unverified'. No side door.
  * Agents never write strength. promote_strength() is the human verb, audited
    and reversible.
  * export_approved() writes approved rules in exactly the per-table YAML shape
    the importer produces and rule_loader already reads, so pointing execution
    at the repository is a one-line path change, not a loader rewrite.

v1 simplification, stated plainly: an approved record's initial strength is
derived from its origin (bank_match -> mapped back from the confidence prior;
inferred -> weak; manual -> unverified). The template's own strength block is
not carried through the CandidateSuggestion in this cut; promotion is human
anyway, and the ledger records every change.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from src.contracts import RuleSpec


# ---------------------------------------------------------------------------
# States and records
# ---------------------------------------------------------------------------

VALID_STATES = ("draft", "approved", "rejected", "retired")

# state -> the states it may legally move to
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("approved", "rejected"),
    "approved": ("retired",),
    "rejected": (),
    "retired": (),
}


@dataclass
class CandidateRecord:
    """One candidate rule and its lifecycle state. The suggestion payload is
    kept verbatim (serialised CandidateSuggestion) so the review UI can render
    the decomposed confidence card without re-deriving anything."""

    candidate_id: str
    state: str
    rule_spec: dict[str, Any]
    origin: str                        # bank_match | inferred | customer_authored
    parameter_source: Optional[str]
    rationale: str
    evidence_citations: list[str] = field(default_factory=list)
    confidence: dict[str, Any] = field(default_factory=dict)
    description_risk: str = "low"
    template_ref: Optional[str] = None
    strength: str = "weak"
    strength_reason: str = "inferred"
    created_at: str = ""
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    decision_reason: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_candidate_id(rule_id: str) -> str:
    """Stable-ish, readable id: the rule id plus a short random suffix (the
    same rule can be suggested again on a later run)."""
    suffix: str = uuid.uuid4().hex[:8]
    return f"{rule_id}::{suffix}"


# ---------------------------------------------------------------------------
# Suggestion serialisation (the batch runner writes; the repository reads)
# ---------------------------------------------------------------------------

def candidate_suggestion_to_dict(candidate: Any) -> dict[str, Any]:
    """Serialise a rule_suggester.CandidateSuggestion (or compatible object)
    into the artefact form. Accepts the dataclass or a plain dict."""
    if isinstance(candidate, dict):
        return dict(candidate)
    payload: dict[str, Any] = {
        "rule_spec": candidate.rule_spec,
        "origin": candidate.origin,
        "parameter_source": candidate.parameter_source,
        "rationale": candidate.rationale,
        "evidence_citations": list(candidate.evidence_citations),
        "confidence": asdict(candidate.confidence),
        "description_risk": candidate.description_risk,
        "template_ref": candidate.template_ref,
    }
    return payload


def _derive_strength(origin: str, confidence: dict[str, Any]) -> tuple[str, str]:
    """v1 origin-derived default strength (see module docstring)."""
    s_prior: Any = (confidence or {}).get("s_prior")
    if origin == "bank_match":
        if isinstance(s_prior, (int, float)) and s_prior >= 0.9:
            return "strong", "proven_template"
        if isinstance(s_prior, (int, float)) and s_prior >= 0.5:
            return "moderate", "proven_template"
        return "weak", "proven_template"
    if origin == "customer_authored":
        return "weak", "unverified"
    return "weak", "inferred"


def record_from_suggestion(payload: dict[str, Any]) -> CandidateRecord:
    """Turn one artefact candidate into a draft record."""
    rule_spec: dict[str, Any] = payload.get("rule_spec") or {}
    origin: str = str(payload.get("origin") or "inferred")
    confidence: dict[str, Any] = payload.get("confidence") or {}
    strength: str = ""
    reason: str = ""

    strength, reason = _derive_strength(origin, confidence)
    return CandidateRecord(
        candidate_id=_new_candidate_id(str(rule_spec.get("rule_id", "UNKNOWN"))),
        state="draft",
        rule_spec=rule_spec,
        origin=origin,
        parameter_source=payload.get("parameter_source"),
        rationale=str(payload.get("rationale") or ""),
        evidence_citations=[str(x) for x in (payload.get("evidence_citations") or [])],
        confidence=confidence,
        description_risk=str(payload.get("description_risk") or "low"),
        template_ref=payload.get("template_ref"),
        strength=strength,
        strength_reason=reason,
        created_at=_now(),
    )


# ---------------------------------------------------------------------------
# The repository (verbs + shared logic); backends differ only in persistence
# ---------------------------------------------------------------------------

class RulesRepository:
    """Verb interface + in-memory state. Subclasses persist. The Streamlit
    surfaces call ONLY these verbs; they never mutate records directly."""

    def __init__(self):
        self.records: dict[str, CandidateRecord] = {}

    # -- persistence hooks (no-ops in the session backend) --------------------

    def _persist(self) -> None:
        return None

    def _ledger(self, entry: dict[str, Any]) -> None:
        return None

    # -- ingestion -------------------------------------------------------------

    def load_candidates(self, artefact_path: str | Path) -> int:
        """Ingest a suggestions artefact (from tools/run_suggestion.py) as
        drafts. Returns the number ingested. Already-ingested candidates are
        not deduplicated across runs by design: a re-run is a new batch, and
        reviewing it again is the steward's call."""
        raw: dict[str, Any] = json.loads(Path(artefact_path).read_text(encoding="utf-8"))
        candidates: list[dict[str, Any]] = raw.get("candidates") or []
        count: int = 0
        payload: dict[str, Any] = {}
        record: CandidateRecord = None

        for payload in candidates:
            record = record_from_suggestion(payload)
            self.records[record.candidate_id] = record
            self._ledger({
                "at": _now(), "candidate_id": record.candidate_id,
                "event": "ingested", "from": None, "to": "draft",
                "actor": "batch_runner", "reason": raw.get("run", {}).get("run_id"),
            })
            count += 1
        self._persist()
        return count

    def add_manual_candidate(
        self,
        rule_spec: dict[str, Any],
        actor: str,
        rationale: str = "",
    ) -> CandidateRecord:
        """Customer-authored rule, entering through the SAME gate. Validated
        against contracts before it becomes a draft; a malformed spec raises
        rather than entering the lifecycle."""
        RuleSpec.model_validate(rule_spec)  # raises on malformed input

        provenance: dict[str, Any] = dict(rule_spec.get("provenance") or {})
        provenance["source"] = "customer_authored"
        rule_spec = dict(rule_spec)
        rule_spec["provenance"] = provenance

        record = CandidateRecord(
            candidate_id=_new_candidate_id(str(rule_spec.get("rule_id", "MANUAL"))),
            state="draft",
            rule_spec=rule_spec,
            origin="customer_authored",
            parameter_source="data_derived",
            rationale=rationale,
            evidence_citations=[],
            confidence={},
            description_risk="unassessed",
            template_ref=None,
            strength="weak",
            strength_reason="unverified",
            created_at=_now(),
        )
        self.records[record.candidate_id] = record
        self._ledger({
            "at": _now(), "candidate_id": record.candidate_id,
            "event": "manual_added", "from": None, "to": "draft",
            "actor": actor, "reason": rationale,
        })
        self._persist()
        return record

    # -- lifecycle verbs ---------------------------------------------------------

    def _transition(
        self,
        candidate_id: str,
        to_state: str,
        actor: str,
        reason: Optional[str],
        event: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> CandidateRecord:
        record: CandidateRecord = self.records.get(candidate_id)
        if record is None:
            raise KeyError(f"no such candidate: {candidate_id}")
        if to_state not in _TRANSITIONS.get(record.state, ()):
            raise ValueError(
                f"illegal transition {record.state} -> {to_state} for {candidate_id}"
            )
        entry: dict[str, Any] = {
            "at": _now(), "candidate_id": candidate_id, "event": event,
            "from": record.state, "to": to_state, "actor": actor, "reason": reason,
        }
        if extra:
            entry.update(extra)
        record.state = to_state
        record.decided_at = _now()
        record.decided_by = actor
        record.decision_reason = reason
        self._ledger(entry)
        self._persist()
        return record

    def approve(self, candidate_id: str, actor: str, reason: str = "") -> CandidateRecord:
        return self._transition(candidate_id, "approved", actor, reason, "approved")

    def reject(self, candidate_id: str, actor: str, reason: str) -> CandidateRecord:
        if not reason:
            raise ValueError("a rejection requires a reason")
        return self._transition(candidate_id, "rejected", actor, reason, "rejected")

    def edit_and_approve(
        self,
        candidate_id: str,
        edited_rule_spec: dict[str, Any],
        actor: str,
        reason: str = "",
    ) -> CandidateRecord:
        """Approve with an edited spec. The edit is validated against contracts
        and the ledger records a before/after diff of the changed keys."""
        record: CandidateRecord = self.records.get(candidate_id)
        if record is None:
            raise KeyError(f"no such candidate: {candidate_id}")
        RuleSpec.model_validate(edited_rule_spec)

        diff: dict[str, Any] = {}
        key: str = ""
        all_keys = set(record.rule_spec) | set(edited_rule_spec)
        for key in sorted(all_keys):
            if record.rule_spec.get(key) != edited_rule_spec.get(key):
                diff[key] = {"before": record.rule_spec.get(key), "after": edited_rule_spec.get(key)}

        record.rule_spec = dict(edited_rule_spec)
        return self._transition(
            candidate_id, "approved", actor, reason, "edited_and_approved",
            extra={"diff": diff},
        )

    def retire(self, candidate_id: str, actor: str, reason: str = "") -> CandidateRecord:
        return self._transition(candidate_id, "retired", actor, reason, "retired")

    def promote_strength(
        self,
        candidate_id: str,
        strength: str,
        reason: str,
        actor: str,
        note: str = "",
    ) -> CandidateRecord:
        """Human-only strength change, audited. Agents never call this."""
        record: CandidateRecord = self.records.get(candidate_id)
        valid_strengths = ("strong", "moderate", "weak")
        if record is None:
            raise KeyError(f"no such candidate: {candidate_id}")
        if strength not in valid_strengths:
            raise ValueError(f"invalid strength: {strength!r}")
        if not reason:
            raise ValueError("a strength change requires a reason")
        self._ledger({
            "at": _now(), "candidate_id": candidate_id, "event": "strength_changed",
            "from": record.strength, "to": strength, "actor": actor,
            "reason": reason, "note": note,
        })
        record.strength = strength
        record.strength_reason = reason
        self._persist()
        return record

    # -- queries -----------------------------------------------------------------

    def by_state(self, state: str) -> list[CandidateRecord]:
        return [r for r in self.records.values() if r.state == state]

    def drafts(self) -> list[CandidateRecord]:
        return self.by_state("draft")

    def approved_rules(self) -> list[dict[str, Any]]:
        """The RuleSpec dicts the execution layer runs."""
        return [r.rule_spec for r in self.by_state("approved")]

    # -- export for execution ------------------------------------------------------

    def export_approved(self, out_dir: str | Path) -> list[Path]:
        """Write approved rules as one YAML per table, in EXACTLY the shape the
        importer produces and rule_loader reads ({table, rule_count, rules}).
        Pointing execution at the repository is therefore a path change, not a
        loader change."""
        out_path: Path = Path(out_dir)
        by_table: dict[str, list[dict[str, Any]]] = {}
        written: list[Path] = []
        record: CandidateRecord = None
        table: str = ""
        rules: list[dict[str, Any]] = []
        payload: dict[str, Any] = {}
        target: Path = None

        out_path.mkdir(parents=True, exist_ok=True)
        for record in self.by_state("approved"):
            table = str(record.rule_spec.get("table", "UNKNOWN"))
            by_table.setdefault(table, []).append(record.rule_spec)

        for table, rules in sorted(by_table.items()):
            payload = {"table": table, "rule_count": len(rules), "rules": rules}
            target = out_path / f"{table.lower()}_rules.yaml"
            with target.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True,
                               default_flow_style=False)
            written.append(target)
        return written


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class SessionRepository(RulesRepository):
    """In-memory backend for the interactive public demo. A visitor's
    approve/reject clicks live and die with their session; the ledger is kept
    in memory so the UI can still show an audit trail for the session."""

    def __init__(self):
        super().__init__()
        self.session_ledger: list[dict[str, Any]] = []

    def _ledger(self, entry: dict[str, Any]) -> None:
        self.session_ledger.append(entry)


class FileRepository(RulesRepository):
    """File-backed backend for real runs: a YAML snapshot of all records plus
    an append-only JSONL ledger. One writer module (this one) keeps the two
    consistent - every verb persists the snapshot and appends the ledger in the
    same call."""

    def __init__(self, repo_dir: str | Path):
        super().__init__()
        self.repo_dir: Path = Path(repo_dir)
        self.snapshot_path: Path = self.repo_dir / "candidates.yaml"
        self.ledger_path: Path = self.repo_dir / "ledger.jsonl"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        raw: Any = None
        entries: list[dict[str, Any]] = []
        entry: dict[str, Any] = {}
        record: CandidateRecord = None

        if not self.snapshot_path.exists():
            return
        raw = yaml.safe_load(self.snapshot_path.read_text(encoding="utf-8"))
        entries = raw.get("records", []) if isinstance(raw, dict) else []
        for entry in entries:
            record = CandidateRecord(**entry)
            self.records[record.candidate_id] = record

    def _persist(self) -> None:
        payload: dict[str, Any] = {
            "version": 1,
            "records": [asdict(record) for record in self.records.values()],
        }
        with self.snapshot_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)

    def _ledger(self, entry: dict[str, Any]) -> None:
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
