# v0.1 | 27-Jun-2026 | Initial end-to-end smoke test for the data foundation
# v0.2 | 27-Jun-2026 | Follow rename of loaders to extract_loader and rule_loader
# v0.3 | 27-Jun-2026 | Add executor-vs-ground-truth test
# v0.4 | 27-Jun-2026 | Add agents-and-scorecard test
# v0.5 | 27-Jun-2026 | Add assess() shared-function test

"""End-to-end smoke test for the AgentDQ data foundation.

Exercises the four modules built so far - loader, profiler-derived schema,
schema parsers and the generator - and asserts the invariants that matter:
leading-zero preservation, composite-key uniqueness, comma-decimal parsing,
the null-date sentinel, referential integrity, mandatory completeness, domain
validity and reproducibility.

Tests that need the real extracts or the generated profiles are skipped
automatically when those inputs are absent, so the file still runs on a fresh
checkout. Run from the repository root with:

    uv run pytest -v
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT: Path = Path(__file__).resolve().parents[1]
SCHEMA_DIR: Path = REPO_ROOT / "config" / "schema"
PROFILE_DIR: Path = REPO_ROOT / "data" / "profile"
RAW_DIR: Path = REPO_ROOT / "data" / "raw"
TABLES: list[str] = ["MARA", "MARC", "MAKT"]

RAW_AVAILABLE: bool = all((RAW_DIR / f"{t}_EX_DATA.xlsx").exists() for t in TABLES)
PROFILES_AVAILABLE: bool = all((PROFILE_DIR / f"{t}_profile.json").exists() for t in TABLES)
RULES_DIR: Path = REPO_ROOT / "config" / "rules"
RULES_AVAILABLE: bool = (RULES_DIR / "marc_rules.yaml").exists()


@pytest.mark.skipif(not RAW_AVAILABLE, reason="real extracts not present in data/raw")
def test_loader_preserves_zeros_and_distinguishes_composite_key() -> None:
    """The loader keeps leading zeros and MATNR alone is not unique in MARC."""
    from src.data.extract_loader import load_sap_table  # v0.2

    marc = load_sap_table(str(RAW_DIR / "MARC_EX_DATA.xlsx"), header_anchor="MATNR")

    assert marc["MATNR"].str.startswith("0").any(), "leading zeros were stripped"
    assert marc["MATNR"].str.len().max() <= 18
    assert not marc.duplicated(["MATNR", "WERKS"]).any(), "composite key should be unique"
    assert marc["MATNR"].duplicated().any(), "multi-plant materials should repeat MATNR"


def test_schema_parsers_and_formatters() -> None:
    """Quantity and date parsing, formatting and key composition behave."""
    from src.data.schema import load_schemas

    schemas = load_schemas(str(SCHEMA_DIR), TABLES)
    marc = schemas["MARC"]

    assert marc.parse_quantity("1.000,000") == 1000.0
    assert marc.parse_quantity("157,000") == 157.0
    assert marc.parse_quantity("4.838,000") == 4838.0
    assert marc.parse_quantity("0,000") == 0.0
    assert marc.parse_quantity("") is None
    assert marc.parse_quantity(None) is None

    assert marc.format_quantity(4838.0) == "4.838,000"
    assert marc.format_quantity(157.0) == "157,000"

    assert marc.is_null("MMSTD", "00/00/0000") is True
    assert marc.parse_date("00/00/0000") is None
    assert marc.parse_date("11/04/2020") is not None

    assert marc.record_key({"MATNR": "X", "WERKS": "0001"}) == "MATNR=X|WERKS=0001"


def test_schema_keys_and_mandatory_fields() -> None:
    """Composite keys and baseline mandatory fields load as expected."""
    from src.data.schema import load_schemas

    schemas = load_schemas(str(SCHEMA_DIR), TABLES)

    assert schemas["MARC"].primary_key == ["MATNR", "WERKS"]
    assert schemas["MAKT"].primary_key == ["MATNR", "SPRAS"]
    assert "MTART" in schemas["MARA"].mandatory_fields()
    assert "MAKTX" in schemas["MAKT"].mandatory_fields()


@pytest.mark.skipif(not PROFILES_AVAILABLE, reason="profiles not generated yet")
def test_generator_baseline_invariants() -> None:
    """A generated baseline is consistent, complete and domain-valid."""
    from src.data.generator import generate_dataset
    from src.data.schema import load_schemas

    schemas = load_schemas(str(SCHEMA_DIR), TABLES)
    frames = generate_dataset(str(SCHEMA_DIR), str(PROFILE_DIR), TABLES, n_materials=500, seed=42)
    mara = frames["MARA"]
    marc = frames["MARC"]
    makt = frames["MAKT"]
    mara_keys = set(mara["MATNR"])

    # Referential integrity.
    assert set(marc["MATNR"]).issubset(mara_keys)
    assert set(makt["MATNR"]).issubset(mara_keys)

    # Composite key uniqueness.
    assert mara["MATNR"].is_unique
    assert not marc.duplicated(["MATNR", "WERKS"]).any()
    assert not makt.duplicated(["MATNR", "SPRAS"]).any()

    # Mandatory completeness.
    for table, frame in (("MARA", mara), ("MARC", marc), ("MAKT", makt)):
        for field_name in schemas[table].mandatory_fields():
            assert frame[field_name].notna().all(), f"{table}.{field_name} has nulls"

    # Domain validity.
    for table, frame in (("MARA", mara), ("MARC", marc)):
        for column in frame.columns:
            domain = schemas[table].domain(column)
            if domain:
                values = frame[column].dropna()
                assert values.isin(domain).all(), f"{table}.{column} has out-of-domain values"

    # Format fidelity.
    assert len(mara["MATNR"].iloc[0]) == 18
    assert makt["MAKTG"].iloc[0] == makt["MAKTX"].iloc[0].upper()


@pytest.mark.skipif(not PROFILES_AVAILABLE, reason="profiles not generated yet")
def test_generator_is_reproducible() -> None:
    """Identical seeds produce identical data; different seeds do not."""
    from src.data.generator import generate_dataset

    first = generate_dataset(str(SCHEMA_DIR), str(PROFILE_DIR), TABLES, n_materials=300, seed=7)
    second = generate_dataset(str(SCHEMA_DIR), str(PROFILE_DIR), TABLES, n_materials=300, seed=7)
    third = generate_dataset(str(SCHEMA_DIR), str(PROFILE_DIR), TABLES, n_materials=300, seed=8)

    assert first["MARA"].equals(second["MARA"])
    assert first["MARC"].equals(second["MARC"])
    assert not first["MARA"].equals(third["MARA"])


@pytest.mark.skipif(not RULES_AVAILABLE, reason="IS rules not imported yet")
def test_imported_rules_reload_as_valid_ir() -> None:
    """Every imported rule YAML reloads as a valid RuleSpec."""
    import yaml

    from src.contracts import RuleSpec

    table_file: str = ""
    total: int = 0
    for table_file in ("mara_rules.yaml", "marc_rules.yaml", "makt_rules.yaml"):
        payload = yaml.safe_load((RULES_DIR / table_file).read_text(encoding="utf-8"))
        for rule_dict in payload["rules"]:
            RuleSpec.model_validate(rule_dict)
            total += 1
    assert total > 0


@pytest.mark.skipif(not RULES_AVAILABLE, reason="IS rules not imported yet")
def test_cross_field_examples_express_scope_and_implies() -> None:
    """The curated examples exercise scope filtering and an implies tree."""
    import yaml

    from src.contracts import BoolOp, RuleSpec

    payload = yaml.safe_load((RULES_DIR / "cross_field_examples.yaml").read_text(encoding="utf-8"))
    rules = [RuleSpec.model_validate(rule_dict) for rule_dict in payload["rules"]]
    scoped = [r for r in rules if r.scope is not None]

    assert scoped, "expected at least one scoped example"
    assert any(
        getattr(r.assertion, "op", None) == BoolOp.IMPLIES for r in rules
    ), "expected an implies assertion among the examples"


@pytest.mark.skipif(
    not (PROFILES_AVAILABLE and RULES_AVAILABLE),
    reason="profiles or rules not available",
)
def test_injector_ground_truth_is_clean() -> None:
    """Every active rule's violations equal exactly the injected labels."""
    import pandas as pd

    from src.contracts import Operator
    from src.data.defect_injector import inject_defects
    from src.data.generator import generate_dataset
    from src.data.schema import load_schemas
    from src.rules.rule_loader import load_rules  # v0.2

    schemas = load_schemas(str(SCHEMA_DIR), TABLES)
    rules = load_rules(str(RULES_DIR))
    baseline = generate_dataset(str(SCHEMA_DIR), str(PROFILE_DIR), TABLES, n_materials=2000, seed=42)
    frames, labels, manifest = inject_defects(baseline, schemas, rules, scenario="degraded", seed=42)

    label_frame = pd.DataFrame([label.model_dump() for label in labels])
    rule_map = {rule.rule_id: rule for rule in rules}
    scope = manifest["evaluation_scope"]

    # Completeness: nulls on the ruled field equal labelled completeness defects.
    for rule_id in scope["completeness_rules"]:
        rule = rule_map[rule_id]
        oracle = int(frames[rule.table][rule.fields[0]].isna().sum())
        labelled = int(((label_frame["dimension"] == "Completeness") & (label_frame["rule_id"] == rule_id)).sum())
        assert oracle == labelled, f"completeness mismatch on {rule_id}"

    # Validity: out-of-domain non-null values equal labelled validity defects.
    for rule_id in scope["validity_rules"]:
        rule = rule_map[rule_id]
        column = frames[rule.table][rule.fields[0]]
        oracle = int((column.notna() & ~column.isin(rule.assertion.value)).sum())
        labelled = int(((label_frame["dimension"] == "Validity") & (label_frame["rule_id"] == rule_id)).sum())
        assert oracle == labelled, f"validity mismatch on {rule_id}"

    # Referential integrity and key integrity survive injection.
    mara_keys = set(frames["MARA"]["MATNR"])
    assert frames["MARA"]["MATNR"].notna().all(), "a key field was nulled"
    assert set(frames["MARC"]["MATNR"]).issubset(mara_keys)
    assert set(frames["MAKT"]["MATNR"]).issubset(mara_keys)


@pytest.mark.skipif(
    not (PROFILES_AVAILABLE and RULES_AVAILABLE),
    reason="profiles or rules not available",
)
def test_injector_reproducible_and_scenarios_scale() -> None:
    """Identical seeds match; critical injects more than healthy."""
    from src.data.defect_injector import inject_defects
    from src.data.generator import generate_dataset
    from src.data.schema import load_schemas
    from src.rules.rule_loader import load_rules  # v0.2

    schemas = load_schemas(str(SCHEMA_DIR), TABLES)
    rules = load_rules(str(RULES_DIR))
    baseline = generate_dataset(str(SCHEMA_DIR), str(PROFILE_DIR), TABLES, n_materials=2000, seed=42)

    _, first, _ = inject_defects(baseline, schemas, rules, scenario="degraded", seed=7)
    _, second, _ = inject_defects(baseline, schemas, rules, scenario="degraded", seed=7)
    assert sorted(d.defect_id for d in first) == sorted(d.defect_id for d in second)

    _, healthy, _ = inject_defects(baseline, schemas, rules, scenario="healthy", seed=7)
    _, critical, _ = inject_defects(baseline, schemas, rules, scenario="critical", seed=7)
    assert len(critical) > len(healthy)


@pytest.mark.skipif(
    not (PROFILES_AVAILABLE and RULES_AVAILABLE),
    reason="profiles or rules not available",
)
def test_executor_reproduces_ground_truth() -> None:
    """The executor's findings match the injected labels exactly (deterministic dims)."""
    from src.data.defect_injector import inject_defects
    from src.data.generator import generate_dataset
    from src.data.schema import load_schemas
    from src.rules.executor import execute_ruleset
    from src.rules.rule_loader import load_rules

    schemas = load_schemas(str(SCHEMA_DIR), TABLES)
    all_rules = load_rules(str(RULES_DIR))
    baseline = generate_dataset(str(SCHEMA_DIR), str(PROFILE_DIR), TABLES, n_materials=1500, seed=42)
    frames, labels, manifest = inject_defects(baseline, schemas, all_rules, scenario="degraded", seed=42)

    scope = manifest["evaluation_scope"]
    active_ids = set(scope["completeness_rules"] + scope["validity_rules"] + scope["consistency_rules"])
    active_rules = [rule for rule in all_rules if rule.rule_id in active_ids]
    findings = execute_ruleset(active_rules, frames, schemas)

    dimension: str = ""
    for dimension in ("Completeness", "Validity", "Consistency"):
        found = {(f.rule_id, f.record_id) for f in findings if f.dimension.value == dimension}
        labelled = {(d.rule_id, d.record_key) for d in labels if d.dimension.value == dimension}
        assert found == labelled, f"{dimension}: executor findings do not match ground truth"


@pytest.mark.skipif(
    not (PROFILES_AVAILABLE and RULES_AVAILABLE),
    reason="profiles or rules not available",
)
def test_agents_and_scorecard() -> None:
    """The two agents produce findings that score perfectly and a sane scorecard."""
    from src.agents.completeness import CompletenessAgent
    from src.agents.validity import ValidityAgent
    from src.data.defect_injector import inject_defects
    from src.data.generator import generate_dataset
    from src.data.schema import load_schemas
    from src.reporting.scorecard import compute_scorecard, evaluate_against_labels
    from src.rules.rule_loader import load_rules

    schemas = load_schemas(str(SCHEMA_DIR), TABLES)
    rules = load_rules(str(RULES_DIR))
    baseline = generate_dataset(str(SCHEMA_DIR), str(PROFILE_DIR), TABLES, n_materials=1500, seed=42)
    frames, labels, _ = inject_defects(baseline, schemas, rules, scenario="degraded", seed=42)

    findings = []
    for agent in (CompletenessAgent(), ValidityAgent()):
        findings.extend(agent.run(frames, schemas, rules).findings)

    scorecard = compute_scorecard(findings, frames, ["Completeness", "Validity"])
    assert 0.0 <= scorecard.overall_score_pct <= 100.0
    assert scorecard.total_findings == len(findings)

    evaluation = evaluate_against_labels(findings, labels, ["Completeness", "Validity"])
    for dimension in ("Completeness", "Validity"):
        assert evaluation[dimension].precision == 1.0
        assert evaluation[dimension].recall == 1.0


@pytest.mark.skipif(
    not (PROFILES_AVAILABLE and RULES_AVAILABLE),
    reason="profiles or rules not available",
)
def test_assess_shared_function() -> None:
    """assess() returns a populated result with an evaluation when labels exist."""
    import tempfile

    from src.data.defect_injector import inject_defects
    from src.data.generator import generate_dataset
    from src.data.schema import load_schemas
    from src.reporting.assessment import assess
    from src.rules.rule_loader import load_rules

    schemas = load_schemas(str(SCHEMA_DIR), TABLES)
    rules = load_rules(str(RULES_DIR))
    baseline = generate_dataset(str(SCHEMA_DIR), str(PROFILE_DIR), TABLES, n_materials=800, seed=42)
    frames, labels, manifest = inject_defects(baseline, schemas, rules, scenario="degraded", seed=42)

    with tempfile.TemporaryDirectory() as tmp:
        from src.data.defect_injector import _persist

        _persist(frames, labels, manifest, __import__("pathlib").Path(tmp))
        result = assess(tmp, str(SCHEMA_DIR), str(RULES_DIR), TABLES, "parquet")

    assert result.total_records > 0
    assert result.scorecard.total_findings > 0
    assert result.has_ground_truth is True
    assert result.evaluation is not None
    assert result.evaluation["Completeness"].recall == 1.0
