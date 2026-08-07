# v0.2 | 04-Aug-2026 | Package 4d. A dimension can state its OWN denominator.
#                      Uniqueness is assessed on one table and holds records
#                      back, so the whole-run row count would dilute a real
#                      problem and count untested rows as clean.
# v0.1 | 27-Jun-2026 | Initial DQ scorecard, evaluation and console rendering

"""Scorecard and console reporting for an assessment run.

Turns a set of findings into a data quality scorecard a non-technical reader can
act on, and - when ground-truth labels are available - into a precision/recall
evaluation that demonstrates the method works. Rendering is plain ASCII so it
prints cleanly on any Windows console.

The score for a dimension is the share of records with no issue in that
dimension:

    score = 100 * (1 - affected_records / total_records)

so 100% means every record passed and lower numbers mean more records carry at
least one issue.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

import pandas as pd
from pydantic import BaseModel, Field

from src.contracts import DefectLabel, Finding

DIMENSION_ORDER: tuple[str, ...] = (
    "Completeness",
    "Validity",
    "Consistency",
    "Accuracy",
    "Timeliness",
    "Uniqueness",
)


class DimensionScore(BaseModel):
    """Score for one dimension across the records it was assessed over.

    total_records is that dimension's OWN denominator, which is not always the
    whole run. Uniqueness is assessed on one table and holds records back, so
    dividing its findings by every row of every table would dilute a real
    problem into near-invisibility.

    records_excluded counts the rows the dimension never checked. Without it a
    score looks better than it is, because an untested record would otherwise
    count as a clean one.
    """

    dimension: str
    total_records: int
    findings: int
    affected_records: int
    score_pct: float
    records_excluded: int = 0  # v0.2


class Scorecard(BaseModel):
    """The full scorecard for an assessment run."""

    total_records: int
    total_findings: int
    overall_score_pct: float
    by_dimension: dict[str, DimensionScore] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)
    top_fields: list[tuple[str, int]] = Field(default_factory=list)


class DimensionEval(BaseModel):
    """Precision/recall/F1 for one dimension against ground truth."""

    dimension: str
    true_positive: int
    false_positive: int
    false_negative: int
    precision: float
    recall: float
    f1: float


def compute_scorecard(
    findings: list[Finding],
    frames: dict[str, pd.DataFrame],
    dimensions: list[str],
    dimension_totals: Optional[dict[str, dict[str, int]]] = None,  # v0.2
) -> Scorecard:
    """Build a scorecard from findings and the assessed frames.

    dimension_totals lets one dimension state its OWN denominator, as
    {'Uniqueness': {'assessed': 2440, 'excluded': 431}}. A dimension that says
    nothing keeps the whole-run row count, which is right for the rule-backed
    agents because they check every loaded table.
    """
    total_records: int = sum(int(frame.shape[0]) for frame in frames.values())
    totals: dict[str, dict[str, int]] = dimension_totals or {}  # v0.2
    denominator: int = 0  # v0.2
    excluded: int = 0  # v0.2
    by_dimension: dict[str, DimensionScore] = {}
    by_severity: Counter = Counter()
    field_counter: Counter = Counter()
    finding: Finding = None
    dimension: str = ""
    dimension_findings: list[Finding] = []
    affected: set = set()
    score: float = 0.0
    scores_for_overall: list[float] = []

    for finding in findings:
        by_severity[finding.severity.value] += 1
        field_counter[f"{finding.table}.{finding.field} ({finding.dimension.value})"] += 1

    for dimension in dimensions:
        dimension_findings = [f for f in findings if f.dimension.value == dimension]
        affected = {(f.table, f.record_id) for f in dimension_findings}
        denominator = int(totals.get(dimension, {}).get("assessed", total_records))  # v0.2
        excluded = int(totals.get(dimension, {}).get("excluded", 0))  # v0.2
        score = 100.0 * (1.0 - len(affected) / denominator) if denominator else 100.0  # v0.2
        by_dimension[dimension] = DimensionScore(
            dimension=dimension,
            total_records=denominator,  # v0.2
            records_excluded=excluded,  # v0.2
            findings=len(dimension_findings),
            affected_records=len(affected),
            score_pct=round(score, 1),
        )
        scores_for_overall.append(score)

    overall: float = round(sum(scores_for_overall) / len(scores_for_overall), 1) if scores_for_overall else 100.0

    return Scorecard(
        total_records=total_records,
        total_findings=len(findings),
        overall_score_pct=overall,
        by_dimension=by_dimension,
        by_severity=dict(by_severity),
        top_fields=field_counter.most_common(8),
    )


def evaluate_against_labels(
    findings: list[Finding],
    labels: list[DefectLabel],
    dimensions: list[str],
) -> dict[str, DimensionEval]:
    """Score findings against ground-truth labels per dimension."""
    result: dict[str, DimensionEval] = {}
    dimension: str = ""
    found: set = set()
    labelled: set = set()
    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0

    for dimension in dimensions:
        found = {(f.rule_id, f.record_id) for f in findings if f.dimension.value == dimension}
        labelled = {(d.rule_id, d.record_key) for d in labels if d.dimension.value == dimension}
        tp = len(found & labelled)
        fp = len(found - labelled)
        fn = len(labelled - found)
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        recall = tp / (tp + fn) if (tp + fn) else 1.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        result[dimension] = DimensionEval(
            dimension=dimension,
            true_positive=tp,
            false_positive=fp,
            false_negative=fn,
            precision=round(precision, 3),
            recall=round(recall, 3),
            f1=round(f1, 3),
        )
    return result


def print_scorecard(scorecard: Scorecard, dataset_label: str) -> None:
    """Render the scorecard to the console in plain ASCII."""
    dimension: str = ""
    score: DimensionScore = None
    label: str = ""
    count: int = 0
    field: str = ""

    print("")
    print("=" * 60)
    print("AgentDQ - Data Quality Scorecard")
    print(f"Dataset: {dataset_label}")
    print(f"Records assessed: {scorecard.total_records}")
    print("=" * 60)

    print(f"\n{'Dimension':<16}{'Findings':>10}{'Affected':>10}{'Score':>9}")
    print("-" * 45)
    for dimension in DIMENSION_ORDER:
        if dimension not in scorecard.by_dimension:
            continue
        score = scorecard.by_dimension[dimension]
        print(f"{dimension:<16}{score.findings:>10}{score.affected_records:>10}{score.score_pct:>8}%")
    print("-" * 45)
    print(f"{'Overall DQ score':<16}{'':>20}{scorecard.overall_score_pct:>8}%")

    if scorecard.by_severity:
        print("\nFindings by severity:")
        for label, count in sorted(scorecard.by_severity.items()):
            print(f"  {label:<10}{count}")

    if scorecard.top_fields:
        print("\nTop offending fields:")
        for field, count in scorecard.top_fields:
            print(f"  {count:>6}  {field}")


def print_evaluation(evaluation: dict[str, DimensionEval]) -> None:
    """Render the ground-truth evaluation to the console."""
    dimension: str = ""
    row: DimensionEval = None

    print("\n" + "=" * 60)
    print("Evaluation against ground truth (synthetic data)")
    print("=" * 60)
    print(f"{'Dimension':<16}{'TP':>7}{'FP':>6}{'FN':>6}{'Prec':>8}{'Rec':>8}{'F1':>8}")
    print("-" * 59)
    for dimension in DIMENSION_ORDER:
        if dimension not in evaluation:
            continue
        row = evaluation[dimension]
        print(f"{dimension:<16}{row.true_positive:>7}{row.false_positive:>6}"
              f"{row.false_negative:>6}{row.precision:>8}{row.recall:>8}{row.f1:>8}")


def print_examples(findings: list[Finding], per_dimension: int = 3) -> None:
    """Print a few example findings per dimension for a human sanity check."""
    seen: Counter = Counter()
    finding: Finding = None

    print("\nExample findings:")
    for finding in findings:
        key: str = finding.dimension.value
        if seen[key] >= per_dimension:
            continue
        seen[key] += 1
        print(f"  [{key}] {finding.record_id} - {finding.issue}")
