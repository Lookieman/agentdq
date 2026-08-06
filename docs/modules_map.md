# AgentDQ - Module and Configuration Map

A single reference for where every module and configuration file lives, so the
repository's unique-filename rule is easy to honour and stale copies are easy to
spot. Versions are the latest change-log entry at the top of each file at the
time of writing; treat them as a snapshot for verifying your working tree is in
sync, not as a live value.

*Updated 04-Aug-2026 (Package 4a - the uniqueness configuration): schema v0.4
extends UniquenessConfig with scope, methods and bands, turns blocking_key into
blocking_keys (a list) and gives each compare field a weight. build_schema.py
v0.3 fixes a real bug - TABLE_META held no file_pattern and no uniqueness block,
so every schema rebuild silently erased both from mara.yaml.
Prior update 20-Jul-2026: adds the LangGraph orchestration (Package 3) - graph
state, nodes, orchestrator, the two graph runners and the interrupt example -
plus the no-checks guard on the assessment runner.
All versions in this map are now confirmed against the GitHub repo clone (the
earlier [P1] "not re-read" tags are dropped). Prior updates added the rule bank,
reference store, agentic core, DSPy signatures, the rules repository and gate
surfaces, the onboarding scaffolder, and the schema refactor (onboarding config
lives in the schema; the short-lived config/objects experiment is retired).*

## Shared contracts

```
Path                    Ver    Purpose
----------------------  -----  -------------------------------------------------
src/contracts.py        v0.3   Enums, Finding, DefectLabel, predicate-tree IR
                               (Comparison / BoolNode / RuleSpec), IS-to-DAMA map.
                               NB: CandidateSuggestion lives in rule_suggester.py
                               and PriorStrengthBlock in rule_bank.py, not here.
```

## Data foundation

```
Path                          Ver    Purpose
----------------------------  -----  -------------------------------------------
src/data/extract_loader.py    v0.3   Load SE16N/SE12 xlsx extracts (leading
                                     zeros, preamble, spacer column)
src/data/profiler.py          v0.5   Deterministic profiler; per-field stats and
                                     composite-key uniqueness. Reads header
                                     anchor / primary key from the schema
                                     (hardcoded dicts are fallbacks)
src/data/schema.py            v0.4   Schema loader and parsers (comma decimals,
                                     null-date sentinel, record keys). The
                                     single onboarding contract: primary_key,
                                     header_anchor, file_pattern, uniqueness.
                                     v0.4 adds the uniqueness dials (scope,
                                     blocking_keys, weighted compare_fields,
                                     methods, bands), effective_bands() for
                                     steward-versus-advisory precedence, and
                                     fingerprint() so a run records its settings
src/data/generator.py         v0.1   Synthetic clean-baseline generator,
                                     calibrated to the profiles
src/data/defect_injector.py   v0.4   Controlled defect injection with
                                     ground-truth labels
```

## Rules - ingestion and execution

```
Path                        Ver    Purpose
--------------------------  -----  ---------------------------------------------
src/rules/is_importer.py    v0.3   Parse the IS workbook into RuleSpec IR
src/rules/rule_loader.py    v0.2   Load rule YAMLs into RuleSpec objects. Reads
                                   the repository's exported approved rules
                                   unchanged (same {table, rule_count, rules}
                                   shape)
src/rules/executor.py       v0.1   Pandas executor; walks the predicate IR with
                                   three-valued logic, emits Findings
```

## Rules - bank, reference and repository (agentic core)

```
Path                          Ver    Purpose
----------------------------  -----  -------------------------------------------
src/rules/rule_bank.py        v1.0   Template schema (RuleSpec + match metadata,
                                     PriorStrengthBlock) and the deterministic
                                     retrieval join
src/rules/reference_store.py  v1.3   Check-table layer: loads reference xlsx via
                                     extract_loader, membership / match-rate /
                                     as-of metadata; values() for instantiation
src/rules/repository.py       v1.0   Approved-rule store and lifecycle (draft ->
                                     approved | rejected -> retired); File and
                                     Session backends; ledger; manual candidates;
                                     export_approved() for the executor
```

## Agents - execution layer

```
Path                          Ver    Purpose
----------------------------  -----  -------------------------------------------
src/agents/base.py            v0.1   BaseAgent, RuleBackedAgent, AgentResult
src/agents/completeness.py    v0.1   Completeness dimension (not-null rules)
src/agents/validity.py        v0.1   Validity dimension (domain rules)
src/agents/consistency.py     v0.1   Consistency dimension (cross-field rules)
```

## Agents - agentic core (DSPy)

```
Path                               Ver    Purpose
---------------------------------  -----  --------------------------------------
src/agents/profile_interpreter.py  v1.3   Profiling Agent: field characterisations
                                          (roles + evidence) and a human readout;
                                          the unknown-role escape hatch
src/agents/rule_suggester.py       v1.0   Rule Suggestion Agent: bank-match
                                          adjudication + data-driven inference;
                                          decomposable confidence;
                                          CandidateSuggestion shape
```

## DSPy modules

```
Path                                        Ver    Purpose
------------------------------------------  -----  ----------------------------
src/dspy_modules/suggestion_signatures.py   v1.1   DSPy signatures: profile
                                                   interpretation, bank-match
                                                   adjudication, data-driven
                                                   inference
```

## Orchestration (LangGraph)

```
Path                   Ver    Purpose
---------------------  -----  ------------------------------------------------
src/state.py           v1.0   Typed graph state for both graphs; reducers for
                              the parallel fan-out (findings, agent_results,
                              upstream_advisories)
src/graph_nodes.py     v1.1   Thin node functions (unpack -> run() -> pack) and
                              the two advisory derivations (threshold modifier,
                              signal suppression). v1.1 reads the field name off
                              a CompareField rather than a plain string
src/orchestrator.py    v1.0   Both compiled StateGraphs; the three-dimension
                              parallel fan-out with a join at aggregate
```

Agents import nothing from this layer; nodes import agents. The three dimension
agents fan out in parallel, so their shared state keys carry reducers (see
design doc 4.3).

## Reporting

```
Path                           Ver    Purpose
-----------------------------  -----  ------------------------------------------
src/reporting/scorecard.py     v0.1   DQ scorecard, ground-truth evaluation,
                                      console rendering
src/reporting/assessment.py    v0.1   Shared assess() used by CLI and dashboard
```

## Application

```
Path                    Ver    Purpose
----------------------  -----  -------------------------------------------------
app/dashboard.py        v0.1   Streamlit dashboard (scorecard, findings,
                               evidence, Phase 2 case)
app/gate.py             v1.1   Suggestion review surface (the approval gate):
                               decomposed confidence card, approve / edit /
                               reject, manual rule authoring
app/bank_browser.py     v1.1   Rule Bank browser + strength governance
                               (human-only, audited promotion)
```

## Tools (one-off / command-line)

```
Path                       Ver    Purpose
-------------------------  -----  ----------------------------------------------
tools/build_schema.py      v0.3   Scaffold schema YAMLs from profiles + overlay.
                                  TABLE_META now carries file_pattern and MARA's
                                  uniqueness block, so a rebuild no longer
                                  erases them
tools/run_assessment.py    v0.2   Console assessment driver (calls assess())
tools/build_rule_bank.py   v1.1   Wrap the imported RuleSpecs into rule-bank
                                  templates with match metadata
tools/onboard_object.py    v2.1   Deterministic onboarding scaffolder: detects
                                  header + composite key, maps roles, reports
                                  reference readiness, emits a DRAFT schema.
                                  v2.1 emits the schema v0.4 uniqueness shape
tools/run_suggestion.py    v1.2   Batch runner: profile -> interpret -> suggest
                                  -> write the suggestions artefact; configures
                                  the LM (loads .env, --model flag)
tools/run_suggestion_graph.py  v1.0  Suggestion graph runner (LangGraph); reuses
                                     run_suggestion's helpers
tools/run_assessment_graph.py  v1.1  Assessment graph runner (LangGraph): the
                                     parallel dimension fan-out, real scorecard;
                                     no-checks guard (0 rules -> warn, not 100%)
tools/interrupt_example.py     v1.0  The LangGraph interrupt() primitive AgentDQ
                                     chose against, kept runnable (design 4.2)
```

## Tests

```
Path                                     Ver    Purpose
---------------------------------------  -----  -----------------------------
tests/test_pipeline_smoke.py             v0.5   End-to-end Phase 1 smoke suite
                                                (execution layer)
tests/test_rule_bank_smoke.py            v1.3   Rule bank retrieval + reference
                                                store (incl. real extracts)
tests/test_profile_interpreter_smoke.py  v1.1   Profiling Agent grounding +
                                                real-profiler reconciliation
tests/test_rule_suggester_smoke.py       v1.0   Both suggestion engines +
                                                confidence + IR round-trip
tests/test_repository_smoke.py           v1.0   Lifecycle, ledger, session
                                                isolation, the full closed loop
tests/test_onboarding_smoke.py           v2.1   Schema onboarding config + the
                                                scaffolder on a synthetic EQKT.
                                                v2.1 moves to the v0.4 shape and
                                                proves the scaffolded draft
                                                actually LOADS, not just that it
                                                contains the right TODO strings
tests/test_orchestrator_smoke.py         v1.1   Both graphs; assessment fan-out
                                                with the real executor;
                                                advisories; the no-checks guard.
                                                v1.1 fixture uses blocking_keys
tests/test_uniqueness_config.py          v1.0   Schema v0.4 uniqueness dials:
                                                weight normalisation, band order,
                                                band precedence, fingerprint,
                                                loud rejection of the old
                                                blocking_key spelling, and a
                                                round trip through the shipped
                                                config/schema/mara.yaml
```

Test position: 106 tests pass and 1 is skipped across the whole suite (the
agentic-core, Package 2 and Package 3 suites, the Package 4a configuration
suite, plus the Phase 1 pipeline suite). No LLM or API key is required for any
of them.

## Configuration - schema

```
Path                        Purpose
--------------------------  -----------------------------------------------------
config/schema/mara.yaml     MARA field contract (23 fields) + onboarding config.
                            Uniqueness (v0.4): blocks on MTART AND MEINS,
                            compares MAKT.MAKTX, bands 0.92 / 0.80
config/schema/marc.yaml     MARC field contract (29 fields) + onboarding config
config/schema/makt.yaml     MAKT field contract (5 fields) + onboarding config
```

Onboarding fields (primary_key, header_anchor, file_pattern, uniqueness) live
here, one file per table. Note the two DISTINCT role vocabularies (see Notes).

These files are GENERATED by `tools/build_schema.py`, so anything hand-added to
them is erased on the next rebuild. Everything a steward must keep therefore
belongs in that tool's TABLE_META, which is where file_pattern and MARA's
uniqueness block now live (v0.3). A rebuilt MARA schema produces the same
uniqueness fingerprint as the file on disk, which is the check that this holds.

## Configuration - rule bank

```
Path                                  Purpose
------------------------------------  -------------------------------------------
config/rule_bank/field_roles.yaml     Controlled semantic role vocabulary
                                      (16 roles + unknown); hand-authored
config/rule_bank/templates.yaml       The wrapped rule bank; GENERATED by
                                      tools/build_rule_bank.py, then committed
```

## Configuration - reference tables

```
Path                             Purpose
-------------------------------  ------------------------------------------------
config/reference/manifest.yaml   The ten reference tables with as-of metadata
                                 (source system, extract date, key columns,
                                 status)
config/reference/T*.xlsx         The reference extracts (T006, T001W, T134, T023,
                                 T024D, T137 loaded; T002, T024, T438A, T141 as
                                 extracted). Loaded via extract_loader
```

## Configuration - rules

```
Path                                  Purpose
------------------------------------  -------------------------------------------
config/rules/mara_rules.yaml          Imported MARA rules (RuleSpec IR)
config/rules/marc_rules.yaml          Imported MARC rules (RuleSpec IR)
config/rules/makt_rules.yaml          Imported MAKT rules (RuleSpec IR)
config/rules/cross_field_examples.yaml Curated cross-field rules (one active
                                      IMPLIES rule, one deactivated OR example)
config/rules/_import_report.json      Import summary (counts, deferred list)
```

## Runtime stores (generated, not source)

```
Path                             Purpose
-------------------------------  ------------------------------------------------
data/repository/candidates.yaml  FileRepository snapshot of all candidate records
data/repository/ledger.jsonl     Append-only audit ledger (every transition)
artefacts/suggestions_*.json     Batch suggestion runs (input to the gate and
                                 the demo replay)
data/approved/*_rules.yaml       Approved rules exported for the executor, in the
                                 importer's shape
```

## Package markers

Empty `__init__.py` files mark the Python packages:

```
src/__init__.py            src/agents/__init__.py       src/data/__init__.py
src/reporting/__init__.py  src/rules/__init__.py        src/dspy_modules/__init__.py
tools/__init__.py          tests/__init__.py            app/__init__.py
```

## Notes

- **Unique filenames.** No two modules share a filename, even across
  directories. The historical clash was `data/loader.py` versus
  `rules/loader.py`, since renamed to `extract_loader.py` and `rule_loader.py`.
- **Two role vocabularies, same word.** `FieldSpec.role` in the schema is
  STRUCTURAL (key / attribute / flag / temporal / client) and drives parsing and
  generation. `field_role` in `config/rule_bank/field_roles.yaml` is SEMANTIC
  (unit_of_measure, material_type, ...) and drives template retrieval. They
  share a word, not a meaning; do not conflate them.
- **Retired experiment.** `config/objects/` and `src/data/object_packs.py` were
  a short-lived parallel onboarding config. The schema already carried
  primary_key and header_anchor, so onboarding config was folded back into the
  schema (v0.3). Remove both if they appear in your tree.
- **Stale leftovers (action needed).** The repo still contains
  `src/rules/loader.py` (v0.1) - the pre-rename original of `rule_loader.py`
  (v0.2). Its own changelog records the rename; the old file was never deleted.
  Run `git rm src/rules/loader.py` - it is dead code and re-creates the exact
  filename clash this map guards against. Likewise `src/models.py` or
  `src/schema_utils.py`, if present, are earlier-build remnants and can go.
- **Uniqueness settings fail at load time, not at run time.** A wrong dial in a
  schema YAML raises a clear error while the file is being read: bands out of
  order, an unknown fuzzy metric, both methods weighted zero, a compare field
  weighted zero, or the pre-v0.4 singular `blocking_key`. That last guard
  matters most - pydantic ignores keys it does not know, so an unguarded old
  file would load with NO blocking at all and compare every record against
  every other one.
- **LangGraph** is a runtime dependency (Package 3 orchestration): `uv add
  langgraph`. Agents never import it; only src/state.py, src/graph_nodes.py,
  src/orchestrator.py and the graph runners do.
- **This map now lives in the repo** under `docs/`, alongside
  `agentdq_design.md` and `agentdq-project-plan.md`, so it travels with the
  code. Update all three in the same session as any package build.
- **Generated artefacts** (profiles under `data/profile/`, synthetic datasets
  under `data/synthetic/`, `mlruns/`, and the runtime stores above) are not
  source; they are reproducible outputs and are git-ignored, except the
  committed generated configs (`config/rules/*`, `config/rule_bank/templates.yaml`)
  which are versioned deliberately.
