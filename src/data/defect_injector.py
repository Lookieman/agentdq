# v0.1 | 27-Jun-2026 | Initial controlled defect injector with ground-truth labels
# v0.2 | 27-Jun-2026 | Inject uniqueness twins onto the clean canvas before field-level injection

"""Controlled defect injector for the synthetic baseline.

Takes a clean baseline and introduces defects at known, scenario-driven rates,
writing a ground-truth label for every defect. Those labels are what make the
evaluation rigorous: an agent's findings are scored against them for precision,
recall and F1 per dimension.

A defect is the targeted violation of a rule, so the injector is rule-driven for
the rule-bound dimensions:

- Completeness  null a field that carries a not-null rule
- Validity      replace a value with one outside the field's permitted domain
- Consistency   make a cross-field rule's antecedent hold and its consequent fail
- Uniqueness    add a near-duplicate material (new key, perturbed description)

Timeliness and Accuracy are stubbed in this first cut: Timeliness awaits the
date fields from the MARA re-export, and Accuracy is the language-model agent we
build last.

Ground truth integrity rests on one principle: before injecting, the baseline is
conformed so that every rule the injector is responsible for passes everywhere.
After that, the only violations present are the ones injected, so the label set
equals the true defect set. Rules that cannot be conformed in this cut are
recorded as out of evaluation scope in the manifest rather than silently left to
contaminate the ground truth.

Run as a module:

    python -m src.data.defect_injector --baseline data/synthetic/baseline \\
        --schema config/schema --rules config/rules --scenario degraded \\
        --seed 42 --out data/synthetic/degraded
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.contracts import (
    BoolNode,
    BoolOp,
    Comparison,
    DefectLabel,
    Dimension,
    Operator,
    RuleArchetype,
    RuleSpec,
)
from src.data.schema import TableSchema, load_schemas
from src.rules.loader import load_rules


# Per-dimension defect rate presets (fraction of eligible records per dimension).
SCENARIO_RATES: dict[str, float] = {
    "healthy": 0.015,
    "degraded": 0.06,
    "critical": 0.17,
}

INJECTED_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension.COMPLETENESS,
    Dimension.VALIDITY,
    Dimension.CONSISTENCY,
    Dimension.UNIQUENESS,
)

TWIN_MATNR_START: int = 900000000
MATNR_WIDTH: int = 18


# --- small predicate evaluation, only what the injector needs -----------------

def _eval_comparison_mask(cmp: Comparison, frame: pd.DataFrame) -> pd.Series:
    """Boolean mask of rows where a leaf comparison holds.

    Covers the operators the injector relies on for scope and antecedent
    selection. This is a deliberately minimal preview of the future executor.
    """
    column: pd.Series = frame[cmp.field] if cmp.field in frame.columns else pd.Series([None] * len(frame))
    mask: pd.Series = pd.Series(False, index=frame.index)

    if cmp.op == Operator.IS_NULL:
        mask = column.isna()
    elif cmp.op == Operator.IS_NOT_NULL:
        mask = column.notna()
    elif cmp.op == Operator.IN:
        mask = column.isin(cmp.value or [])
    elif cmp.op == Operator.NOT_IN:
        mask = ~column.isin(cmp.value or [])
    elif cmp.op == Operator.EQ:
        mask = column == cmp.value
    elif cmp.op == Operator.NE:
        mask = column != cmp.value
    return mask


# --- out-of-domain value helper ----------------------------------------------

def _out_of_domain_value(domain: list[str], rng: np.random.Generator) -> str:
    """Produce a short code guaranteed not to be in the domain."""
    domain_set: set[str] = set(domain)
    candidate: str = ""
    attempt: int = 0
    alphabet: str = "ZYXQW"

    candidate = "ZZ"
    while candidate in domain_set and attempt < 50:
        candidate = "".join(rng.choice(list(alphabet), size=2)) + str(int(rng.integers(0, 99)))
        attempt += 1
    return candidate


# --- rule grouping ------------------------------------------------------------

def _is_implies(rule: RuleSpec) -> bool:
    """Whether a rule's assertion is a top-level IMPLIES with leaf operands."""
    assertion = rule.assertion
    if not isinstance(assertion, BoolNode) or assertion.op != BoolOp.IMPLIES:
        return False
    if len(assertion.operands) != 2:
        return False
    return all(isinstance(operand, Comparison) for operand in assertion.operands)


def _group_rules(rules: list[RuleSpec], schemas: dict[str, TableSchema]) -> dict[str, list[RuleSpec]]:  # v0.3
    """Split rules into the groups the injector acts on.

    not-null rules on primary-key fields are excluded from completeness: nulling
    a key would destroy record identity and referential integrity, which is not a
    completeness defect we ever want to inject.
    """
    completeness: list[RuleSpec] = []
    validity: list[RuleSpec] = []
    consistency: list[RuleSpec] = []
    rule: RuleSpec = None
    schema: TableSchema = None

    for rule in rules:
        if not rule.executable:
            continue
        schema = schemas.get(rule.table)  # v0.3
        if rule.archetype == RuleArchetype.NOT_NULL:
            if schema is not None and rule.fields[0] in schema.primary_key:  # v0.3
                continue  # v0.3
            completeness.append(rule)
        elif rule.archetype in (RuleArchetype.DOMAIN_IN, RuleArchetype.REFERENCE_EXISTS):
            if isinstance(rule.assertion, Comparison) and rule.assertion.value:
                validity.append(rule)
        elif rule.archetype == RuleArchetype.CROSS_FIELD and _is_implies(rule):
            consistency.append(rule)
    return {"completeness": completeness, "validity": validity, "consistency": consistency}


# --- conformance (establish a clean canvas) -----------------------------------

def _conform_completeness(frames: dict[str, pd.DataFrame], rules: list[RuleSpec], rng: np.random.Generator) -> None:
    """Fill every not-null-ruled field to full population in the baseline."""
    rule: RuleSpec = None
    field: str = ""
    frame: pd.DataFrame = None
    populated: pd.Series = None
    empty_mask: pd.Series = None
    fill_values: np.ndarray = None

    for rule in rules:
        frame = frames.get(rule.table)
        if frame is None:
            continue
        field = rule.fields[0]
        if field not in frame.columns:
            continue
        empty_mask = frame[field].isna()
        if not empty_mask.any():
            continue
        populated = frame.loc[~empty_mask, field]
        if populated.empty:
            continue
        fill_values = rng.choice(populated.to_numpy(), size=int(empty_mask.sum()))
        frame.loc[empty_mask, field] = fill_values


def _conform_consistency(frames: dict[str, pd.DataFrame], rules: list[RuleSpec]) -> None:
    """Ensure each IMPLIES rule holds: where antecedent is true, force consequent true."""
    rule: RuleSpec = None
    frame: pd.DataFrame = None
    antecedent: Comparison = None
    consequent: Comparison = None
    scope_mask: pd.Series = None
    ante_mask: pd.Series = None
    target_mask: pd.Series = None

    for rule in rules:
        frame = frames.get(rule.table)
        if frame is None:
            continue
        antecedent = rule.assertion.operands[0]
        consequent = rule.assertion.operands[1]
        scope_mask = pd.Series(True, index=frame.index)
        if rule.scope is not None and isinstance(rule.scope, Comparison):
            scope_mask = _eval_comparison_mask(rule.scope, frame)
        ante_mask = _eval_comparison_mask(antecedent, frame)
        target_mask = scope_mask & ante_mask
        if not target_mask.any():
            continue
        # Force the consequent true on those rows.
        if consequent.op == Operator.IS_NOT_NULL:
            fix_mask = target_mask & frame[consequent.field].isna()
            frame.loc[fix_mask, consequent.field] = "0,000"
        elif consequent.op == Operator.IN and consequent.value:
            fix_mask = target_mask & ~frame[consequent.field].isin(consequent.value)
            frame.loc[fix_mask, consequent.field] = consequent.value[0]
        elif consequent.op == Operator.EQ:
            fix_mask = target_mask & (frame[consequent.field] != consequent.value)
            frame.loc[fix_mask, consequent.field] = consequent.value


# --- injection ----------------------------------------------------------------

def _sample_indices(eligible: pd.Index, rate: float, rng: np.random.Generator) -> list[int]:
    """Pick a rate-proportion of eligible row indices."""
    count: int = int(round(len(eligible) * rate))
    chosen: np.ndarray = None
    if count <= 0 or len(eligible) == 0:
        return []
    chosen = rng.choice(eligible.to_numpy(), size=min(count, len(eligible)), replace=False)
    return [int(i) for i in chosen]


def _inject_completeness(
    frames: dict[str, pd.DataFrame],
    rules: list[RuleSpec],
    rate: float,
    rng: np.random.Generator,
    schemas: dict[str, TableSchema],
    used: set,
    labels: list[DefectLabel],
) -> None:
    """Null not-null-ruled fields and label each as a Completeness defect."""
    rule: RuleSpec = None
    frame: pd.DataFrame = None
    field: str = ""
    eligible: pd.Index = None
    idx: int = 0
    original: Any = None

    for rule in rules:
        frame = frames.get(rule.table)
        if frame is None:
            continue
        field = rule.fields[0]
        eligible = frame.index[frame[field].notna()]
        for idx in _sample_indices(eligible, rate, rng):
            if (rule.table, idx, field) in used:
                continue
            original = frame.at[idx, field]
            frame.at[idx, field] = None
            used.add((rule.table, idx, field))
            labels.append(DefectLabel(
                defect_id=f"COMP_{rule.table}_{idx}_{field}",
                table=rule.table,
                record_key=schemas[rule.table].record_key(frame.loc[idx].to_dict()),
                dimension=Dimension.COMPLETENESS,
                field=field,
                rule_id=rule.rule_id,
                original_value=original,
                corrupted_value=None,
            ))


def _inject_validity(
    frames: dict[str, pd.DataFrame],
    rules: list[RuleSpec],
    rate: float,
    rng: np.random.Generator,
    schemas: dict[str, TableSchema],
    used: set,
    labels: list[DefectLabel],
) -> None:
    """Replace in-domain values with out-of-domain ones and label them."""
    rule: RuleSpec = None
    frame: pd.DataFrame = None
    field: str = ""
    domain: list[str] = []
    eligible: pd.Index = None
    idx: int = 0
    original: Any = None
    bad_value: str = ""

    for rule in rules:
        frame = frames.get(rule.table)
        if frame is None:
            continue
        field = rule.fields[0]
        domain = rule.assertion.value
        eligible = frame.index[frame[field].notna() & frame[field].isin(domain)]
        for idx in _sample_indices(eligible, rate, rng):
            if (rule.table, idx, field) in used:
                continue
            original = frame.at[idx, field]
            bad_value = _out_of_domain_value(domain, rng)
            frame.at[idx, field] = bad_value
            used.add((rule.table, idx, field))
            labels.append(DefectLabel(
                defect_id=f"VAL_{rule.table}_{idx}_{field}",
                table=rule.table,
                record_key=schemas[rule.table].record_key(frame.loc[idx].to_dict()),
                dimension=Dimension.VALIDITY,
                field=field,
                rule_id=rule.rule_id,
                original_value=original,
                corrupted_value=bad_value,
            ))


def _inject_consistency(
    frames: dict[str, pd.DataFrame],
    rules: list[RuleSpec],
    rate: float,
    rng: np.random.Generator,
    schemas: dict[str, TableSchema],
    used: set,
    labels: list[DefectLabel],
) -> None:
    """Make antecedent true and consequent false on in-scope rows; label them."""
    rule: RuleSpec = None
    frame: pd.DataFrame = None
    antecedent: Comparison = None
    consequent: Comparison = None
    scope_mask: pd.Series = None
    eligible: pd.Index = None
    idx: int = 0
    original_ante: Any = None
    original_cons: Any = None
    new_ante: Any = None

    for rule in rules:
        frame = frames.get(rule.table)
        if frame is None:
            continue
        antecedent = rule.assertion.operands[0]
        consequent = rule.assertion.operands[1]
        scope_mask = pd.Series(True, index=frame.index)
        if rule.scope is not None and isinstance(rule.scope, Comparison):
            scope_mask = _eval_comparison_mask(rule.scope, frame)
        eligible = frame.index[scope_mask]
        for idx in _sample_indices(eligible, rate, rng):
            if (rule.table, idx, antecedent.field) in used or (rule.table, idx, consequent.field) in used:
                continue
            original_ante = frame.at[idx, antecedent.field]
            original_cons = frame.at[idx, consequent.field]
            # Make the antecedent hold.
            if antecedent.op == Operator.IN and antecedent.value:
                new_ante = antecedent.value[0]
            elif antecedent.op == Operator.EQ:
                new_ante = antecedent.value
            else:
                new_ante = original_ante
            frame.at[idx, antecedent.field] = new_ante
            # Make the consequent fail.
            if consequent.op == Operator.IS_NOT_NULL:
                frame.at[idx, consequent.field] = None
            elif consequent.op == Operator.IN and consequent.value:
                frame.at[idx, consequent.field] = _out_of_domain_value(consequent.value, rng)
            elif consequent.op == Operator.EQ:
                frame.at[idx, consequent.field] = "__INCONSISTENT__"
            used.add((rule.table, idx, antecedent.field))
            used.add((rule.table, idx, consequent.field))
            labels.append(DefectLabel(
                defect_id=f"CONS_{rule.table}_{idx}_{rule.rule_id}",
                table=rule.table,
                record_key=schemas[rule.table].record_key(frame.loc[idx].to_dict()),
                dimension=Dimension.CONSISTENCY,
                field=f"{antecedent.field},{consequent.field}",
                rule_id=rule.rule_id,
                original_value=f"{antecedent.field}={original_ante};{consequent.field}={original_cons}",
                corrupted_value=f"{antecedent.field}={new_ante};{consequent.field}=violated",
            ))


_PERTURBATIONS: tuple[str, ...] = ("upper", "trail_space", "punct", "swap")


def _perturb(text: str, rng: np.random.Generator) -> str:
    """Return a near-duplicate variant of a description."""
    strategy: str = str(rng.choice(_PERTURBATIONS))
    chars: list[str] = []
    pos: int = 0

    if not text:
        return text
    if strategy == "upper":
        return text.upper()
    if strategy == "trail_space":
        return text + "  "
    if strategy == "punct":
        return text.replace(" ", " - ", 1)
    # swap two adjacent characters
    chars = list(text)
    if len(chars) >= 4:
        pos = int(rng.integers(0, len(chars) - 1))
        chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]
    return "".join(chars)


def _inject_uniqueness(
    frames: dict[str, pd.DataFrame],
    rate: float,
    rng: np.random.Generator,
    schemas: dict[str, TableSchema],
    labels: list[DefectLabel],
) -> None:
    """Add near-duplicate materials: new key, copied rows, perturbed description."""
    mara: pd.DataFrame = frames.get("MARA")
    makt: pd.DataFrame = frames.get("MAKT")
    marc: pd.DataFrame = frames.get("MARC")
    source_matnrs: list[str] = []
    twin_counter: int = TWIN_MATNR_START
    chosen: list[int] = []
    new_mara: list[dict] = []
    new_makt: list[dict] = []
    new_marc: list[dict] = []
    source_matnr: str = ""
    twin_matnr: str = ""
    idx: int = 0

    if mara is None:
        return
    chosen = _sample_indices(mara.index, rate, rng)
    for idx in chosen:
        source_matnr = mara.at[idx, "MATNR"]
        twin_matnr = str(twin_counter).zfill(MATNR_WIDTH)
        twin_counter += 1

        mara_row = mara.loc[idx].to_dict()
        mara_row["MATNR"] = twin_matnr
        new_mara.append(mara_row)

        if makt is not None:
            makt_src = makt[makt["MATNR"] == source_matnr]
            if not makt_src.empty:
                makt_row = makt_src.iloc[0].to_dict()
                original_desc = makt_row.get("MAKTX", "")
                makt_row["MATNR"] = twin_matnr
                makt_row["MAKTX"] = _perturb(str(original_desc), rng)
                makt_row["MAKTG"] = str(makt_row["MAKTX"]).upper()
                new_makt.append(makt_row)

        if marc is not None:
            marc_src = marc[marc["MATNR"] == source_matnr]
            for _, marc_row in marc_src.iterrows():
                row_dict = marc_row.to_dict()
                row_dict["MATNR"] = twin_matnr
                new_marc.append(row_dict)

        labels.append(DefectLabel(
            defect_id=f"UNIQ_{twin_matnr}",
            table="MARA",
            record_key=f"MATNR={twin_matnr}",
            dimension=Dimension.UNIQUENESS,
            field="MAKTX",
            rule_id=None,
            original_value=source_matnr,
            corrupted_value=twin_matnr,
            detail={"duplicate_of": source_matnr},
        ))

    if new_mara:
        frames["MARA"] = pd.concat([mara, pd.DataFrame(new_mara)], ignore_index=True)
    if new_makt:
        frames["MAKT"] = pd.concat([makt, pd.DataFrame(new_makt)], ignore_index=True)
    if new_marc:
        frames["MARC"] = pd.concat([marc, pd.DataFrame(new_marc)], ignore_index=True)


# --- orchestration ------------------------------------------------------------

def inject_defects(
    baseline: dict[str, pd.DataFrame],
    schemas: dict[str, TableSchema],
    rules: list[RuleSpec],
    scenario: str = "degraded",
    seed: int = 42,
    rates: Optional[dict[str, float]] = None,
) -> tuple[dict[str, pd.DataFrame], list[DefectLabel], dict[str, Any]]:
    """Conform the baseline, inject labelled defects, and return everything."""
    frames: dict[str, pd.DataFrame] = deepcopy(baseline)
    rng: np.random.Generator = np.random.default_rng(seed)
    grouped: dict[str, list[RuleSpec]] = _group_rules(rules, schemas)  # v0.3
    base_rate: float = SCENARIO_RATES.get(scenario, SCENARIO_RATES["degraded"])
    dim_rates: dict[str, float] = rates or {
        "completeness": base_rate,
        "validity": base_rate,
        "consistency": base_rate,
        "uniqueness": base_rate,
    }
    used: set = set()
    labels: list[DefectLabel] = []

    # Conform first so the only violations are the injected ones.
    _conform_completeness(frames, grouped["completeness"], rng)
    _conform_consistency(frames, grouped["consistency"])

    # Add near-duplicate twins to the clean canvas first, so subsequent
    # field-level injection sees them as ordinary rows and labels any defect it
    # lands on them. Doing this last would copy already-corrupted rows and leave
    # unlabelled defects on the twins.
    _inject_uniqueness(frames, dim_rates["uniqueness"], rng, schemas, labels)

    # Inject the remaining dimensions over all rows, twins included.
    _inject_completeness(frames, grouped["completeness"], dim_rates["completeness"], rng, schemas, used, labels)
    _inject_validity(frames, grouped["validity"], dim_rates["validity"], rng, schemas, used, labels)
    _inject_consistency(frames, grouped["consistency"], dim_rates["consistency"], rng, schemas, used, labels)

    manifest: dict[str, Any] = _manifest(scenario, seed, dim_rates, grouped, labels)
    return frames, labels, manifest


def _manifest(
    scenario: str,
    seed: int,
    dim_rates: dict[str, float],
    grouped: dict[str, list[RuleSpec]],
    labels: list[DefectLabel],
) -> dict[str, Any]:
    """Summarise the injection run, including evaluation scope."""
    by_dimension: dict[str, int] = {}
    label: DefectLabel = None
    manifest: dict[str, Any] = {}

    for label in labels:
        by_dimension[label.dimension.value] = by_dimension.get(label.dimension.value, 0) + 1

    manifest = {
        "scenario": scenario,
        "seed": seed,
        "rates": dim_rates,
        "defects_by_dimension": by_dimension,
        "total_defects": len(labels),
        "stubbed_dimensions": ["Timeliness", "Accuracy"],
        "evaluation_scope": {
            "completeness_rules": [r.rule_id for r in grouped["completeness"]],
            "validity_rules": [r.rule_id for r in grouped["validity"]],
            "consistency_rules": [r.rule_id for r in grouped["consistency"]],
        },
    }
    return manifest


def _persist(
    frames: dict[str, pd.DataFrame],
    labels: list[DefectLabel],
    manifest: dict[str, Any],
    out_dir: Path,
) -> None:
    """Write corrupted tables, the ground-truth labels and the manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    table: str = ""
    frame: pd.DataFrame = None
    labels_frame: pd.DataFrame = None

    for table, frame in frames.items():
        frame.to_parquet(out_dir / f"{table}.parquet", index=False)
    labels_frame = pd.DataFrame([json.loads(label.model_dump_json()) for label in labels])
    labels_frame.to_parquet(out_dir / "ground_truth.parquet", index=False)
    with (out_dir / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    print(f"wrote corrupted dataset and {len(labels)} labels to {out_dir}")
    for dimension, count in manifest["defects_by_dimension"].items():
        print(f"  {dimension}: {count} defects")


def _load_baseline(baseline_dir: str, tables: list[str]) -> dict[str, pd.DataFrame]:
    """Load the clean baseline parquet files."""
    base: Path = Path(baseline_dir)
    frames: dict[str, pd.DataFrame] = {}
    table: str = ""
    for table in tables:
        frames[table] = pd.read_parquet(base / f"{table}.parquet")
    return frames


def main() -> None:
    """Entry point for module execution."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Inject labelled defects into a clean baseline."
    )
    parser.add_argument("--baseline", required=True, help="directory with clean baseline parquet")
    parser.add_argument("--schema", required=True, help="schema YAML directory")
    parser.add_argument("--rules", required=True, help="rule YAML directory")
    parser.add_argument("--tables", default="MARA,MARC,MAKT", help="comma separated tables")
    parser.add_argument("--scenario", default="degraded", choices=sorted(SCENARIO_RATES.keys()))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", required=True, help="output directory")
    args: argparse.Namespace = parser.parse_args()
    table_list: list[str] = [t.strip() for t in args.tables.split(",") if t.strip()]

    baseline: dict[str, pd.DataFrame] = _load_baseline(args.baseline, table_list)
    schemas: dict[str, TableSchema] = load_schemas(args.schema, table_list)
    rules: list[RuleSpec] = load_rules(args.rules)
    frames, labels, manifest = inject_defects(baseline, schemas, rules, args.scenario, args.seed)
    _persist(frames, labels, manifest, Path(args.out))


if __name__ == "__main__":
    main()
