# ---------------------------------------------------------------------------
# src/agents/adjudicator.py
# v1.0 | 11-Aug-2026 | Package 4g. The second-pass adjudicator on the uncertain
#                      band. judge() calls the model on capped, highest-first
#                      pairs and skips pairs already judged. The verdicts live
#                      in ONE artefact beside the dataset, like the embeddings,
#                      and apply_verdicts() turns confirmed pairs into clusters
#                      and findings deterministically, offline.
# ---------------------------------------------------------------------------
"""Judge the pairs the deterministic methods could not settle.

Three parts, and only the first needs a model:

    judge()           call the model on the band, cap the spend, cache by pair
    save / load       the verdicts artefact beside the dataset
    apply_verdicts()  deterministic: confirmed pairs become clusters and
                      findings, rejected pairs leave the open list

The artefact is the cache. A pair key is a hash of the two record keys and the
two normalised texts, so a judged pair costs nothing on a re-run, and a changed
description is judged again. The settings fingerprint and the model name travel
with every verdict; a consumer that finds a different fingerprint marks the
verdicts stale and does not apply them.

The model never merges anything. A confirmed pair becomes a cluster with
resolution needs_steward, and a person decides.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from src.contracts import (
    ClusterMember,
    ClusterResolution,
    Dimension,
    DuplicateCluster,
    Finding,
    MatchMode,
    PairVerdict,
    Severity,
    SurvivorReason,
    VerdictDecision,
)
from src.agents.text_normaliser import normalise_text
from src.data.schema import TableSchema

VERDICTS_FILENAME: str = "verdicts.json"
DEFAULT_CAP: int = 500


def pair_key(left_id: str, right_id: str, left_text: str, right_text: str) -> str:
    """A stable key for one pair, independent of member order.

    The texts are normalised HERE, so a caller with the original wording (the
    CLI shows the model what is really in the field) and a caller with the
    normalised wording (the matcher) compute the SAME key. A changed
    description still changes the key, which is the point: that pair must be
    judged again.
    """
    first: tuple[str, str]
    second: tuple[str, str]

    first, second = sorted([
        (left_id, normalise_text(left_text)),
        (right_id, normalise_text(right_text)),
    ])
    raw: str = "|".join([first[0], first[1], second[0], second[1]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def verdicts_path(data_dir: str | Path) -> Path:
    """Where the verdicts artefact lives: beside its dataset."""
    return Path(data_dir) / VERDICTS_FILENAME


def load_verdicts(data_dir: str | Path) -> dict[str, Any]:
    """Read the artefact. An absent or unreadable file is an empty artefact."""
    path: Path = verdicts_path(data_dir)

    if not path.exists():
        return {"meta": {}, "verdicts": {}}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {"meta": {}, "verdicts": {}}


def save_verdicts(data_dir: str | Path, artefact: dict[str, Any]) -> Path:
    path: Path = verdicts_path(data_dir)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(artefact, handle, ensure_ascii=False, indent=2)
    return path


def _pair_score(pair: dict[str, Any]) -> float:
    """Sort key for the band. A named function, not a lambda."""
    return float(pair.get("score", 0.0))


class Adjudicator:
    """Judge the uncertain band with a capped number of model calls.

    program is injectable: the real one is dspy.ChainOfThought(PairAdjudication)
    wired by the CLI; tests inject a fake, so the tests are offline.
    """

    def __init__(
        self,
        program: Any,
        model_label: str = "",
        cap: int = DEFAULT_CAP,
    ) -> None:
        self.program = program
        self.model_label = model_label
        self.cap = cap
        self.calls_made: int = 0
        self.skipped_cached: int = 0

    def judge(
        self,
        pairs: list[dict[str, Any]],
        texts: dict[str, str],
        fingerprint: str,
        block_context: dict[str, dict[str, str]],
        artefact: Optional[dict[str, Any]] = None,
        match_mode: str = "full",
    ) -> dict[str, Any]:
        """Judge up to `cap` pairs, highest scores first, and return the artefact.

        Highest first, because a pair near the duplicate band is the most
        likely real duplicate, so a capped run spends its calls where a find is
        most probable. Pairs already in the artefact under the same fingerprint
        are skipped; the artefact is the cache.
        """
        working: dict[str, Any] = artefact if artefact is not None else {"meta": {}, "verdicts": {}}
        verdicts: dict[str, Any] = working.setdefault("verdicts", {})
        ordered: list[dict[str, Any]] = sorted(pairs, key=_pair_score, reverse=True)
        pair: dict[str, Any] = {}
        key: str = ""
        left_text: str = ""
        right_text: str = ""
        context: dict[str, str] = {}
        answer: Any = None
        semantic_ran: bool = match_mode == "full"

        self.calls_made = 0
        self.skipped_cached = 0
        for pair in ordered:
            if self.calls_made >= self.cap:
                break
            left_text = texts.get(str(pair.get("left_id", "")), "")
            right_text = texts.get(str(pair.get("right_id", "")), "")
            key = pair_key(str(pair["left_id"]), str(pair["right_id"]), left_text, right_text)
            if key in verdicts and verdicts[key].get("fingerprint") == fingerprint:
                self.skipped_cached += 1
                continue
            context = block_context.get(f"{pair['left_id']}|{pair['right_id']}", {})
            answer = self.program(
                description_a=left_text,
                description_b=right_text,
                material_type=context.get("MTART", ""),
                unit=context.get("MEINS", ""),
                fuzzy_score=float(pair.get("score", 0.0)),
                semantic_score=float(pair.get("score", 0.0)) if semantic_ran else -1.0,
            )
            self.calls_made += 1
            verdicts[key] = PairVerdict(
                table=str(pair.get("table", "")),
                left_id=str(pair["left_id"]),
                right_id=str(pair["right_id"]),
                decision=VerdictDecision(str(answer.decision)),
                confidence=max(0.0, min(1.0, float(answer.confidence))),
                reasoning=str(answer.reasoning),
                score=float(pair.get("score", 0.0)),
                model=self.model_label,
                fingerprint=fingerprint,
            ).model_dump(mode="json")

        working["meta"] = {
            "fingerprint": fingerprint,
            "model": self.model_label,
            "cap": self.cap,
            "judged_total": len(verdicts),
        }
        return working


# ---------------------------------------------------------------------------
# Applying verdicts - deterministic, offline
# ---------------------------------------------------------------------------

def _record_completeness(
    record_id: str,
    frame: Optional[pd.DataFrame],
    schema: Optional[TableSchema],
) -> tuple[int, int]:
    """Populated mandatory fields and populated fields for one record.

    Used to pick the survivor of a verdict pair the same way the matcher picks
    one: the more complete record is the one to keep.
    """
    populated_mandatory: int = 0
    populated_total: int = 0
    key_fields: list[str] = []
    row: pd.DataFrame = None
    values: dict[str, Any] = {}
    name: str = ""
    value: Any = None
    part: str = ""

    if frame is None or schema is None:
        return 0, 0
    key_fields = list(schema.primary_key)
    working: pd.DataFrame = frame
    for part in record_id.split("|"):
        if "=" not in part:
            return 0, 0
        name, _, value = part.partition("=")
        if name not in working.columns:
            return 0, 0
        working = working[working[name].astype(str) == str(value)]
    if working.empty:
        return 0, 0
    values = working.iloc[0].to_dict()
    for name, value in values.items():
        if value is None or str(value).strip() == "" or str(value).strip().lower() == "nan":
            continue
        populated_total += 1
        if name in schema.fields and schema.fields[name].mandatory:
            populated_mandatory += 1
    return populated_mandatory, populated_total


def apply_verdicts(
    candidate_pairs: list[dict[str, Any]],
    verdict_artefact: dict[str, Any],
    fingerprint: str,
    texts: dict[str, str],
    frame: Optional[pd.DataFrame] = None,
    schema: Optional[TableSchema] = None,
    match_mode: str = "full",
    cluster_offset: int = 0,
) -> dict[str, Any]:
    """Turn verdicts into clusters and findings; return what stays open.

    Deterministic and offline. A confirmed pair becomes a two-member cluster
    with resolution needs_steward and a finding whose confidence is the
    model's. A rejected pair leaves the open list, and the rejection is kept,
    because an answer must not vanish. Verdicts whose fingerprint does not
    match the current settings are STALE: none are applied, and the caller
    shows that instead.
    """
    verdicts: dict[str, Any] = (verdict_artefact or {}).get("verdicts", {})
    meta: dict[str, Any] = (verdict_artefact or {}).get("meta", {})
    open_pairs: list[dict[str, Any]] = []
    clusters: list[DuplicateCluster] = []
    findings: list[Finding] = []
    applied: list[PairVerdict] = []
    stale: bool = False
    pair: dict[str, Any] = {}
    key: str = ""
    entry: dict[str, Any] = {}
    verdict: PairVerdict = None
    confirmed: int = 0
    rejected: int = 0

    if not verdicts:
        return {"open_pairs": list(candidate_pairs), "clusters": [], "findings": [],
                "verdicts": [], "confirmed": 0, "rejected": 0, "stale": False,
                "model": meta.get("model", "")}
    if meta.get("fingerprint") and meta.get("fingerprint") != fingerprint:
        return {"open_pairs": list(candidate_pairs), "clusters": [], "findings": [],
                "verdicts": [], "confirmed": 0, "rejected": 0, "stale": True,
                "model": meta.get("model", "")}

    for pair in candidate_pairs:
        key = pair_key(
            str(pair["left_id"]), str(pair["right_id"]),
            texts.get(str(pair["left_id"]), ""), texts.get(str(pair["right_id"]), ""),
        )
        entry = verdicts.get(key)
        if entry is None or entry.get("fingerprint") != fingerprint:
            open_pairs.append(pair)
            continue
        verdict = PairVerdict.model_validate(entry)
        applied.append(verdict)
        if verdict.decision == VerdictDecision.NOT_DUPLICATE:
            rejected += 1
            continue
        confirmed += 1
        clusters.append(_verdict_cluster(
            pair, verdict, frame, schema, match_mode, cluster_offset + confirmed,
        ))
        findings.append(_verdict_finding(pair, verdict, clusters[-1]))

    return {"open_pairs": open_pairs, "clusters": clusters, "findings": findings,
            "verdicts": applied, "confirmed": confirmed, "rejected": rejected,
            "stale": stale, "model": meta.get("model", "")}


def _verdict_cluster(
    pair: dict[str, Any],
    verdict: PairVerdict,
    frame: Optional[pd.DataFrame],
    schema: Optional[TableSchema],
    match_mode: str,
    index: int,
) -> DuplicateCluster:
    """One confirmed pair as a two-member cluster. A person still decides."""
    left_counts: tuple[int, int] = _record_completeness(verdict.left_id, frame, schema)
    right_counts: tuple[int, int] = _record_completeness(verdict.right_id, frame, schema)
    survivor: str = verdict.left_id
    other: str = verdict.right_id
    survivor_counts: tuple[int, int] = left_counts
    other_counts: tuple[int, int] = right_counts
    reason: SurvivorReason = SurvivorReason.MOST_COMPLETE

    if right_counts > left_counts:
        survivor, other = verdict.right_id, verdict.left_id
        survivor_counts, other_counts = right_counts, left_counts
    if right_counts == left_counts:
        reason = SurvivorReason.NONE

    return DuplicateCluster(
        cluster_id=f"CL-LLM-{index:04d}",
        table=verdict.table or str(pair.get("table", "")),
        survivor_id=survivor,
        survivor_reason=reason,
        resolution=ClusterResolution.NEEDS_STEWARD,
        members=[
            ClusterMember(record_id=survivor, score=1.0, is_survivor=True,
                          populated_mandatory=survivor_counts[0],
                          populated_total=survivor_counts[1]),
            ClusterMember(record_id=other, score=verdict.score,
                          populated_mandatory=other_counts[0],
                          populated_total=other_counts[1]),
        ],
        weakest_link=verdict.score,
        mode=MatchMode(match_mode),
        blocking_values=dict(pair.get("blocking_values", {})),
    )


def _verdict_finding(
    pair: dict[str, Any],
    verdict: PairVerdict,
    cluster: DuplicateCluster,
) -> Finding:
    """The finding for a confirmed pair. Confidence is the MODEL'S, not 1.0."""
    other: str = cluster.members[1].record_id

    return Finding(
        dimension=Dimension.UNIQUENESS,
        table=cluster.table,
        record_id=other,
        field=None,
        rule_id=None,
        issue=f"Adjudicated duplicate of {cluster.survivor_id}: {verdict.reasoning}",
        severity=Severity.MEDIUM,
        confidence=verdict.confidence,
        metadata={
            "cluster_id": cluster.cluster_id,
            "survivor": cluster.survivor_id,
            "score": verdict.score,
            "source": "llm_adjudicator",
            "model": verdict.model,
        },
    )
