# ---------------------------------------------------------------------------
# src/reporting/uniqueness_eval.py
# v1.0 | 04-Aug-2026 | Package 4e. How well did the matcher do? Three numbers,
#                      not one:
#                          cluster recall on injected twins  - simple
#                          decoy error rate                  - HEADLINE precision
#                          unlabelled joined pairs           - a count, not a rate
#                      Plus a score-spread report by strategy, so Package 5 can
#                      set the bands from data.
# ---------------------------------------------------------------------------
"""Measuring the matcher without lying about it.

Uniqueness cannot be scored the way the other dimensions are. The injector
labels a twin and names its source. The injector has no view on which of the
two should be kept, so scoring the survivor choice would measure a business
judgement against a label with no opinion on it.

Cluster-level detection is measurable. Precision is harder. Two pairs the
agent joins can mean two different things:

    the generator made two similar descriptions by accident  -> false positive
    two materials are genuinely alike and no label says so   -> not measurable

The clean baseline (generator v0.3) removes most of the first case; the decoys
(injector v0.5) turn a labelled subset of the second case into measurable
precision. What remains is reported as a count of "unlabelled joins", so the
report is honest about the limit rather than pretending to a single number.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from src.contracts import DefectLabel, Dimension, DuplicateCluster


class TwinRecall(BaseModel):
    """Did the injected twins land in the same cluster as their source?

    hidden_by_other_defect counts twins whose blocking key was later corrupted
    by the validity injector (a wrong MTART or a wrong MEINS puts a twin in a
    different block from its source, so the two are NEVER compared and no
    matcher could have found them). Reporting these separately is honest: a
    lower recall on this dataset is a fact about the dataset, not a matcher
    fault, and it is exactly the case the exclusion rule from Package 4b
    protects unrelated materials from.
    """

    total_twins: int = 0
    matched: int = 0
    hidden_by_other_defect: int = 0  # v1.0 - see the class docstring
    matchable: int = 0               # v1.0 - total_twins minus the hidden ones
    recall_pct: float = 0.0
    recall_on_matchable_pct: float = 0.0  # v1.0
    by_strategy: dict[str, dict[str, int]] = Field(default_factory=dict)


class DecoyResult(BaseModel):
    """Did the agent WRONGLY join a decoy pair?

    A decoy is two DIFFERENT materials given a confusable description. Every
    decoy that ends up in one cluster is a false positive with no argument.
    This is the honest precision figure.
    """

    total_decoys: int = 0
    wrongly_joined: int = 0
    error_rate_pct: float = 0.0
    by_kind: dict[str, dict[str, int]] = Field(default_factory=dict)


class UniquenessEvaluation(BaseModel):
    """The three numbers plus the score-spread report."""

    twin_recall: TwinRecall
    decoy_result: DecoyResult
    unlabelled_joins: int = 0
    score_spread: dict[str, int] = Field(default_factory=dict)
    strategy_scores: dict[str, dict[str, float]] = Field(default_factory=dict)


def _read_labels(labels_path: Path) -> list[DefectLabel]:
    """Read the ground-truth labels the injector wrote."""
    frame: pd.DataFrame = None
    labels: list[DefectLabel] = []
    row: dict = {}

    frame = pd.read_parquet(labels_path)
    for row in frame.to_dict(orient="records"):
        labels.append(DefectLabel(**{
            key: value for key, value in row.items() if pd.notna(value)
        }))
    return labels


def _read_decoys(decoys_path: Path) -> list[dict[str, Any]]:
    """Read the decoys the injector wrote. A missing file returns nothing."""
    if not decoys_path.exists():
        return []
    with decoys_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _twin_labels(labels: list[DefectLabel]) -> list[DefectLabel]:
    """Every uniqueness label the injector wrote, one per twin."""
    return [label for label in labels if label.dimension is Dimension.UNIQUENESS]


def _cluster_index(clusters: list[DuplicateCluster]) -> dict[str, str]:
    """Which cluster holds each record."""
    index: dict[str, str] = {}
    cluster: DuplicateCluster = None
    member: Any = None

    for cluster in clusters:
        for member in cluster.members:
            index[member.record_id] = cluster.cluster_id
    return index


def _score_twins(
    labels: list[DefectLabel],
    clusters: list[DuplicateCluster],
    frames: dict[str, Any] = None,
    blocking_keys: list[str] = None,
) -> TwinRecall:
    """Recall on the injected twins, overall and by strategy.

    A twin is matched when it lands in the SAME cluster as its source. Any
    other outcome (its own cluster, no cluster, a cluster with unrelated
    records only) is a miss.

    When the source MARA frame is given, this function also counts twins whose
    blocking key was corrupted after injection: those twins CANNOT match their
    source because the two land in different blocks. Both figures are reported.
    """
    index: dict[str, str] = _cluster_index(clusters)
    twin_labels: list[DefectLabel] = _twin_labels(labels)
    by_strategy: dict[str, dict[str, int]] = {}
    mara_by_matnr: dict[str, dict[str, Any]] = {}
    matched: int = 0
    hidden: int = 0
    matchable: int = 0
    twin_key: str = ""
    source_matnr: str = ""
    source_key: str = ""
    twin_matnr: str = ""
    strategy: str = ""
    detail: dict[str, Any] = {}
    hit: bool = False
    is_hidden: bool = False
    label: DefectLabel = None
    keys: list[str] = list(blocking_keys or [])

    if frames is not None and "MARA" in frames and keys:
        mara_by_matnr = {
            str(row["MATNR"]): row
            for row in frames["MARA"].to_dict(orient="records")
        }

    for label in twin_labels:
        detail = label.detail or {}
        strategy = str(detail.get("strategy", "unknown"))
        source_matnr = str(detail.get("duplicate_of", ""))
        twin_matnr = label.record_key.replace("MATNR=", "", 1)
        twin_key = label.record_key
        source_key = f"MATNR={source_matnr}"
        by_strategy.setdefault(strategy, {"total": 0, "matched": 0, "hidden": 0})
        by_strategy[strategy]["total"] += 1
        is_hidden = False
        if mara_by_matnr and twin_matnr in mara_by_matnr and source_matnr in mara_by_matnr:
            for key in keys:
                twin_value = str(mara_by_matnr[twin_matnr].get(key) or "").strip()
                source_value = str(mara_by_matnr[source_matnr].get(key) or "").strip()
                if not twin_value or not source_value or twin_value != source_value:
                    is_hidden = True
                    break
        if is_hidden:
            hidden += 1
            by_strategy[strategy]["hidden"] += 1
            continue
        matchable += 1
        hit = (twin_key in index and source_key in index
               and index[twin_key] == index[source_key])
        if hit:
            matched += 1
            by_strategy[strategy]["matched"] += 1
    return TwinRecall(
        total_twins=len(twin_labels),
        matched=matched,
        hidden_by_other_defect=hidden,
        matchable=matchable,
        recall_pct=round(100.0 * matched / len(twin_labels), 1) if twin_labels else 0.0,
        recall_on_matchable_pct=round(100.0 * matched / matchable, 1) if matchable else 0.0,
        by_strategy=by_strategy,
    )


def _score_decoys(
    decoys: list[dict[str, Any]], clusters: list[DuplicateCluster]
) -> DecoyResult:
    """Decoy error rate: how often the agent joined two different materials."""
    index: dict[str, str] = _cluster_index(clusters)
    by_kind: dict[str, dict[str, int]] = {}
    wrongly_joined: int = 0
    decoy: dict[str, Any] = {}
    kind: str = ""
    left_key: str = ""
    right_key: str = ""
    hit: bool = False

    for decoy in decoys:
        kind = str(decoy["kind"])
        left_key = f"MATNR={decoy['left_matnr']}"
        right_key = f"MATNR={decoy['right_matnr']}"
        by_kind.setdefault(kind, {"total": 0, "wrongly_joined": 0})
        by_kind[kind]["total"] += 1
        hit = (left_key in index and right_key in index
               and index[left_key] == index[right_key])
        if hit:
            wrongly_joined += 1
            by_kind[kind]["wrongly_joined"] += 1
    return DecoyResult(
        total_decoys=len(decoys),
        wrongly_joined=wrongly_joined,
        error_rate_pct=round(100.0 * wrongly_joined / len(decoys), 1) if decoys else 0.0,
        by_kind=by_kind,
    )


def _count_unlabelled_joins(
    labels: list[DefectLabel],
    decoys: list[dict[str, Any]],
    clusters: list[DuplicateCluster],
) -> int:
    """Pairs the agent joined that neither the labels nor the decoys knew about.

    A count, not a rate: some are the generator's residual accidents (Fix B
    catches most, not all), some may be real duplicates that carry no label
    yet. Reported as a number so a reader can see how much slack the honest
    numbers left uncovered.
    """
    known_pairs: set[frozenset] = set()
    joined_pairs: set[frozenset] = set()
    label: DefectLabel = None
    decoy: dict[str, Any] = {}
    cluster: DuplicateCluster = None
    member_a: Any = None
    member_b: Any = None
    unlabelled: int = 0

    for label in labels:
        if label.dimension is Dimension.UNIQUENESS:
            source_matnr = (label.detail or {}).get("duplicate_of", "")
            if source_matnr:
                known_pairs.add(frozenset({label.record_key, f"MATNR={source_matnr}"}))
    for decoy in decoys:
        known_pairs.add(frozenset({f"MATNR={decoy['left_matnr']}", f"MATNR={decoy['right_matnr']}"}))

    for cluster in clusters:
        for member_a in cluster.members:
            for member_b in cluster.members:
                if member_a.record_id < member_b.record_id:
                    joined_pairs.add(frozenset({member_a.record_id, member_b.record_id}))

    for pair in joined_pairs:
        if pair not in known_pairs:
            unlabelled += 1
    return unlabelled


def _summarise_strategy_scores(
    labels: list[DefectLabel], findings: list[Any]
) -> dict[str, dict[str, float]]:
    """Average score the agent gave each strategy's twins.

    Reads the score off the finding metadata, so no separate wiring is needed:
    every finding carries the score of its cluster member against the survivor.
    The report groups by strategy so Package 5 can see where each change lands.
    """
    finding_by_key: dict[str, Any] = {}
    summary: dict[str, dict[str, float]] = {}
    finding: Any = None
    strategy: str = ""
    score: float = 0.0
    label: DefectLabel = None

    for finding in findings:
        if finding.dimension is Dimension.UNIQUENESS:
            finding_by_key[finding.record_id] = finding
    for label in _twin_labels(labels):
        finding = finding_by_key.get(label.record_key)
        if finding is None:
            continue
        strategy = str((label.detail or {}).get("strategy", "unknown"))
        score = float(finding.metadata.get("score", 0.0))
        summary.setdefault(strategy, {"count": 0, "total_score": 0.0})
        summary[strategy]["count"] += 1
        summary[strategy]["total_score"] += score

    for strategy in list(summary):
        if summary[strategy]["count"]:
            summary[strategy]["average_score"] = round(
                summary[strategy]["total_score"] / summary[strategy]["count"], 3
            )
        else:
            summary[strategy]["average_score"] = 0.0
        summary[strategy]["count"] = int(summary[strategy]["count"])
        summary[strategy].pop("total_score", None)
    return summary


def evaluate_uniqueness(
    clusters: list[DuplicateCluster],
    findings: list[Any],
    labels_path: Path,
    decoys_path: Path,
    score_spread: dict[str, int] = None,
    frames: dict[str, Any] = None,
    blocking_keys: list[str] = None,
) -> UniquenessEvaluation:
    """Combine every number the matcher's evaluation cares about."""
    labels: list[DefectLabel] = _read_labels(labels_path)
    decoys: list[dict[str, Any]] = _read_decoys(decoys_path)

    return UniquenessEvaluation(
        twin_recall=_score_twins(labels, clusters, frames, blocking_keys),
        decoy_result=_score_decoys(decoys, clusters),
        unlabelled_joins=_count_unlabelled_joins(labels, decoys, clusters),
        score_spread=dict(score_spread or {}),
        strategy_scores=_summarise_strategy_scores(labels, findings),
    )


def print_evaluation(evaluation: UniquenessEvaluation) -> None:
    """Console output for the assessment runner."""
    recall: TwinRecall = evaluation.twin_recall
    decoys: DecoyResult = evaluation.decoy_result
    strategy: str = ""
    counts: dict[str, int] = {}

    print("\nUniqueness evaluation:")
    print(f"  Twin recall     : {recall.matched}/{recall.total_twins}"
          f" ({recall.recall_pct}%)")
    if recall.hidden_by_other_defect:
        print(f"  ... of which     : {recall.hidden_by_other_defect} twin(s) had their "
              f"blocking key corrupted and could NEVER match")
        print(f"  Recall on the    : {recall.matched}/{recall.matchable}"
              f" ({recall.recall_on_matchable_pct}%)   <- what the matcher could have caught")
        print(f"  matchable set")
    print(f"  Decoy errors    : {decoys.wrongly_joined}/{decoys.total_decoys}"
          f" ({decoys.error_rate_pct}%)   <- headline precision")
    print(f"  Unlabelled joins: {evaluation.unlabelled_joins}   <- a count, not a rate")
    print(f"  Score spread    : {evaluation.score_spread}")
    if recall.by_strategy:
        print("\n  Twin recall by change:")
        for strategy, counts in sorted(recall.by_strategy.items()):
            print(f"    {strategy:12} : {counts['matched']}/{counts['total']}")
    if evaluation.strategy_scores:
        print("\n  Average twin score by change:")
        for strategy, counts in sorted(evaluation.strategy_scores.items()):
            print(f"    {strategy:12} : {counts.get('average_score', 0.0)}"
                  f"  (over {counts.get('count', 0)} twins)")
    if decoys.by_kind:
        print("\n  Decoy errors by kind:")
        for kind, counts in sorted(decoys.by_kind.items()):
            print(f"    {kind:14} : {counts['wrongly_joined']}/{counts['total']}")
