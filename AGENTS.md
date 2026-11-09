# AgentDQ agent instructions

Version 0.1 | 09-Nov-2026 | Luqman

This file adds project facts to my shared working agreement. Where the two conflict, this file wins. My latest explicit instruction wins over both.

## Know the project

AgentDQ assesses the data quality of SAP material master data (MARA, MARC, MAKT). Agents profile the data and suggest rules, a human approves them, a deterministic executor runs them, and agents adjudicate uncertain duplicates and write a remediation plan. It is a proof of concept and a public portfolio piece, not a product. Current phase: Package 4 close-out (4z), then the Package 5 work breakdown.

## Load the shared agreement

The shared agreement covers decision classes, the prepare/implement workflow, scope, dependencies, Python conventions, Git and test safeguards, completion reports and writing style.

If the shared agreement is not already in your context, read `~/agent-standards/core.md` before you start work.

## Map the documents

| Role | Path |
|---|---|
| Design | `docs/agentdq_design.md` |
| Plan, with the WBS of each package | `docs/agentdq-project-plan.md` |
| Module map | `docs/modules_map.md` |
| Rule bank source | `docs/Infomation_Steward_Rules_Tables.xlsx` |
| Runbook | `README.md` for setup and commands; the plan's package WBS for each unit |

There is no separate runbook file and no ADR folder. The README holds the setup and run procedures. The WBS of each package in the plan takes the place of the unit's runbook section: `Prepare` writes it, and `Implement` treats it as the contract.

The design, the plan and the module map are the three living documents.

## Run the standard commands

Run these commands from the repository root. Run Python modules with `uv run python -m`, never as a script path, because the tools import from `src`.

```text
Set up:        uv sync
Test:          uv run pytest -q
Assess:        uv run python -m tools.run_assessment_graph --data data/synthetic/degraded --format parquet
                 (default rules: data/approved; add --rules config/rules for all imported rules)
Dashboard:     uv run streamlit run app/dashboard.py
Approval gate: uv run streamlit run app/gate.py
Rule bank:     uv run streamlit run app/bank_browser.py
```

Regenerate the data in this order:

```text
1. uv run python -m src.data.profiler --input data/raw --tables MARA,MARC,MAKT \
       --pattern "{table}_EX_DATA.xlsx" --out data/profiles
2. uv run python -m src.rules.is_importer --workbook docs/Infomation_Steward_Rules_Tables.xlsx \
       --schema config/schema --tables MARA,MARC,MAKT --out config/rules
3. uv run python -m src.data.generator --schema config/schema --profiles data/profiles \
       --tables MARA,MARC,MAKT --materials 5000 --seed 42 --out data/synthetic/baseline
4. uv run python -m src.data.defect_injector --baseline data/synthetic/baseline \
       --schema config/schema --rules config/rules --scenario degraded \
       --seed 42 --out data/synthetic/degraded
```

`config/rules/cross_field_examples.yaml` is written by hand. The importer does not make it. Step 4 needs it in the rules folder, or the degraded set has no Consistency defects.

These commands call a paid model or download a model. I run them, or I approve each run first:

```text
uv run python -m tools.run_suggestion ...
uv run python -m tools.run_adjudication ...
uv run python -m tools.run_remediation ...
uv run python -m tools.build_embeddings ...
uv run python -m tools.benchmark_embedding_models ...
```

## Respect the locked architecture

The items below are locked for this project. Each one needs my explicit decision to change.

- The agentic value is rule suggestion from profiled data. Rule execution stays deterministic, in the pandas executor.
- A model call earns its place only on judgement: which rules apply, why a value failed, whether an uncertain pair is a duplicate, and the remediation narrative. Never call deterministic work agentic, and never add a model call for show.
- Agents reason only over provided knowledge: the rule bank, reference tables, the schema and profile evidence. Never over model memory. Every suggestion carries a rationale, evidence and a confidence.
- The Information Steward workbook is a rule bank of templates, not the active ruleset.
- A human approves every rule before it runs. Rule state changes only through the `RulesRepository` verbs. The Streamlit apps render and invoke; they never decide. Manual rules enter through the same gate. Agents never write rule strength.
- `export_approved()` writes approved rules in the importer's YAML shape, so the loader and the executor do not change.
- Orchestration is two LangGraph graphs, suggestion and assessment, joined by the repository. The pause for approval is the repository, not `interrupt()`. Agents do not import LangGraph.
- Model calls go through DSPy signatures, never prompt strings.
- `config/schema/` is the single onboarding contract for a table.
- Ground truth equals executor reality. The defect injector checks each injected violation against the executor before it labels it.

## Treat these as changeable baselines

The choices below are current, and my latest direction may revise them without a design exercise.

- Uniqueness dials in `config/schema/mara.yaml`: blocking key, fuzzy method (`token_sort_ratio`), score bands, block pair ceiling and attribute veto cap (0.75).
- Default model `openai/gpt-4o-mini`, set by `AGENTDQ_MODEL` or `--model`.
- The embedding model and the adjudication cap.
- Retrieval floor, highlight floor and confidence weights. These are placeholders until Package 5 calibrates them.
- Dashboard layout and tabs.
- The order of Packages 5 (evaluation), 6 (local LLM) and 7 (public demo).

## Know the environments

The agent works only on the Mac mini, in `~/projects/agentdq`. It uses `~/agentdq_scratch` and `~/agentdq_backup` only when a task names them. It never searches the home folder.

| Machine | Role | Test tier |
|---|---|---|
| Mac mini, user `websvc` | Agent, Git, coding, offline tests, local Streamlit | A |
| Mac mini with the OpenAI API | Live model runs, on my approval | B |
| Public demo through Cloudflare (Package 7, not built) | Replay only, synthetic data, no live model calls | C |

The Windows desktop holds an old clone. It is not a build machine.

## Keep code in its ownership area

Each directory below owns one concern. Do not place logic outside its owner.

```text
src/data/          Extract loading, profiling, schema, synthetic generator, defect injector
src/rules/         Rule IR loading, IS importer, rule bank, rule repository and ledger
src/agents/        Dimension agents, uniqueness matcher, adjudicator, remediation, embeddings
src/dspy_modules/  DSPy signatures and model configuration
src/reporting/     Scorecard, assessment, evaluation, display shaping
src/*.py           Shared contracts, graph state, graph nodes, orchestrator
tools/             Command-line runners
app/               Streamlit apps: render and invoke only
config/            Table schemas, imported rules, rule bank
tests/             Offline pytest suite with fake models
docs/              The three living documents and the IS workbook
```

Never put rule logic, scoring or model calls in `app/`. Display shaping lives in `src/reporting/`.

Data folders:

- Not tracked: `data/raw/` (real SAP CAL extracts), `data/synthetic/` (generated data, vectors and verdicts), `.env`.
- Tracked on purpose: `data/profiles/`, `config/rules/`, `data/repository/`, `data/approved/`, `artefacts/suggestions_mara.json`.
- The repository is public. Never commit real extracts, secrets, vectors or personal paths.

## Name units of work

Units use the format `Package N` with steps `Na`, `Nb` and so on. Step `Nz` is the close-out of a package. `Prepare Package N` writes the package WBS into the plan and reports the decisions that need my input. No code is written until I agree the WBS. `Implement Package N`, or one step of it, follows the shared agreement.

## Apply project-specific rules

- At the end of every package, update the three living documents in the same session. This is my standing request for documentation maintenance. Check each document for balanced code fences, Latin-1-safe tables and no "whilst".
- Keep the change history from the shared agreement, and also tag each changed line with its version, for example `# v1.2`. Keep the entry order that each file already uses.
- Follow the existing code style, which overrides the shared agreement here: declare local variables at the start of each function, use no lambdas, and use no emojis in print statements.
- Module filenames are unique across the repository. Refer to each module by its repository path, for example `src/data/extract_loader.py`.
- When a change needs regenerated data before the tests pass, list the regeneration commands in dependency order.
- Schema dials and thresholds are not implementation choices. List the alternatives and the reason for the default before you write a value.
- Label example outputs as examples when they are not my own regenerated data.
- Commands quoted in docs or articles must be exact and verified by a run.
