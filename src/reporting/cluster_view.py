# ---------------------------------------------------------------------------
# src/reporting/cluster_view.py
# v1.0 | 10-Aug-2026 | Package 4f. Turns clusters, candidate pairs and resolved
#                      uniqueness settings into display tables. Pure: pandas
#                      and the contracts only, no Streamlit, so every shape here
#                      is testable offline. The dashboard imports this module
#                      and renders what it returns.
# ---------------------------------------------------------------------------
"""What a person needs to see about a duplicate, and nothing more.

A cluster carries record keys and scores. A record key tells a reader nothing
about why two rows look alike, so this module puts the ORIGINAL description
beside each member. The matcher compares normalised text; a reader must see the
text as it stands in the table, because that is the text a steward will judge.

Three groups of function live here:

    clusters      the cluster list, and the members of one cluster
    candidates    the pairs in the uncertain band, which wait for the
                  adjudicator in Package 4g
    settings      the bands, keys, weights and methods in force, read-only

Nothing here decides anything. Every number was worked out by the agent.
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from src.agents.embedding_store import LANGUAGE_FIELD, resolve_compare_field
from src.contracts import ClusterMember, DuplicateCluster
from src.data.schema import COMPARE_LANGUAGE, TableSchema

# What a reader is told when a record has no text for a compare field.
NO_TEXT: str = "(no text)"


def _block_label(blocking_values: dict[str, str]) -> str:
    """One readable string for the block a cluster sits in."""
    parts: list[str] = []
    name: str = ""
    value: Any = None

    for name, value in sorted((blocking_values or {}).items()):
        parts.append(f"{name}={value}")
    return ", ".join(parts)


def cluster_overview_frame(clusters: list[DuplicateCluster]) -> pd.DataFrame:
    """One row per cluster: the list a reader scans first.

    Weakest link is shown beside size on purpose. A cluster of three whose
    weakest pair scores 0.30 is held together by a chain, and it looks as tight
    as a cluster whose members all match each other until that number is read.
    """
    rows: list[dict[str, Any]] = []
    cluster: Optional[DuplicateCluster] = None

    for cluster in clusters or []:
        rows.append({
            "Cluster": cluster.cluster_id,
            "Table": cluster.table,
            "Records": cluster.size,
            "Block": _block_label(cluster.blocking_values),
            "Weakest link": cluster.weakest_link,
            "Keep": cluster.survivor_id,
            "Why": cluster.survivor_reason.value,
            "Resolution": cluster.resolution.value,
            "Mode": cluster.mode.value,
        })
    return pd.DataFrame(rows)


def compare_field_texts(
    frames: dict[str, pd.DataFrame],
    schemas: dict[str, TableSchema],
    subject: str,
    language: str = COMPARE_LANGUAGE,
) -> dict[str, dict[str, str]]:
    """The ORIGINAL text of every compare field, keyed by subject record key.

    The matcher works on MARA rows and compares MAKT text, so a MAKT value is
    filed under the MARA key. The language filter matches the one the matcher
    uses, so the text on screen is the text that was scored.

    The value is taken as written. Normalisation belongs to the matcher; a
    steward has to see what is really in the field.
    """
    texts: dict[str, dict[str, str]] = {}
    schema: Optional[TableSchema] = schemas.get(subject)
    entry: Any = None
    source_table: str = ""
    field: str = ""
    working: pd.DataFrame = None
    key_fields: list[str] = []
    missing: list[str] = []
    key_field: str = ""
    row: dict[str, Any] = {}
    record_key: str = ""
    value: Any = None

    if schema is None or schema.uniqueness is None:
        return texts
    key_fields = list(schema.primary_key)

    for entry in schema.uniqueness.compare_fields:
        source_table, field = resolve_compare_field(subject, entry.field)
        if source_table not in frames:
            continue
        working = frames[source_table]
        if field not in working.columns:
            continue
        missing = [key_field for key_field in key_fields if key_field not in working.columns]
        if missing:
            continue
        if LANGUAGE_FIELD in working.columns:
            working = working[working[LANGUAGE_FIELD].astype(str).str.strip() == language]
        texts[entry.field] = {}
        for row in working.to_dict(orient="records"):
            record_key = "|".join(f"{key_field}={row.get(key_field)}" for key_field in key_fields)
            value = row.get(field)
            if value is None or str(value).strip() == "" or str(value).strip().lower() == "nan":
                continue
            texts[entry.field][record_key] = str(value)
    return texts


def cluster_members_frame(
    cluster: DuplicateCluster,
    texts: Optional[dict[str, dict[str, str]]] = None,
) -> pd.DataFrame:
    """The members of one cluster, survivor first, with their descriptions.

    Score is measured against the survivor. "Below band" marks a member that
    joined through a chain rather than by matching the survivor directly, which
    is the member a steward should look at hardest.
    """
    rows: list[dict[str, Any]] = []
    by_field: dict[str, dict[str, str]] = texts or {}
    survivors: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    member: Optional[ClusterMember] = None
    entry: dict[str, Any] = {}
    field_label: str = ""

    for member in cluster.members:
        entry = {
            "Record": member.record_id,
            "Keep": "yes" if member.is_survivor else "no",
            "Score vs survivor": member.score,
            "Below band": "yes" if member.below_band else "no",
            "Mandatory filled": f"{member.populated_mandatory}/{member.populated_total}",
        }
        for field_label in by_field:
            entry[field_label] = by_field[field_label].get(member.record_id, NO_TEXT)
        if member.is_survivor:
            survivors.append(entry)
        else:
            others.append(entry)

    others.sort(key=_member_score, reverse=True)
    rows = survivors + others
    return pd.DataFrame(rows)


def _member_score(entry: dict[str, Any]) -> float:
    """Sort key for the member listing. A named function, not a lambda."""
    return float(entry.get("Score vs survivor", 0.0))


def candidate_pairs_frame(
    pairs: list[dict[str, Any]],
    texts: Optional[dict[str, dict[str, str]]] = None,
) -> pd.DataFrame:
    """The uncertain band: pairs the scores could not settle.

    These are NOT clustered and they raise no finding. They are the work the
    adjudicator takes on in Package 4g, and the number of them is the cost
    control of the whole design, because model calls scale with real ambiguity
    rather than with dataset size.
    """
    rows: list[dict[str, Any]] = []
    by_field: dict[str, dict[str, str]] = texts or {}
    pair: dict[str, Any] = {}
    entry: dict[str, Any] = {}
    field_label: str = ""

    for pair in pairs or []:
        entry = {
            "Table": pair.get("table", ""),
            "Record A": pair.get("left_id", ""),
            "Record B": pair.get("right_id", ""),
            "Score": pair.get("score", 0.0),
            "Block": _block_label(pair.get("blocking_values", {})),
        }
        for field_label in by_field:
            entry[f"A: {field_label}"] = by_field[field_label].get(str(pair.get("left_id", "")), NO_TEXT)
            entry[f"B: {field_label}"] = by_field[field_label].get(str(pair.get("right_id", "")), NO_TEXT)
        rows.append(entry)
    return pd.DataFrame(rows)


def score_spread_frame(summary: dict[str, Any]) -> pd.DataFrame:
    """How the scored pairs fell across the three outcomes."""
    spread: dict[str, Any] = (summary or {}).get("score_spread", {}) or {}
    meanings: dict[str, str] = {
        "duplicate": "at or above the duplicate band, so joined into a cluster",
        "uncertain": "in the review band, so held for the adjudicator",
        "below": "below the review band, so treated as different records",
    }
    rows: list[dict[str, Any]] = []
    name: str = ""

    for name in ("duplicate", "uncertain", "below"):
        if name not in spread:
            continue
        rows.append({
            "Outcome": name,
            "Pairs": int(spread[name]),
            "What it means": meanings[name],
        })
    return pd.DataFrame(rows)


def held_back_frame(summary: dict[str, Any]) -> pd.DataFrame:
    """The records that took no part in deduplication, and why.

    A held-back record is not a clean record. It was never compared, so the
    count belongs on screen beside the score.
    """
    reasons: dict[str, Any] = (summary or {}).get("held_back", {}) or {}
    rows: list[dict[str, Any]] = []
    reason: str = ""

    for reason in sorted(reasons):
        rows.append({"Reason": reason, "Records": int(reasons[reason])})
    return pd.DataFrame(rows)


def match_mode_note(summary: dict[str, Any]) -> tuple[str, str]:
    """A level and a message describing which rungs of the ladder ran.

    Returns "ok" when both the fuzzy and the semantic rungs ran, and "warn" when
    only the fuzzy rung did. A fuzzy-only run that looks like a full one is the
    failure this note exists to prevent.
    """
    mode: str = str((summary or {}).get("mode", ""))
    reason: str = str((summary or {}).get("mode_reason", "") or "")

    if not mode:
        return "warn", "The uniqueness stage did not report a match mode."
    if mode == "full":
        return "ok", "Full match: the letter-by-letter and the meaning-based rungs both ran."
    return "warn", (
        "Fuzzy only: descriptions were compared letter by letter and NOT by meaning, "
        "so pairs that say the same thing in different words were missed. "
        f"Reason: {reason or 'not recorded'}. "
        "Build the vectors with tools/build_embeddings.py to run the full ladder."
    )


def settings_rows(resolved: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, str]]:
    """The uniqueness settings in force, as rows a person can read.

    Read-only on purpose. The schema YAML files are generated by
    tools/build_schema.py, so a screen that wrote back to them would lose its
    writes on the next rebuild. Editing arrives with the settings screen in
    Package 7, which has somewhere safe to write.
    """
    bands: dict[str, Any] = (resolved or {}).get("bands", {}) or {}
    weights: dict[str, Any] = (resolved or {}).get("compare_weights", {}) or {}
    methods: dict[str, Any] = (resolved or {}).get("method_weights", {}) or {}
    keys: list[str] = list((resolved or {}).get("blocking_keys", []) or [])
    rows: list[dict[str, str]] = []
    field_name: str = ""
    shift: Any = bands.get("shift", 0.0)

    rows.append({
        "Setting": "Blocking keys",
        "In force": ", ".join(keys) or "(none)",
        "What it means": "two records are compared only when they agree exactly on all of these",
    })
    rows.append({
        "Setting": "Duplicate band",
        "In force": str(bands.get("duplicate", "-")),
        "What it means": f"steward set {bands.get('steward_duplicate', '-')}; advisories moved it by {shift}",
    })
    rows.append({
        "Setting": "Review band floor",
        "In force": str(bands.get("review_low", "-")),
        "What it means": f"steward set {bands.get('steward_review_low', '-')}; pairs above this and below "
                         f"the duplicate band wait for the adjudicator",
    })
    for field_name in sorted(weights):
        rows.append({
            "Setting": f"Compare field: {field_name}",
            "In force": str(round(float(weights[field_name]), 4)),
            "What it means": "this field's share of the record score",
        })
    rows.append({
        "Setting": "Fuzzy method",
        "In force": str((resolved or {}).get("fuzzy_metric", "-")),
        "What it means": f"letter-by-letter comparison, weighted {methods.get('fuzzy', '-')}",
    })
    rows.append({
        "Setting": "Semantic model",
        "In force": str((resolved or {}).get("semantic_model", "-")),
        "What it means": f"meaning-based comparison, weighted {methods.get('semantic', '-')}",
    })
    rows.append({
        "Setting": "Block size ceiling",
        "In force": f"{int((resolved or {}).get('max_block_pairs', 0)):,} pairs",
        "What it means": "a block above this is held back whole rather than compared",
    })
    rows.append({
        "Setting": "Records compared",
        "In force": str((summary or {}).get("records_assessed", "-")),
        "What it means": "the denominator of the uniqueness score; held-back records are not in it",
    })
    rows.append({
        "Setting": "Records held back",
        "In force": str((summary or {}).get("held_back_total", 0)),
        "What it means": "records with no usable evidence, which took no part in matching",
    })
    rows.append({
        "Setting": "Settings code",
        "In force": str((resolved or {}).get("fingerprint", "-")),
        "What it means": "a short code for this exact set of dials, so two runs can be compared",
    })
    return rows


def oversized_blocks_frame(summary: dict[str, Any]) -> pd.DataFrame:
    """Blocks that were too large to compare, named one by one.

    Comparison inside a block is all-pairs, so cost is n(n-1)/2. A block past
    the ceiling is held back whole and its records leave the denominator. A
    steward has to know WHICH block went unchecked, because 'the score is over
    the rest' is only useful with the rest named.
    """
    blocks: list[dict[str, Any]] = (summary or {}).get("oversized_blocks", []) or []
    rows: list[dict[str, Any]] = []
    entry: dict[str, Any] = {}

    for entry in blocks:
        rows.append({
            "Block": _block_label(entry.get("block", {})),
            "Records": entry.get("records", 0),
            "Pairs needed": f"{int(entry.get('pairs', 0)):,}",
            "Ceiling": f"{int(entry.get('ceiling', 0)):,}",
        })
    return pd.DataFrame(rows)


def advisory_lines(settings: dict[str, Any]) -> list[str]:
    """The advice other agents sent to the uniqueness stage, one line each."""
    return list((settings or {}).get("readable", []) or [])


def twin_recall_frame(evaluation: Any) -> pd.DataFrame:
    """Recall on the injected twins, broken down by the change that made them."""
    rows: list[dict[str, Any]] = []
    by_strategy: dict[str, Any] = {}
    scores: dict[str, Any] = {}
    strategy: str = ""
    counts: dict[str, Any] = {}

    if evaluation is None:
        return pd.DataFrame(rows)
    by_strategy = evaluation.twin_recall.by_strategy or {}
    scores = evaluation.strategy_scores or {}
    for strategy in sorted(by_strategy):
        counts = by_strategy[strategy]
        rows.append({
            "Change": strategy,
            "Found": counts.get("matched", 0),
            "Total": counts.get("total", 0),
            "Average score": scores.get(strategy, {}).get("average_score", "-"),
        })
    return pd.DataFrame(rows)


def decoy_frame(evaluation: Any) -> pd.DataFrame:
    """Decoy errors by kind: the headline precision figure, broken out."""
    rows: list[dict[str, Any]] = []
    by_kind: dict[str, Any] = {}
    kind: str = ""
    counts: dict[str, Any] = {}

    if evaluation is None:
        return pd.DataFrame(rows)
    by_kind = evaluation.decoy_result.by_kind or {}
    for kind in sorted(by_kind):
        counts = by_kind[kind]
        rows.append({
            "Decoy kind": kind,
            "Wrongly joined": counts.get("wrongly_joined", 0),
            "Total": counts.get("total", 0),
        })
    return pd.DataFrame(rows)
