# ---------------------------------------------------------------------------
# src/agents/uniqueness.py
# v1.0 | 04-Aug-2026 | Package 4d. The matcher. Blocks on exact agreement, scores
#                      the remaining pairs with the fuzzy and semantic rungs,
#                      chains matched pairs into clusters, and recommends a
#                      survivor. No language model: pairs that the scores cannot
#                      settle are held as candidates for the adjudicator (4g).
# ---------------------------------------------------------------------------
"""Are these records the same thing?

Every other agent asks "is this row valid?" and answers from a rule. This one
compares records against each other and answers with a score, because two
records that describe one material rarely hold one identical value.

The work runs in five stages:

    1. HOLD BACK   Records with no usable evidence take no part. Three reasons:
                   a validity finding on a compare field or a blocking key, a
                   description that normalises to nothing, and a missing
                   blocking value.
    2. BLOCK       Only records that agree EXACTLY on every blocking key are
                   ever compared. MARA blocks on MTART and MEINS, so a bolt is
                   never proposed as a duplicate of a coil.
    3. SCORE       Fuzzy compares letters. Semantic compares meaning. The two
                   are combined with the weights the steward set.
    4. CLUSTER     Pairs at or above the duplicate band are joined. Chaining
                   applies: if A matches B and B matches C, all three form one
                   group. Pairs in the uncertain band are NOT clustered. They
                   wait for the adjudicator.
    5. SURVIVE     The cluster recommends one record to keep. It never merges.

What this agent does NOT do is decide whether it was right. The injected ground
truth labels a twin but holds no opinion on which record deserves to survive, so
scoring the survivor choice would measure a business judgement against a label
that has no view on it. Cluster-level precision and recall arrive in Package 4e.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz.distance import JaroWinkler

from src.agents.base import AgentResult, BaseAgent
from src.agents.embedding_store import (
    DEFAULT_MODEL,
    collect_texts,
    load_verified,
    resolve_compare_field,
    subject_tables,
)
from src.agents.uniqueness_settings import excluded_record_keys, resolve_settings
from src.contracts import (
    ClusterMember,
    ClusterResolution,
    Dimension,
    DuplicateCluster,
    Finding,
    MatchMode,
    RuleSpec,
    Severity,
    SurvivorReason,
)
from src.data.schema import COMPARE_LANGUAGE, TableSchema
from src.rules.executor import PandasRuleExecutor

# All pairs inside one block is n(n-1)/2. The largest block in the synthetic
# degraded dataset holds 2,632 rows, which is 3.46 million pairs and a few
# seconds of work. This guard stops a customer-sized block from appearing to
# hang: a clear error naming the block is far better than a run that never ends.
MAX_BLOCK_PAIRS: int = 5_000_000  # v1.0

# Reasons a record takes no part in matching. Kept apart so a steward can tell
# "the description is junk" from "the material type is junk".
HELD_VALIDITY: str = "validity_failed"
HELD_NO_TEXT: str = "no_description"
HELD_NO_BLOCK: str = "no_blocking_key"

# Every fuzzy metric is brought to the same 0 to 1 scale here, because rapidfuzz
# returns 0 to 100 for some scorers and 0 to 1 for others.
_FUZZY_SCORERS: dict[str, tuple[Any, float]] = {
    "jaro_winkler": (JaroWinkler.normalized_similarity, 1.0),
    "ratio": (fuzz.ratio, 100.0),
    "token_sort_ratio": (fuzz.token_sort_ratio, 100.0),
    "token_set_ratio": (fuzz.token_set_ratio, 100.0),
}


class _Groups:
    """Union-find: joins records that match, directly or through a chain.

    If A matches B and B matches C, all three end in one group even when A and C
    were never a match themselves.
    """

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, item: int) -> None:
        if item not in self.parent:
            self.parent[item] = item

    def find(self, item: int) -> int:
        root: int = item
        walker: int = item

        self.add(item)
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[walker] != root:
            self.parent[walker], walker = root, self.parent[walker]
        return root

    def join(self, left: int, right: int) -> None:
        left_root: int = self.find(left)
        right_root: int = self.find(right)

        if left_root != right_root:
            self.parent[right_root] = left_root

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        item: int = 0

        for item in sorted(self.parent):
            out.setdefault(self.find(item), []).append(item)
        return out


class UniquenessAgent(BaseAgent):
    """Finds groups of records that appear to describe one thing.

    advisories and prior_findings come from the upstream agents. The agent
    resolves them itself rather than being handed resolved settings, so it can
    be run and tested on its own.

    data_dir names the dataset the vectors were built for. With no data_dir, or
    with a vector file that fails its checks, the agent scores with the fuzzy
    rung alone and records that on every finding.
    """

    dimension = Dimension.UNIQUENESS
    name = "Uniqueness Agent"

    def __init__(
        self,
        advisories: Optional[list[dict[str, Any]]] = None,
        prior_findings: Optional[list[Finding]] = None,
        data_dir: Optional[str] = None,
        model_name: str = DEFAULT_MODEL,
        language: str = COMPARE_LANGUAGE,
    ) -> None:
        self.advisories = list(advisories or [])
        self.prior_findings = list(prior_findings or [])
        self.data_dir = data_dir
        self.model_name = model_name
        self.language = language
        # Filled during run() so a caller can read them afterwards.
        self.settings: dict[str, Any] = {}
        self.held_back: dict[str, str] = {}
        self.score_spread: dict[str, int] = {}
        self.candidate_pairs: list[dict[str, Any]] = []
        self.mode: MatchMode = MatchMode.FULL
        self.mode_reason: str = ""

    # -- stage 1: what takes no part --------------------------------------

    def _to_subject_key(self, record_key: str, subject_schema: TableSchema) -> str:
        """Rewrite a record key from another table into the subject's key.

        A validity finding on MAKT.MAKTX names its record as
        'MATNR=...|SPRAS=E', because MAKT is keyed on both. The subject MARA is
        keyed on MATNR alone. Without this step the two never match and every
        exclusion driven by a description would be lost in silence.
        """
        wanted: set[str] = set(subject_schema.primary_key)
        parts: list[str] = []
        part: str = ""

        for part in record_key.split("|"):
            if "=" in part and part.split("=", 1)[0] in wanted:
                parts.append(part)
        if not parts:
            return record_key
        return "|".join(parts)

    def _validity_excluded(self, subject_schema: TableSchema) -> set[str]:
        """Record keys held back because a validity check failed on a field this
        agent depends on.

        BOTH kinds of field count, and for different reasons.

        A junk description gives nothing to compare. A junk material type or
        unit of measure is worse: it puts the record in the WRONG BLOCK, so its
        true duplicate is never even considered. Left in, such a record would be
        reported as unique when in truth it was never checked at all.
        """
        excluded: dict[str, set[str]] = {}
        keys: set[str] = set()
        table_keys: set[str] = set()
        record_key: str = ""

        excluded = excluded_record_keys(self.settings, self.prior_findings)
        for table_keys in excluded.values():
            for record_key in table_keys:
                keys.add(self._to_subject_key(record_key, subject_schema))
        return keys

    # -- stage 2: the text each record is compared on ----------------------

    def _text_map(
        self,
        frames: dict[str, pd.DataFrame],
        subject_schema: TableSchema,
        source_table: str,
        field: str,
    ) -> tuple[dict[str, str], list[str], list[str]]:
        """Normalised text per subject record key, for one compare field.

        The keys and texts are also returned in the builder's own order, because
        the content code that guards the vector file is calculated over exactly
        that set. Any filtering must happen AFTER the check, or a correct file
        would be rejected.
        """
        keys: list[str] = []
        texts: list[str] = []
        mapping: dict[str, str] = {}
        position: int = 0

        keys, texts, _, _ = collect_texts(
            frames[source_table], subject_schema, field, self.language
        )
        for position in range(len(keys)):
            mapping[keys[position]] = texts[position]
        return mapping, keys, texts

    # -- stage 3: scoring --------------------------------------------------

    def _fuzzy_matrix(self, texts: list[str], metric: str) -> np.ndarray:
        """Every text against every text, on a 0 to 1 scale."""
        scorer: Any = None
        scale: float = 1.0
        matrix: np.ndarray = None

        scorer, scale = _FUZZY_SCORERS[metric]
        matrix = process.cdist(texts, texts, scorer=scorer, workers=-1, dtype=np.float32)
        if scale != 1.0:
            matrix = matrix / np.float32(scale)
        return matrix

    def _semantic_matrix(self, rows: list[int], vectors: np.ndarray) -> np.ndarray:
        """Every vector against every vector.

        The stored vectors are already unit length, so the similarity is one
        multiply-and-add rather than a division on every pair.
        """
        block_vectors: np.ndarray = vectors[rows, :]
        return np.clip(block_vectors @ block_vectors.T, -1.0, 1.0).astype(np.float32)

    # -- stage 5: who survives ---------------------------------------------

    def _completeness(self, row: dict[str, Any], schema: TableSchema) -> tuple[int, int]:
        """Count the populated fields of one record: mandatory first, then all.

        Mandatory fields rank first because a record full of optional detail is
        not more useful than one that carries the fields the business requires.
        """
        mandatory: int = 0
        total: int = 0
        field_name: str = ""
        spec: Any = None
        value: Any = None

        for field_name, spec in schema.fields.items():
            value = row.get(field_name)
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue
            if str(value).strip() == "":
                continue
            total += 1
            if getattr(spec, "mandatory", False):
                mandatory += 1
        return mandatory, total

    def _choose_survivor(
        self,
        member_keys: list[str],
        texts: dict[str, str],
        rows_by_key: dict[str, dict[str, Any]],
        schema: TableSchema,
    ) -> tuple[str, SurvivorReason, ClusterResolution, dict[str, tuple[int, int]]]:
        """Recommend the record to keep, and say whether a person must confirm.

        Rule (a): if every description matches exactly after normalisation, any
        member may survive, so the choice is arbitrary and the lowest key is
        taken to keep the output reproducible.

        Rule (b): otherwise the most complete record survives. If two records
        tie on that measure, no recommendation is made and the cluster goes to a
        steward.
        """
        counts: dict[str, tuple[int, int]] = {}
        distinct: set[str] = set()
        ranked: list[tuple[int, int, str]] = []
        best: tuple[int, int, str] = (0, 0, "")
        runner_up: tuple[int, int, str] = (0, 0, "")
        key: str = ""

        for key in member_keys:
            counts[key] = self._completeness(rows_by_key.get(key, {}), schema)
            distinct.add(texts.get(key, ""))

        if len(distinct) == 1:
            return sorted(member_keys)[0], SurvivorReason.IDENTICAL, ClusterResolution.AUTOMATIC, counts

        for key in member_keys:
            ranked.append((counts[key][0], counts[key][1], key))
        ranked.sort(reverse=True)
        best = ranked[0]
        runner_up = ranked[1]
        if (best[0], best[1]) == (runner_up[0], runner_up[1]):
            # A tie on completeness. Name a reference point so member scores can
            # still be shown, but make no recommendation.
            return (
                sorted(member_keys)[0],
                SurvivorReason.NONE,
                ClusterResolution.NEEDS_STEWARD,
                counts,
            )
        return best[2], SurvivorReason.MOST_COMPLETE, ClusterResolution.AUTOMATIC, counts

    # -- the run -----------------------------------------------------------

    def run(
        self,
        frames: dict[str, pd.DataFrame],
        schemas: dict[str, TableSchema],
        rules: list[RuleSpec],
    ) -> AgentResult:
        """Assess one subject table for duplicates. rules is not used: this
        dimension asks a question no rule can express."""
        subjects: list[str] = subject_tables(schemas)
        subject: str = ""
        schema: TableSchema = None
        frame: pd.DataFrame = None
        result: AgentResult = None

        if not subjects:
            return self._empty_result("no table declares compare fields")
        subject = subjects[0]
        schema = schemas[subject]
        frame = frames.get(subject)
        if frame is None:
            return self._empty_result(f"{subject} was not loaded")

        self.settings = resolve_settings(schema.uniqueness, self.advisories)
        result = self._assess(subject, schema, schemas, frames, frame)
        return result

    def _empty_result(self, reason: str) -> AgentResult:
        self.mode_reason = reason
        return AgentResult(dimension=self.dimension, agent=self.name, rules_run=0)

    def _assess(
        self,
        subject: str,
        schema: TableSchema,
        schemas: dict[str, TableSchema],
        frames: dict[str, pd.DataFrame],
        frame: pd.DataFrame,
    ) -> AgentResult:
        blocking_keys: list[str] = list(schema.uniqueness.blocking_keys)
        compare_weights: dict[str, float] = self.settings["compare_weights"]
        bands: dict[str, float] = self.settings["bands"]
        working: pd.DataFrame = frame
        texts_by_field: dict[str, dict[str, str]] = {}
        vectors_by_field: dict[str, tuple[dict[str, int], np.ndarray]] = {}
        rows_by_key: dict[str, dict[str, Any]] = {}
        combined_text: dict[str, str] = {}
        excluded_by_validity: set[str] = set()
        candidate_keys: list[str] = []
        blocks: dict[tuple, list[str]] = {}
        clusters: list[DuplicateCluster] = []
        findings: list[Finding] = []
        entry: Any = None
        source_table: str = ""
        field: str = ""
        record_key: str = ""
        row: dict[str, Any] = {}
        block_key: tuple = ()
        value: Any = None
        key_field: str = ""
        missing_block: bool = False

        self.held_back = {}
        self.candidate_pairs = []
        self.score_spread = {"duplicate": 0, "uncertain": 0, "below": 0}

        # -- scope: an optional row filter, written in the same predicate IR
        #    the rules use, so no new expression language was invented.
        if schema.uniqueness.scope is not None:
            working = working[PandasRuleExecutor(schema).evaluate(schema.uniqueness.scope, working)]

        for row in working.to_dict(orient="records"):
            rows_by_key[schema.record_key(row)] = row

        # -- the text for every compare field, and its vectors
        for entry in schema.uniqueness.compare_fields:
            source_table, field = resolve_compare_field(subject, entry.field)
            if source_table not in frames or field not in frames[source_table].columns:
                continue
            mapping, keys, texts = self._text_map(frames, schema, source_table, field)
            texts_by_field[entry.field] = mapping
            if self.data_dir:
                lookup, vectors, reason = load_verified(
                    self.data_dir, source_table, field, keys, texts,
                    self.model_name, self.language,
                )
                if reason:
                    self.mode = MatchMode.FUZZY_ONLY
                    self.mode_reason = reason
                else:
                    vectors_by_field[entry.field] = (lookup, vectors)
            else:
                self.mode = MatchMode.FUZZY_ONLY
                self.mode_reason = "no dataset directory given, so no vectors were read"

        if not texts_by_field:
            return self._empty_result("no compare field could be read")
        if self.mode is MatchMode.FUZZY_ONLY:
            vectors_by_field = {}

        excluded_by_validity = self._validity_excluded(schema)

        # -- stage 1: hold back the records with no usable evidence
        for record_key in rows_by_key:
            if record_key in excluded_by_validity:
                self.held_back[record_key] = HELD_VALIDITY
                continue
            combined_text[record_key] = " ".join(
                texts_by_field[name].get(record_key, "") for name in texts_by_field
            ).strip()
            if not combined_text[record_key]:
                self.held_back[record_key] = HELD_NO_TEXT
                continue
            missing_block = False
            for key_field in blocking_keys:
                value = rows_by_key[record_key].get(key_field)
                if value is None or str(value).strip() == "" or str(value).strip().lower() == "nan":
                    missing_block = True
            if missing_block:
                self.held_back[record_key] = HELD_NO_BLOCK
                continue
            candidate_keys.append(record_key)

        # -- stage 2: block
        for record_key in candidate_keys:
            block_key = tuple(
                str(rows_by_key[record_key].get(key_field)).strip() for key_field in blocking_keys
            )
            blocks.setdefault(block_key, []).append(record_key)

        # -- stages 3 to 5, block by block
        for block_key in sorted(blocks):
            clusters.extend(self._assess_block(
                subject=subject,
                schema=schema,
                blocking_keys=blocking_keys,
                block_key=block_key,
                member_keys=sorted(blocks[block_key]),
                texts_by_field=texts_by_field,
                vectors_by_field=vectors_by_field,
                compare_weights=compare_weights,
                bands=bands,
                rows_by_key=rows_by_key,
                cluster_offset=len(clusters),
            ))

        findings = self._to_findings(subject, clusters)
        return AgentResult(
            dimension=self.dimension,
            agent=self.name,
            findings=findings,
            clusters=clusters,
            rules_run=0,
            records_assessed=len(candidate_keys),
            records_excluded=len(self.held_back),
            findings_by_table={subject: len(findings)} if findings else {},
        )

    def _assess_block(
        self,
        subject: str,
        schema: TableSchema,
        blocking_keys: list[str],
        block_key: tuple,
        member_keys: list[str],
        texts_by_field: dict[str, dict[str, str]],
        vectors_by_field: dict[str, tuple[dict[str, int], np.ndarray]],
        compare_weights: dict[str, float],
        bands: dict[str, float],
        rows_by_key: dict[str, dict[str, Any]],
        cluster_offset: int,
    ) -> list[DuplicateCluster]:
        """Score every pair in one block, then group the confirmed matches."""
        size: int = len(member_keys)
        pair_count: int = 0
        combined: np.ndarray = None
        field_score: np.ndarray = None
        texts: list[str] = []
        rows: list[int] = []
        groups: _Groups = _Groups()
        clusters: list[DuplicateCluster] = []
        duplicate_band: float = float(bands["duplicate"])
        review_low: float = float(bands["review_low"])
        method_weights: dict[str, float] = self.settings["method_weights"]
        metric: str = ""
        name: str = ""
        weight: float = 0.0
        lookup: dict[str, int] = {}
        vectors: np.ndarray = None
        use_semantic: bool = False
        fuzzy_weight: float = 1.0
        left: int = 0
        right: int = 0
        score: float = 0.0

        if size < 2:
            return clusters
        pair_count = size * (size - 1) // 2
        if pair_count > MAX_BLOCK_PAIRS:
            raise ValueError(
                f"block {dict(zip(blocking_keys, block_key))} holds {size} records, "
                f"which is {pair_count} pairs and above the {MAX_BLOCK_PAIRS} limit. "
                f"Narrow the search with another blocking key or a scope filter."
            )

        metric = schema.uniqueness.methods.fuzzy.metric
        combined = np.zeros((size, size), dtype=np.float32)
        for name in texts_by_field:
            weight = compare_weights.get(name, 0.0)
            if weight <= 0:
                continue
            texts = [texts_by_field[name].get(key, "") for key in member_keys]
            rows = []
            use_semantic = False
            if name in vectors_by_field and method_weights["semantic"] > 0:
                lookup, vectors = vectors_by_field[name]
                rows = [lookup.get(key, -1) for key in member_keys]
                use_semantic = all(index >= 0 for index in rows)
            # When the semantic rung cannot run, its share of the weight goes
            # BACK to the fuzzy rung. Without this a perfect fuzzy match would
            # score 0.5 instead of 1.0, and no pair in a fuzzy-only run could
            # ever reach the duplicate band. The failure would be silent: the
            # agent would simply report that nothing is a duplicate.
            fuzzy_weight = method_weights["fuzzy"] if use_semantic else 1.0
            field_score = self._fuzzy_matrix(texts, metric) * np.float32(fuzzy_weight)
            if use_semantic:
                field_score = field_score + self._semantic_matrix(rows, vectors) * np.float32(
                    method_weights["semantic"]
                )
            combined = combined + field_score * np.float32(weight)

        for left in range(size):
            for right in range(left + 1, size):
                score = float(combined[left, right])
                if score >= duplicate_band:
                    self.score_spread["duplicate"] += 1
                    groups.join(left, right)
                elif score >= review_low:
                    self.score_spread["uncertain"] += 1
                    self.candidate_pairs.append({
                        "table": subject,
                        "left_id": member_keys[left],
                        "right_id": member_keys[right],
                        "score": round(score, 4),
                        "blocking_values": dict(zip(blocking_keys, block_key)),
                    })
                else:
                    self.score_spread["below"] += 1

        clusters = self._build_clusters(
            subject=subject,
            schema=schema,
            blocking_keys=blocking_keys,
            block_key=block_key,
            member_keys=member_keys,
            groups=groups,
            combined=combined,
            texts_by_field=texts_by_field,
            rows_by_key=rows_by_key,
            duplicate_band=duplicate_band,
            cluster_offset=cluster_offset,
        )
        return clusters

    def _build_clusters(
        self,
        subject: str,
        schema: TableSchema,
        blocking_keys: list[str],
        block_key: tuple,
        member_keys: list[str],
        groups: _Groups,
        combined: np.ndarray,
        texts_by_field: dict[str, dict[str, str]],
        rows_by_key: dict[str, dict[str, Any]],
        duplicate_band: float,
        cluster_offset: int,
    ) -> list[DuplicateCluster]:
        """Turn each joined group into a cluster with a survivor."""
        clusters: list[DuplicateCluster] = []
        first_text: dict[str, str] = {}
        indices: list[int] = []
        keys: list[str] = []
        survivor_key: str = ""
        reason: SurvivorReason = SurvivorReason.NONE
        resolution: ClusterResolution = ClusterResolution.NEEDS_STEWARD
        counts: dict[str, tuple[int, int]] = {}
        members: list[ClusterMember] = []
        weakest: float = 1.0
        survivor_index: int = 0
        left: int = 0
        right: int = 0
        index: int = 0
        score: float = 0.0
        name: str = ""

        name = next(iter(texts_by_field))
        first_text = texts_by_field[name]
        for indices in groups.groups().values():
            if len(indices) < 2:
                continue
            keys = [member_keys[index] for index in indices]
            survivor_key, reason, resolution, counts = self._choose_survivor(
                keys, first_text, rows_by_key, schema
            )
            survivor_index = member_keys.index(survivor_key)
            members = []
            weakest = 1.0
            for index in indices:
                score = 1.0 if index == survivor_index else float(combined[survivor_index, index])
                members.append(ClusterMember(
                    record_id=member_keys[index],
                    score=round(score, 4),
                    below_band=score < duplicate_band and index != survivor_index,
                    is_survivor=index == survivor_index,
                    populated_mandatory=counts[member_keys[index]][0],
                    populated_total=counts[member_keys[index]][1],
                ))
            for left in indices:
                for right in indices:
                    if left < right:
                        weakest = min(weakest, float(combined[left, right]))
            clusters.append(DuplicateCluster(
                cluster_id=f"CL-{cluster_offset + len(clusters) + 1:04d}",
                table=subject,
                survivor_id=survivor_key,
                survivor_reason=reason,
                resolution=resolution,
                members=members,
                weakest_link=round(weakest, 4),
                mode=self.mode,
                blocking_values=dict(zip(blocking_keys, block_key)),
            ))
        return clusters

    def _to_findings(self, subject: str, clusters: list[DuplicateCluster]) -> list[Finding]:
        """One finding per record the agent would NOT keep.

        The survivor raises no finding: it is the record that stays. A cluster of
        three therefore raises two findings, which is the number of records that
        would leave the table.
        """
        findings: list[Finding] = []
        cluster: DuplicateCluster = None
        member: ClusterMember = None
        message: str = ""

        for cluster in clusters:
            for member in cluster.members:
                if member.is_survivor:
                    continue
                if cluster.resolution is ClusterResolution.AUTOMATIC:
                    message = (
                        f"duplicate of {cluster.survivor_id} at {member.score:.2f}; "
                        f"keep {cluster.survivor_id} ({cluster.survivor_reason.value})"
                    )
                else:
                    message = (
                        f"duplicate of {cluster.survivor_id} at {member.score:.2f}; "
                        f"a steward must choose which record to keep"
                    )
                findings.append(Finding(
                    dimension=self.dimension,
                    table=subject,
                    record_id=member.record_id,
                    field=None,
                    rule_id=f"UNIQ_{subject}_DUPLICATE",
                    issue=message,
                    severity=Severity.HIGH,
                    observed_value=member.score,
                    expected=f"no duplicate of {cluster.survivor_id}",
                    metadata={
                        "cluster_id": cluster.cluster_id,
                        "survivor_id": cluster.survivor_id,
                        "score": member.score,
                        "below_band": member.below_band,
                        "weakest_link": cluster.weakest_link,
                        "resolution": cluster.resolution.value,
                        "mode": cluster.mode.value,
                        "bands": self.settings.get("bands", {}),
                        "settings_code": self.settings.get("fingerprint", ""),
                    },
                ))
        return findings

    def summary(self) -> dict[str, Any]:
        """What the run did, for a console or a screen."""
        reasons: dict[str, int] = {}
        reason: str = ""

        for reason in self.held_back.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "mode": self.mode.value,
            "mode_reason": self.mode_reason,
            "held_back": reasons,
            "held_back_total": len(self.held_back),
            "score_spread": dict(self.score_spread),
            "candidate_pairs": len(self.candidate_pairs),
            "bands": self.settings.get("bands", {}),
            "settings_code": self.settings.get("fingerprint", ""),
        }
