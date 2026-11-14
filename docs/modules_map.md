# AgentDQ - Module and Configuration Map

A single reference for where every module and configuration file lives, so the
repository's unique-filename rule is easy to honour and stale copies are easy to
spot. Versions are the latest change-log entry at the top of each file at the
time of writing; treat them as a snapshot for verifying your working tree is in
sync, not as a live value.

*Updated 13-Nov-2026 (Package 4z - close-out; Package 4 complete): FileRepository
(src/rules/repository.py v1.1) takes an optional approved_dir and refreshes the
approved-rules export after every state change; the gate and the bank browser
pass AGENTDQ_APPROVED_DIR (default data/approved), and data/approved/ is now a
tracked store. The dashboard names the tables that have approved rules. The
graph runner's usage text names the module form. load_labels()
(src/reporting/assessment.py v0.6) keeps the label detail, so the adjudicator's
report card counts twins again. Versions below are re-checked
against every file header; the 4g, 4h and 4z tests are listed.*

*Prior update 11-Aug-2026 (Packages 4g and 4h - the adjudicator and remediation;
Package 4 complete): the attribute veto (src/agents/text_normaliser.py v1.1,
tapped in src/agents/uniqueness.py v1.2) caps a pair whose digit groups or
material words disagree, BEFORE any model call; the dials live in the schema
(uniqueness.attribute_veto, cap default 0.75, alternatives in mara.yaml). The
adjudicator (src/agents/adjudicator.py) judges the uncertain band through a
batch CLI (tools/run_adjudication.py) that writes verdicts.json BESIDE the
dataset; uniqueness_node applies it offline: confirmed pairs become
needs_steward clusters and findings carrying the model's confidence, rejected
pairs leave the open list, and a fingerprint mismatch applies NOTHING and is
shown as stale. Remediation (src/agents/remediation.py) groups findings,
ranks by severity x affected records with a 1.5 mandatory lift, turns
held-back records and fuzzy-only mode into actions, and attaches the model
narrative from a plan artefact (tools/run_remediation.py) only under a
matching fingerprint. Shared LM setup: src/dspy_modules/lm_config.py (CLI
flag, then AGENTDQ_MODEL, then openai/gpt-4o-mini, temperature 0).*

*Prior update 10-Aug-2026 (Package 4f fix - the block pair ceiling): a block above the
ceiling is HELD BACK rather than raising, so one large block cannot end a whole
assessment, and the ceiling moves from a module constant to a schema dial
(uniqueness.max_block_pairs, default 20,000,000). Found because the CLEAN
baseline dataset crashed and the DEGRADED one did not: degraded holds 342
records back, and that alone kept its largest block under the old 5,000,000
limit. tools/build_schema.py also still held jaro_winkler as the MARA fuzzy
metric, so the next schema rebuild would have reverted the Package 4e default.
Prior update 10-Aug-2026 (Package 4f - the dashboard moves onto the graph): assess()
now BUILDS and INVOKES the assessment graph, so the dashboard, the console driver
and the graph runner run ONE path and cannot disagree. A new module,
src/reporting/cluster_view.py, holds the display shaping for the Duplicates and
Settings tabs, so those tables are testable without Streamlit. Two defects were
fixed: the graph runner scored three dimensions and left Uniqueness out, and
scorecard_node never passed the per-dimension denominator, so the Uniqueness score
was divided by every row of every loaded table. ACTION: src/agents/uniqueness_eval.py
is a byte-identical duplicate of src/reporting/uniqueness_eval.py, imported by
nothing - run `git rm src/agents/uniqueness_eval.py`.
Prior update 04-Aug-2026 (Package 4c - the embeddings artefact): a shared text
normaliser now decides what text BOTH scoring rungs see, and the batch builder
writes one vector file per compare field BESIDE its dataset. Each file carries
an identity code (model, field, language, normalisation) and a content code (the
keys and the text), so stale or foreign vectors are refused rather than used.
Prior update 04-Aug-2026 (Package 4b - structured advisories): advice between agents
is now a small dictionary with six named keys instead of a sentence, and signal
suppression is replaced by RECORD EXCLUSION - a description that failed a
validity check holds its record out of deduplication. A new module,
src/agents/uniqueness_settings.py, resolves a steward's settings against the
advice that arrives.
Prior update 04-Aug-2026 (Package 4a - the uniqueness configuration): schema v0.4
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
src/contracts.py        v0.6   Enums, Finding, DefectLabel, predicate-tree IR,
                               DuplicateCluster, PairVerdict, RemediationAction.
                               v0.4 adds AdvisoryAction, the vocabulary one
                               agent uses to advise another
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
src/data/profiler.py          v0.6   Deterministic profiler; per-field stats and
                                     composite-key uniqueness. Reads header
                                     anchor / primary key from the schema
                                     (hardcoded dicts are fallbacks)
src/data/schema.py            v0.6   Schema loader and parsers (comma decimals,
                                     null-date sentinel, record keys). The
                                     single onboarding contract: primary_key,
                                     header_anchor, file_pattern, uniqueness.
                                     v0.4 adds the uniqueness dials (scope,
                                     blocking_keys, weighted compare_fields,
                                     methods, bands), effective_bands() for
                                     steward-versus-advisory precedence, and
                                     fingerprint() so a run records its settings
src/data/generator.py         v0.3   Synthetic clean-baseline generator,
                                     calibrated to the profiles
src/data/defect_injector.py   v0.5   Controlled defect injection with
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
src/rules/repository.py       v1.1   Approved-rule store and lifecycle (draft ->
                                     approved | rejected -> retired); File and
                                     Session backends; ledger; manual candidates;
                                     export_approved() for the executor. v1.1:
                                     FileRepository(repo_dir, approved_dir=None)
                                     refreshes the export after every state
                                     change (old *_rules.yaml files deleted
                                     first); SessionRepository never exports
```

## Agents - execution layer

```
Path                          Ver    Purpose
----------------------------  -----  -------------------------------------------
src/agents/base.py            v0.2   BaseAgent, RuleBackedAgent, AgentResult
src/agents/completeness.py    v0.1   Completeness dimension (not-null rules)
src/agents/validity.py        v0.1   Validity dimension (domain rules)
src/agents/consistency.py     v0.1   Consistency dimension (cross-field rules)
src/agents/text_normaliser.py v1.1   The ONE text normaliser, plus the veto
                               signatures (digit groups, material words).
                                     accents, punctuation to space, spaces
                                     collapsed. The embeddings builder and the
                                     matcher both call it, so the fuzzy score
                                     and the semantic score always describe the
                                     same text
src/agents/uniqueness_settings.py
                              v1.2   Resolves a steward's uniqueness settings
                                     against the advisories that arrive:
                                     effective bands (with the arithmetic
                                     shown), effective compare fields, and the
                                     records to hold out of deduplication.
                                     Pure - no data, no pandas, no LLM
src/agents/uniqueness.py      v1.2   Uniqueness agent: hold back, block, score
                                     (fuzzy and semantic rungs, attribute veto),
                                     cluster, survive. No model call
src/agents/embedding_store.py v1.0   One vector file per compare field, with an
                                     identity code and a content code, so stale
                                     or foreign vectors are refused. The only
                                     module that loads an embedding model
src/agents/adjudicator.py     v1.0   judge() (capped model calls on the band),
                                     the verdicts artefact, and
                                     apply_verdicts(): deterministic application
                                     with the stale gate
src/agents/remediation.py     v1.0   group_findings, build_actions (rank =
                                     severity x records x mandatory lift), the
                                     plan artefact and attach_narrative with a
                                     stale gate
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
src/dspy_modules/lm_config.py               v1.0   Shared LM setup for every DSPy
                                                   program: CLI flag, then
                                                   AGENTDQ_MODEL, then
                                                   openai/gpt-4o-mini, temp 0.
src/dspy_modules/adjudication_signatures.py v1.0   PairAdjudication (one uncertain
                                                   pair) and RemediationSynthesis
                                                   (the plan narrative, one call).
src/dspy_modules/suggestion_signatures.py   v1.1   DSPy signatures: profile
                                                   interpretation, bank-match
                                                   adjudication, data-driven
                                                   inference
```

## Orchestration (LangGraph)

```
Path                   Ver    Purpose
---------------------  -----  ------------------------------------------------
src/state.py           v1.2   Typed graph state for both graphs; reducers for
                              the parallel fan-out (findings, agent_results,
                              upstream_advisories). v1.1 carries advisories as
                              dictionaries and adds uniqueness_settings
src/graph_nodes.py     v1.5   Thin node functions (unpack -> run() -> pack) and
                              the two advisory derivations. v1.1 reads the field
                              name off a CompareField. v1.2 emits dictionaries
                              rather than sentences, replaces signal suppression
                              with record exclusion, and makes the uniqueness
                              stub resolve real settings
src/orchestrator.py    v1.2   Both compiled StateGraphs; the three-dimension
                              parallel fan-out with a join at aggregate
```

Agents import nothing from this layer; nodes import agents. The three dimension
agents fan out in parallel, so their shared state keys carry reducers (see
design doc 4.3).

## Reporting

```
Path                           Ver    Purpose
-----------------------------  -----  ------------------------------------------
src/reporting/scorecard.py     v0.2   DQ scorecard, ground-truth evaluation,
                                      console rendering. A dimension may state
                                      its OWN denominator
src/reporting/assessment.py    v0.6   Shared assess(): builds and invokes the
                                      assessment graph, returns the scorecard,
                                      the clusters, the resolved uniqueness
                                      settings and the cluster-level evaluation.
                                      Also holds the no-checks guard, so the
                                      dashboard need not import from tools/
src/reporting/uniqueness_eval  v1.1   Twin recall, decoy error rate, unlabelled
  .py                                 joins and the score spread by change
src/reporting/cluster_view.py  v1.3   Display shaping for the Duplicates, Actions and
                                      Settings tabs: cluster tables, candidate
                                      pairs, the settings in force, the match
                                      mode note, and (v1.3) the note naming the
                                      tables with approved rules. Pure - no
                                      Streamlit import
```

## Application

```
Path                    Ver    Purpose
----------------------  -----  -------------------------------------------------
app/dashboard.py        v0.6   Streamlit dashboard, 7 tabs: Scorecard,
                               Findings, Duplicates, Actions, Evidence,
                               Settings, About. Rules-directory selector (with
                               the approved-tables caption), no-checks guard,
                               match-mode banner
app/gate.py             v1.2   Suggestion review surface (the approval gate):
                               decomposed confidence card, approve / edit /
                               reject, manual rule authoring. Passes
                               AGENTDQ_APPROVED_DIR to FileRepository
app/bank_browser.py     v1.2   Rule Bank browser + strength governance
                               (human-only, audited promotion). Passes
                               AGENTDQ_APPROVED_DIR to FileRepository
```

## Tools (one-off / command-line)

```
Path                       Ver    Purpose
-------------------------  -----  ----------------------------------------------
tools/build_embeddings.py  v1.1   Batch builder for the semantic vectors. One
                                  .npz per compare field, written to
                                  <dataset>/embeddings/. Never runs inside
                                  Streamlit: the model is too heavy for the
                                  free tier. The encoder is passed in, so the
                                  tests never load it
tools/build_schema.py      v0.6   Scaffold schema YAMLs from profiles + overlay.
                                  TABLE_META now carries file_pattern and MARA's
                                  uniqueness block, so a rebuild no longer
                                  erases them
tools/run_assessment.py    v0.4   Console assessment driver (calls assess());
                               prints verdicts and the ranked plan.
                                  Prints the uniqueness stage and the cluster-
                                  level evaluation
tools/build_rule_bank.py   v1.1   Wrap the imported RuleSpecs into rule-bank
                                  templates with match metadata
tools/onboard_object.py    v2.1   Deterministic onboarding scaffolder: detects
                                  header + composite key, maps roles, reports
                                  reference readiness, emits a DRAFT schema.
                                  v2.1 emits the schema v0.4 uniqueness shape
tools/run_suggestion.py    v1.3   Batch runner: profile -> interpret -> suggest
                                  -> write the suggestions artefact; configures
                                  the LM (loads .env, --model flag)
tools/run_adjudication.py  v1.0   Judge the uncertain band (capped, highest
                               first, artefact-as-cache); writes verdicts.json
                               beside the dataset.
tools/run_remediation.py   v1.0   One model call writes the plan narrative into
                               artefacts/remediation/; a failed validation
                               writes nothing and the template plan stands.
tools/run_suggestion_graph.py  v1.0  Suggestion graph runner (LangGraph); reuses
                                     run_suggestion's helpers
tools/run_assessment_graph.py  v1.6  Assessment graph runner (LangGraph): the
                                     parallel dimension fan-out, real scorecard;
                                     no-checks guard (0 rules -> warn, not 100%).
                                     Run as a module only. Does not print
                                     verdicts; tools/run_assessment.py does
tools/benchmark_embedding_models.py
                               v1.1  Compares candidate embedding models on twin
                                     recall, decoy error rate and uncertain pair
                                     count. Can call the OpenAI API (paid)
tools/interrupt_example.py     v1.0  The LangGraph interrupt() primitive AgentDQ
                                     chose against, kept runnable (design 4.2)
```

## Tests

```
Path                                     Ver    Purpose
---------------------------------------  -----  -----------------------------
tests/test_pipeline_smoke.py             v0.7   End-to-end Phase 1 smoke suite
                                                (execution layer)
tests/test_rule_bank_smoke.py            v1.3   Rule bank retrieval + reference
                                                store (incl. real extracts)
tests/test_profile_interpreter_smoke.py  v1.1   Profiling Agent grounding +
                                                real-profiler reconciliation
tests/test_rule_suggester_smoke.py       v1.0   Both suggestion engines +
                                                confidence + IR round-trip
tests/test_repository_smoke.py           v1.1   Lifecycle, ledger, session
                                                isolation, the full closed loop.
                                                v1.1: the automatic approved-
                                                rules export (approve writes a
                                                loadable file, a retire removes
                                                a table's file, no folder means
                                                no export, session writes
                                                nothing)
tests/test_onboarding_smoke.py           v2.1   Schema onboarding config + the
                                                scaffolder on a synthetic EQKT
tests/test_orchestrator_smoke.py         v1.3   Both graphs; assessment fan-out
                                                with the real executor;
                                                advisories; the no-checks guard.
                                                v1.1 fixture uses blocking_keys
tests/test_embeddings.py                 v1.1   The shared normaliser and the
                                                embeddings builder. Offline: a
                                                small fake encoder stands in for
                                                the model. Checks an artefact's
                                                SHAPE and round trip, never its
                                                numbers, because encoding is not
                                                bit-identical across platforms
tests/test_advisories.py                 v1.0   Structured advisories: the six
                                                keys, loud failure on an unknown
                                                or malformed one, largest-shift
                                                combination, and record exclusion
                                                driven by validity findings
tests/test_uniqueness_config.py          v1.0   Schema v0.4 uniqueness dials:
                                                weight normalisation, band order,
                                                band precedence, fingerprint,
                                                loud rejection of the old
                                                blocking_key spelling, and a
                                                round trip through the shipped
                                                config/schema/mara.yaml
tests/test_uniqueness_agent.py           v1.0   The matcher's five stages: hold
                                                back, block, score, cluster,
                                                survive. Fake encoder
tests/test_uniqueness_evaluation.py      v1.0   Package 4e: harder near-copies,
                                                decoy pairs, cluster measurement
tests/test_assess_graph_path.py          v1.4   assess() runs the graph; scored
                                                dimensions and denominator. v1.3
                                                (4g/4h): verdicts end to end,
                                                stale verdicts refused, the
                                                ranked plan. v1.4 (4z): labels
                                                keep duplicate_of through
                                                load_labels(), so the report
                                                card counts twins
tests/test_cluster_view.py               v1.2   Display shaping for the
                                                Duplicates, Actions and Settings
                                                tabs. v1.2: the approved-tables
                                                note
tests/test_attribute_veto.py             v1.0   Package 4g: the veto caps a pair
                                                only when digit groups or
                                                material words disagree
tests/test_adjudicator.py                v1.0   Package 4g: judge() with a fake
                                                model, the cap, the verdicts
                                                artefact, apply_verdicts() and
                                                the stale gate
tests/test_lm_config.py                  v1.0   Package 4g: model resolution
                                                order (flag, AGENTDQ_MODEL,
                                                default). No DSPy import
tests/test_remediation.py                v1.0   Package 4h: grouping, ranking,
                                                the plan artefact, and the
                                                narrative with a fake model
```

Test position (14-Nov-2026): 291 tests pass and 0 are skipped, with the real
CAL extract in data/raw. Without it, one test skips. This number is MEASURED each
time, not carried forward. A stale "84 passing, 1 skipped" sat in this file for
several packages while seven tests skipped in silence, because the pipeline test
looked for the profiles in data/profile and they live in data/profiles. A
skipped test reports as a pass at a glance, so a wrong path can hide for a long
time; always read the skip reasons with `uv run pytest -q -rs` across the whole suite (the
agentic-core, Package 2 and Package 3 suites, the Package 4a and 4b suites, plus
the Phase 1 pipeline suite). No LLM or API key is required for any
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
                                      IMPLIES rule, one deactivated OR example).
                                      HAND-WRITTEN and committed; the importer
                                      does not make it
config/rules/_import_report.json      Import summary (counts, deferred list)
```

The importer writes the three `*_rules.yaml` files and the import report.
`cross_field_examples.yaml` is a hand-written input, not an importer output, so
a re-import leaves it in place and it must not be deleted. The defect injector
reads every rule file in its `--rules` folder, so this file must sit beside the
imported rules. Without it the degraded set has no Consistency defects (9,232
labels instead of 10,596). The regeneration commands, in dependency order, are
in `README.md` under "Regenerate the data".

## Runtime stores (written by the code, tracked in git)

```
Path                             Purpose
-------------------------------  ------------------------------------------------
data/repository/candidates.yaml  FileRepository snapshot of all candidate records
data/repository/ledger.jsonl     Append-only audit ledger (every transition)
artefacts/suggestions_*.json     Batch suggestion runs (input to the gate and
                                 the demo replay). Only suggestions_mara.json
                                 is tracked
data/approved/*_rules.yaml       Approved rules exported for the executor, in the
                                 importer's shape. Rewritten by FileRepository
                                 after every state change when the gate or the
                                 bank browser runs. Tracked, so a fresh clone
                                 runs the approved set. Default --rules of the
                                 graph runner
```

These stores change only through the `RulesRepository` verbs (and the suggestion
runner for the artefact). Never edit them by hand.

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
- **Stale leftovers (removed).** `src/agents/uniqueness_eval.py` (a duplicate
  of `src/reporting/uniqueness_eval.py`) and `src/rules/loader.py` (the
  pre-rename original of `rule_loader.py`) are gone, as are `src/models.py` and
  `src/schema_utils.py`. Checked 13-Nov-2026. If one reappears in your tree,
  remove it.
- **Vectors live beside their dataset, never in one shared folder.** A single
  `artefacts/embeddings/` folder would hold one dataset's vectors at a time, so
  changing dataset on the dashboard would need a rebuild, and a rebuild needs
  the model that the free tier cannot load. The per-dataset path also keeps the
  files out of git with no `.gitignore` change, since `data/raw/` and
  `data/synthetic/` are already ignored.
- **Advice that cannot be acted on FAILS.** An advisory with an unknown action,
  a missing key, or a threshold request carrying no number raises rather than
  being skipped. A silently dropped advisory is the worst outcome available: the
  upstream agent would believe its advice was taken, the report would list it as
  delivered, and nothing would ever show the gap.
- **Record exclusion, not signal suppression.** MARA compares ONE field, so
  dropping that field would leave nothing to compare and every material would
  score as unique - silent, perfect and meaningless. Validity findings therefore
  hold the offending RECORDS out of matching instead. This also removes a severe
  false-positive mode: twenty materials described "TEST" normalise to the same
  text, score a perfect match against each other, and would otherwise form ONE
  cluster of genuinely different materials that the survivorship rules would
  merge automatically.
- **Every schema dial gets listed options and a stated reason for its default.** The Jaro-Winkler default was chosen without either, and the cost showed several packages later when the review found it flattered prefix matches and missed suffix matches. Package 4f-prep moved MARA to `token_sort_ratio`; the alternatives sit in a comment above the setting in `config/schema/mara.yaml`.
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
- **Generated artefacts.** Synthetic datasets under `data/synthetic/` (with
  their vectors, verdicts.json files and bench runs), the real extracts under
  `data/raw/`, the remediation plans under `artefacts/remediation/` and
  `mlruns/` are git-ignored. Tracked on purpose: the profiles
  under `data/profiles/`, the generated configs (`config/rules/*`,
  `config/rule_bank/templates.yaml`), and the runtime stores above
  (`data/repository/`, `data/approved/`, `artefacts/suggestions_mara.json`).
