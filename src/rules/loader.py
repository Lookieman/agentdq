# v0.1 | 27-Jun-2026 | Initial rules loader (YAML -> RuleSpec)

"""Loader for the rule YAMLs produced by the IS importer and curated by hand.

Reads every rule file in a directory into RuleSpec objects. Both the defect
injector and the rule executor consume this, so the parse lives in one place.
Files are expected to contain a top-level 'rules' list; the import report
(_import_report.json) and any non-YAML files are ignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.contracts import RuleSpec


def load_rules(rules_dir: str) -> list[RuleSpec]:
    """Load all rules from every *.yaml file in a directory."""
    base: Path = Path(rules_dir)
    rules: list[RuleSpec] = []
    yaml_path: Path = None
    payload: dict[str, Any] = {}
    rule_dict: dict[str, Any] = {}

    for yaml_path in sorted(base.glob("*.yaml")):
        payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if not payload or "rules" not in payload:
            continue
        for rule_dict in payload["rules"]:
            rules.append(RuleSpec.model_validate(rule_dict))

    return rules


def rules_for_table(rules: list[RuleSpec], table: str) -> list[RuleSpec]:
    """Filter a rule list to one table."""
    return [rule for rule in rules if rule.table == table]
