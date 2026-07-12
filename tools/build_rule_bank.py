# ---------------------------------------------------------------------------
# tools/build_rule_bank.py
# v1.0 | 05-Jul-2026 | Initial creation. One-off builder: wraps imported
#                      RuleSpecs (the 107 IS rules) into rule-bank templates
#                      with derived binding, applicability signals and a
#                      default prior_strength_block. Emits config/rule_bank/
#                      templates.yaml. Includes a --self-test mode.
# v1.1 | 05-Jul-2026 | Wired to the REAL serialised RuleSpec shape (from
#                      is_importer): fields[] not field, assertion.value for the
#                      domain, dama_dimension, and provenance.reference_table as
#                      the authoritative parameter-source signal. load_source_
#                      rules() now reads config/rules/*_rules.yaml. Self-test
#                      rules updated to the exclude_none on-disk shape.
# ---------------------------------------------------------------------------
"""Build the rule bank from the imported IS rules.

This builder now reads the REAL serialised RuleSpec shape emitted by
is_importer (see _adapt_rule for the exact keys). It is testable via
`--self-test`, whose built-in rules mirror that on-disk shape and touch none of
your modules.

  load_source_rules()  reads config/rules/*_rules.yaml (the files the importer
      writes) and concatenates each file's `rules` list. No import of the
      RuleSpec class, so the bank stays decoupled and Phase-2 portable.

  The one thing worth an eyeball on first real run: enum serialisation. The
  adapter normalises archetype by lower-casing and substring-matching, so
  `domain_in` / `DOMAIN_IN` / `reference_exists` all resolve correctly; if a
  future importer change alters the on-disk key NAMES (not just their casing),
  _adapt_rule is the single place to adjust.

Run:
    python -m tools.build_rule_bank --self-test           # no repo deps
    python -m tools.build_rule_bank                        # real build
    python -m tools.build_rule_bank --rules-dir config/rules
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional

import yaml

# The bank's own dataclasses are reused so the emitted YAML round-trips through
# rule_bank.template_from_dict() exactly.
try:
    from src.rules.rule_bank import load_field_roles
except ImportError:  # allow running as a loose script during self-test
    load_field_roles = None  # type: ignore


# ---------------------------------------------------------------------------
# Field-name -> role resolution (uses the controlled vocabulary)
# ---------------------------------------------------------------------------

# A minimal fallback map so the builder can assign roles even if a field is not
# listed under any role's sap_examples. The authoritative source remains
# field_roles.yaml; this only fills gaps.
_FALLBACK_FIELD_TO_ROLE = {
    "MATNR": "material_identifier",
    "WERKS": "org_unit_plant",
    "MTART": "material_type",
    "MATKL": "material_group",
    "MBRSH": "industry_sector",
    "MEINS": "unit_of_measure",
    "SPRAS": "language_key",
    "MAKTX": "description_text",
    "BESKZ": "procurement_type",
    "DISMM": "mrp_type",
    "DISPO": "mrp_controller",
    "EKGRP": "purchasing_group",
    "LVORM": "status_flag",
    "MSTAE": "status_flag",
    "MMSTA": "status_flag",
    "ERSDA": "date_field",
    "MMSTD": "date_field",
    "BISMT": "old_material_ref",
    "BRGEW": "quantity_numeric",
    "NTGEW": "quantity_numeric",
    "MABST": "quantity_numeric",
}


def _build_field_role_index(roles: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Map SAP field name -> role id, from field_roles.yaml sap_examples,
    falling back to the built-in map for anything unlisted."""
    index: dict[str, str] = dict(_FALLBACK_FIELD_TO_ROLE)
    for role_id, entry in roles.items():
        examples = entry.get("sap_examples", []) or []
        for field_name in examples:
            index[field_name] = role_id
    return index


# ---------------------------------------------------------------------------
# The adapter: one RuleSpec dict -> one Template dict
# ---------------------------------------------------------------------------

def _adapt_rule(
    rule: dict[str, Any],
    field_role_index: dict[str, str],
    roles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Wrap a single imported RuleSpec (as a dict) into a template dict.

    Real serialised RuleSpec shape (from is_importer, model_dump_json with
    exclude_none, so None-valued keys are ABSENT on disk):
        rule_id            stable id of the source rule
        table              e.g. MARA
        fields             list[str]; the primary field is fields[0]
        dama_dimension     the DAMA dimension (enum .value, e.g. "Validity")
        archetype          enum .value, e.g. not_null | domain_in |
                           reference_exists (case handled by normalisation)
        assertion          {field, op, value}; value is the domain list for IN
        provenance         {source, original_name, original_expression,
                           reference_table?, rule_doc?}
        description        the rule's plain-language documentation
    """
    # Declare-first.
    rule_id = rule.get("rule_id")
    table = rule.get("table", "ANY")
    fields = rule.get("fields") or []                                   #v1.1
    field_name = fields[0] if fields else None                          #v1.1
    dimension = rule.get("dama_dimension")                              #v1.1
    archetype = rule.get("archetype", "")
    assertion = rule.get("assertion") or {}                             #v1.1
    assertion_value = assertion.get("value")                            #v1.1
    domain_values = assertion_value if isinstance(assertion_value, list) else []  #v1.1
    provenance_in = rule.get("provenance") or {}                        #v1.1
    # The rule's OWN provenance is authoritative for whether its values come
    # from a check table (reference) rather than the field's role.
    reference_table = provenance_in.get("reference_table")              #v1.1
    role_id = field_role_index.get(field_name) if field_name else None

    binding = {
        "target_table": table if table else "ANY",
        "target_field": field_name,
        "field_role": role_id,
    }

    applicability = _derive_applicability(archetype, domain_values, reference_table)
    parameterisation = _derive_parameters(archetype, domain_values, reference_table)

    template = {
        "template_id": f"TPL-{rule_id}" if rule_id else f"TPL-{table}-{field_name}",
        "source_rule_id": rule_id,
        "rule_spec": rule,  # denormalised copy keeps the bank self-contained
        "provenance": {
            "is_rule_id": rule_id,
            "original_expression": provenance_in.get("original_expression"),  #v1.1
            "nl_description": rule.get("description"),                          #v1.1
            "dimension": dimension,
        },
        "binding": binding,
        "applicability": applicability,
        "parameterisation": parameterisation,
        # Origin sets the default; a human Data Manager may later promote it.
        "prior_strength": {
            "strength": "strong",
            "strength_source": "default",
            "strength_reason": "proven_template",
            "set_by": None,
            "set_at": None,
            "note": None,
        },
    }
    return template


def _derive_applicability(
    archetype: str,
    domain_values: list[Any],
    reference_table: Optional[str],
) -> dict[str, Any]:
    """Propose STARTING applicability signals from the rule's archetype. These
    are recall-oriented defaults, meant to be refined, not gospel."""
    applicability: dict[str, Any] = {}
    archetype_lower = archetype.lower()

    if "not_null" in archetype_lower or "completeness" in archetype_lower:
        applicability["population_min"] = 0.98

    if "domain" in archetype_lower or "reference" in archetype_lower:
        applicability["type_hint"] = "categorical_string"
        applicability["population_min"] = applicability.get("population_min", 0.90)
        if domain_values:
            count = len(domain_values)
            applicability["distinct_count_min"] = 1
            applicability["distinct_count_max"] = max(count * 3, count + 5)
        if reference_table is not None:
            applicability["reference_match_min"] = 0.90

    if "format" in archetype_lower or "regex" in archetype_lower:
        applicability["type_hint"] = applicability.get("type_hint", "string")

    return applicability


def _derive_parameters(
    archetype: str,
    domain_values: list[Any],
    reference_table: Optional[str],
) -> list[dict[str, Any]]:
    """Declare each parameter's source of truth - the anti-overfitting handle."""
    parameters: list[dict[str, Any]] = []
    archetype_lower = archetype.lower()

    if "domain" in archetype_lower or "reference" in archetype_lower:
        if reference_table is not None:
            parameters.append({"name": "domain_values", "source": "reference"})
        elif domain_values:
            parameters.append({"name": "domain_values", "source": "template_fixed"})

    return parameters


# ---------------------------------------------------------------------------
# INTEGRATION POINT 1: load the imported rules
# ---------------------------------------------------------------------------

def load_source_rules(rules_dir: str | Path = "config/rules") -> list[dict[str, Any]]:  #v1.1
    """Return the imported IS rules as a list of serialised dicts.

    Reads the per-table YAMLs the importer already writes
    (config/rules/*_rules.yaml), each carrying a `rules` list of RuleSpec dicts,
    and concatenates them. Reading the YAMLs (rather than importing the RuleSpec
    class) keeps the bank decoupled from contracts.py and portable to Phase 2.
    The `*_rules.yaml` glob naturally excludes cross_field_examples.yaml and the
    JSON import report.
    """
    directory = Path(rules_dir)                                          #v1.1
    combined: list[dict[str, Any]] = []                                  #v1.1
    yaml_path: Path                                                      #v1.1
    for yaml_path in sorted(directory.glob("*_rules.yaml")):             #v1.1
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))      #v1.1
        entries = raw.get("rules", []) if isinstance(raw, dict) else []  #v1.1
        combined.extend(entries)                                         #v1.1
    return combined                                                      #v1.1


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_templates(
    rules: list[dict[str, Any]],
    roles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    field_role_index = _build_field_role_index(roles)
    templates: list[dict[str, Any]] = []
    for rule in rules:
        templates.append(_adapt_rule(rule, field_role_index, roles))
    return templates


def write_bank(templates: list[dict[str, Any]], out_path: Path) -> None:
    payload = {"version": 1, "templates": templates}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)


def _load_roles(roles_path: Path) -> dict[str, dict[str, Any]]:
    if load_field_roles is not None:
        return load_field_roles(roles_path)
    # Self-test fallback loader (no repo import available).
    raw = yaml.safe_load(roles_path.read_text(encoding="utf-8"))
    roles_list = raw.get("roles", []) if isinstance(raw, dict) else []
    roles_by_id: dict[str, dict[str, Any]] = {}
    for entry in roles_list:
        role_id = entry.get("id")
        if role_id is not None:
            roles_by_id[role_id] = entry
    return roles_by_id


# Mirrors the real on-disk shape: fields[], assertion{op,value}, provenance{},
# and exclude_none (absent keys where the importer had None). v1.1
_SELF_TEST_RULES = [
    {
        "rule_id": "IS_MARA_MEINS_REFERENCE_EXISTS",
        "name": "MEINS valid unit of measure",
        "table": "MARA",
        "dama_dimension": "Validity",
        "is_dimension": "Conformity",
        "archetype": "reference_exists",
        "severity": "MEDIUM",
        "description": "Base unit of measure must be a valid unit.",
        "fields": ["MEINS"],
        "assertion": {"field": "MEINS", "op": "in", "value": ["ST", "KG", "L", "M", "EA"]},
        "executable": True,
        "provenance": {
            "source": "information_steward",
            "original_name": "MEINS valid unit of measure",
            "original_expression": "exists(MEINS in T006)",
            "reference_table": "T006",
            "rule_doc": "Base unit must exist in the units-of-measure check table.",
        },
    },
    {
        "rule_id": "IS_MARA_MATKL_NOT_NULL",
        "name": "MATKL populated",
        "table": "MARA",
        "dama_dimension": "Completeness",
        "archetype": "not_null",
        "severity": "HIGH",
        "description": "Material group must be populated.",
        "fields": ["MATKL"],
        "assertion": {"field": "MATKL", "op": "is_not_null"},
        "executable": True,
        "provenance": {
            "source": "information_steward",
            "original_name": "MATKL populated",
            "original_expression": "MATKL IS NOT NULL",
        },
    },
    {
        "rule_id": "IS_MARC_BESKZ_DOMAIN_IN",
        "name": "BESKZ domain",
        "table": "MARC",
        "dama_dimension": "Validity",
        "archetype": "domain_in",
        "severity": "MEDIUM",
        "description": "Procurement type must be one of E, F, X.",
        "fields": ["BESKZ"],
        "assertion": {"field": "BESKZ", "op": "in", "value": ["E", "F", "X"]},
        "executable": True,
        "provenance": {
            "source": "information_steward",
            "original_name": "BESKZ domain",
            "original_expression": "BESKZ IN ('E','F','X')",
        },
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the AgentDQ rule bank.")
    parser.add_argument("--self-test", action="store_true", help="Build from a built-in sample, no repo deps.")
    parser.add_argument("--roles", default="config/rule_bank/field_roles.yaml", help="Path to field_roles.yaml.")
    parser.add_argument("--rules-dir", default="config/rules", help="Directory of *_rules.yaml to wrap.")  #v1.1
    parser.add_argument("--out", default="config/rule_bank/templates.yaml", help="Output templates YAML path.")
    args = parser.parse_args()

    roles_path = Path(args.roles)
    out_path = Path(args.out)
    roles = _load_roles(roles_path)

    if args.self_test:
        rules = _SELF_TEST_RULES
        print(f"Self-test: building from {len(rules)} sample rules.")
    else:
        rules = load_source_rules(args.rules_dir)  #v1.1
        print(f"Building from {len(rules)} imported rules.")

    templates = build_templates(rules, roles)
    write_bank(templates, out_path)
    print(f"Wrote {len(templates)} templates to {out_path}.")

    # A small readout so a human can eyeball the derivation.
    for template in templates:
        binding = template["binding"]
        source = template["parameterisation"]
        source_label = source[0]["source"] if source else "none"
        print(
            f"  {template['template_id']:<18} "
            f"role={binding['field_role'] or '-':<20} "
            f"param_source={source_label}"
        )


if __name__ == "__main__":
    main()
