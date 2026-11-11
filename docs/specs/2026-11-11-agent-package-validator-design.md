# Agent package validator: agent-scope rules and a judged end-to-end run

Status: design, 2026-11-11. Scope ruled by the owner on 2026-11-11: build the check only; no repair
step and no generator in this change. Location ruled the same day: `evolver/validator/`.

## 1. Intent

Raven's shipped agents (`agents/raven-code`, `raven-research`, `raven-design`, `raven-oncall`,
`raven-ppt`) customise one shared runtime from outside it: an agent folder with `subagent.json`,
a `config.json` diff, optional `soul.md` / `skills/` / `modes/`, and plugin contributions under
`plugins/<id>/` or an engine wheel under `plugins-dist/`. The kernel never imports them.

Harness development is going to be automated. Whatever produces an agent folder -- a person, a
future generator, a retargeted Evolver or Curator -- needs one command that decides whether the
folder can ship with Raven:

1. it stays inside the scope Raven grants an agent (the **agent scope**, section 4), and
2. it runs end to end through the host's own dispatch path and returns results that a judge model
   accepts on an acceptance suite the folder did not write.

The check gives a verdict and evidence. It never edits the folder.

## 2. Terms

- **Discovered agent** -- as defined in `CONTEXT.md`: a folder under an `agents/` tree holding a
  launcher (`run.py`) and a `subagent.json` manifest. In this document "agent folder" means the
  folder being checked.
- **Agent scope** -- the set of places an agent folder may change, defined in section 4 by what the
  five shipped agents use.
- **Acceptance suite** (new term; define it in `CONTEXT.md` in the same change) -- a directory of
  cases kept outside the agent folder. Each case is a task, its input files, and the criteria a
  judge model checks the delivered result against.
- **Case verdict** -- pass/fail for one case, derived from per-criterion judge verdicts.

## 3. Goals, non-goals, anti-goals

Goals:

- `uv run python -m evolver validate <folder> --suite <suite-dir>` prints a layered report, writes it
  as JSON, and exits 0 only when every layer passes.
- Layers that make no model call (folder, declarations, code, activation) run first; the first
  failing layer stops the run.
- The end-to-end layer installs the folder into an isolated `RAVEN_HOME`, requires discovery to list
  it enabled, and dispatches every case through `AcpAgentBackend`, the backend the host uses for
  `kind: acp` rows.
- The judge layer has a model decide each criterion for each case.
- The five shipped agents pass layers F, D, C and A; this is the calibration of the rules.

Non-goals (this change):

- Repairing a failing folder, or any generate/repair loop.
- Retargeting Evolver or Curator to emit agent folders.
- Checking whether the host model would *choose* the agent from its `description` (routing).
- Scoring quality beyond pass/fail, benchmarks, or comparisons between versions.
- Installing anything. An engine wheel must already be importable by the interpreter running the
  check; otherwise discovery lists the row disabled and A1 fails with that reason.
- Remote-machine rows (`machine` on the row).

Anti-goals -- outcomes that count as failure even if the command works:

- A folder can pass by grading itself. Cases or criteria shipped inside the folder never count
  toward the verdict.
- A rule that rejects any of the five shipped agents. The shipped agents define the scope; a rule
  they fail is a wrong rule, or it is advisory.
- A layer that could not run reported as a pass or a skip. Anything that did not run is a failure
  with its reason (except `--static-only`, which can never report PASS).
- Touching the operator's real `RAVEN_HOME`, config file, roster or memory store.
- Spending a model call before layers F, D, C and A have passed.
- Printing or persisting a credential anywhere except the isolated home (files mode 600), which is
  deleted at the end of the run together with the package snapshot.

## 4. Agent scope (normative)

The rules in section 6 enforce this table. It is read from the shipped agents on main `2eb428ac8`
and from `agents/BUILDING.md`.

| Place | What an agent may set | Evidence |
| --- | --- | --- |
| `subagent.json` | `name`, `description`, `owns`, launcher `command` with `{PYTHON}` / `{SUBAGENT_DIR}`, `everos` identity, optional `engine` | all five |
| `config.json` | any block of the host schema: model, provider, effort, limits, `tools` (incl. `disabledTools`, `sandbox`, `restrictToWorkspace`, `exec`, `mcpServers`), `context`, `compaction`, memory identity (`memory.userId` / `agentId`, `memoryTopK`), `plugins.config["<id>"]`, `language` | all five |
| identity and prompts | `soul.md`, other top-level `*.md` read by the launcher, plugin `prompts/` | code, research, design |
| `modes/*.json` | ACP mode profiles (e.g. effort per mode) | all five |
| `skills/` | the agent's own skills | design |
| plugin contributions | `tools`, `hooks`, `memory_backends`, `services`, `tool_gates`, `session_observers`, `onboard` (`raven/plugins/manifest.py:Contributes`) | code, research, oncall, design and ppt engines |
| hook phases | `before_user_inbound`, `before_iteration`, `before_execute_tools`, `after_iteration`, `terminal_answerless`, `after_send` | all five |
| participant verbs | `intake`, `select_tools`, `advise`, `system_addendum`, `review`, `salvage`, `judge`, `outbound`, `archive` (`raven/contracts/participant.py`) | all five |
| tool names | new names, and same-name replacement of a built-in (code replaces 5, research 3) | code, research |
| engine wheel | package with `raven.plugins` entry point and manifest inside the package | design, ppt |

Outside the scope:

- editing anything under `raven/`, or any file outside the agent folder (and its engine directory);
- replacing the four harness modules (`loop.harness` is always `default_harness_modules`,
  `raven/agent/loop/main.py:500`);
- choosing the long-term memory store or its address, and `plugins.dirs`: the launcher renders these
  from the host (`raven/config/product_render.py:inherit_memory_backend`, the launcher's
  `plugins["dirs"]`). The host also supplies, at render time, the LLM block when the agent holds
  no key (`inherit_llm`), the host's deny rules (`inherit_host_denials`), plugin opt-outs
  (`inherit_plugin_opt_outs`) and the image section (`inherit_media_image`).

Shipped plugins import many `raven.*` modules (`raven.agent.tools`, `raven.agent.hook`,
`raven.permissions`, `raven.ops`, `raven.utils`, `raven.security`, ...). Importing `raven` is
therefore inside the scope; section 6 lists the imports that are not.

## 5. Command and inputs

```
python -m evolver validate <folder>
    --suite <dir>              acceptance suite (required unless --static-only)
    [--engine <dir>]           engine project for a wheel-shaped agent
    [--host-config <path>]     where provider credentials are read from (default: the active
                               RAVEN_HOME's config.json); `providers`, `tools.web` and `tools.media` are copied
    [--model <name>]           model the agent runs on (default deepseek/deepseek-flash)
    [--judge-model <name>]     default deepseek/deepseek-flash
    [--judge-repeats <n>]      judge calls per case, default 1; majority per criterion
    [--min-pass-rate <x>]      fraction of cases that must pass, default 1.0
    [--report <path>]          JSON report path (default: printed location under the run dir)
    [--keep]                   keep the isolated home and the package snapshot
    [--static-only]            run layers F, D, C and A (no model call); prints STATIC-OK, never PASS
```

The agent runs on `--model` because an agent without its own key inherits the host's LLM block,
and the isolated host config sets `agents.defaults.model` to `--model` and names its provider. The report records the model
the turn actually used where the ACP record exposes it, and the configured one otherwise.

Exit codes: `0` pass (or static-ok with `--static-only`); `1` the folder failed a rule or a case;
`2` usage or input error (missing folder, malformed suite, suite `target` naming another agent);
`3` environment failure unrelated to the folder (provider unreachable, missing credentials). An
engine wheel that is not importable is `1`: the folder declares a dependency the environment lacks.

### Acceptance suite format

```
<suite>/
  suite.json        {"name", "version", "target": "<agent name> | null", "timeout_s": 900}
  cases/<id>.json   {"id", "task", "files": {"<relpath>": "<text>" | {"base64": "..."}},
                     "criteria": [{"id", "statement"}],
                     "reference": "<optional notes the judge may use>",
                     "timeout_s": <optional override>}
```

Authoring rules, written into the suite documentation: every criterion must be checkable from the
task text and the delivered result; a criterion may not demand something the task did not ask for;
the reference gives exact values the judge needs. `experts/cases/*/questions.json` in the experimental
fork follows the same shape (task, files, reference) and is a usable source of cases.

The report records a content digest of the suite and of the agent folder, so a verdict is tied to
exact inputs.

## 6. Layers and rules

Every rule has a stable id (prefix per layer) so callers, including a future generator, can key on
it. Each failure carries the rule id, the file and line or key, and a one-line fix hint. Rules marked
*advisory* print a warning and never fail. Within a layer every rule runs, so one report lists all of
that layer's failures; the run stops after the first layer that fails.

The check copies the folder (and engine directory) into a run directory first and checks the copy,
so what is checked is what runs. The copy leaves out `__pycache__` directories and `*.pyc` files:
build output is never part of a package, and a working checkout of the shipped agents carries it.

### Layer F -- folder

- F1 required files: `subagent.json`, `run.py`, `config.json`.
- F2 allowed top-level entries: `subagent.json`, `run.py`, `config.json`, `install.py`,
  `.env.example`, `README.md`, `soul.md`, other `*.md`, `modes/`, `skills/`, `plugins/`.
- F3 no `.env`; no non-empty value under a credential-named key (`*_API_KEY=...`, `"apiKey": ...`);
  no live-looking vendor key shape (`sk-...`, `AKIA...`, `ghp_...`, `xox?-...`) in any text file.
- F4 no symlink resolving outside the folder; no file over 1 MiB (AGENTS.md section 7 parity).
- F5 *advisory*: build output (`__pycache__`, `*.pyc`) present in the source folder; it is left out
  of the copy.

### Layer D -- declarations

- D1 `subagent.json` validates as `ThirdPartyAcpSubagentConfig`; `kind` is `acp`.
- D2 `description` and `owns` are non-empty and carry no scaffold TODO marker.
- D3 `command` / `cwd` use `{PYTHON}` / `{SUBAGENT_DIR}`, with no absolute path.
- D4 every `plugins/*/raven-plugin.toml` (or the engine's packaged manifest) parses via
  `PluginManifest.from_toml_path`; every factory reference is well formed.
- D5 `config.json` validates against the host config schema, through the same loader the ACP
  server uses on the rendered file.
- D6 `config.json` holds no credential values, no `plugins.dirs`, and none of the memory-slice keys
  the host's value replaces at render time (`_MEMORY_SLICE_INHERITED` in
  `raven/config/product_render.py`, read by `inherit_memory_backend`). Naming `memory.backend` is
  allowed (all five do; the host's value wins).
- D7 *advisory*: tool names not derived from the agent name; memory identity shared with another
  discovered agent (raven-ppt reuses raven-design's).

### Layer C -- code

- C1 every `.py` compiles.
- C2 no import of `evolver`, `experimental`, `benchmarks`, `tests`, or another agent folder's plugin
  package (AST scan of every `.py`, including `run.py`).
- C3 importing each factory module in a child process (empty temp `HOME` and `RAVEN_HOME`, cwd a
  temp dir, bounded time) succeeds and leaves the folder copy byte-identical.

### Isolated home (set up after layer C, before layer A)

- Build an isolated home in the run directory: `config.json` (file mode 600) with
  `agents.defaults.model = --model` and `agents.defaults.provider` named for it (the host's provider
  when the host's default model is `--model`, else the provider whose `models` list it, else the model
  id's provider prefix); the `providers`, `tools.web` and `tools.media` blocks copied from
  `--host-config`, which are what a launcher inherits at render time; and `memory.backend: null` (the
  host's off switch travels to the agent at render time, so the run writes to no long-term store).
  Nothing else of the host config is copied. An agent's own provider survives the render whenever the
  host config names none, so the provider is always named.
- `agents/<name>/` holds the folder copy, beside the shipped agents its `routes[].to` names (copied from
  the repository's `agents/`): discovery disables a routing folder whose target is absent.
- No permission settings are needed for an unattended dispatch: the host answers an agent's
  `session/request_permission` itself and approves unless a deny rule matches
  (`raven/acp_client/permissions.py`).
- The agent state root and ACP home both derive from `RAVEN_HOME` (`product_state_root`,
  `product_acp_home`), so they land inside the isolated home.
- Every child process (launcher, activation probe) starts with `RAVEN_HOME` pointing at the isolated
  home and with the operator's other `RAVEN_*` variables and every variable carrying the agent's env
  prefix (for example `CODE_API_KEY`, the state-root and ACP-home overrides) removed. An agent key in
  the environment would switch the agent to own-key mode and bypass `--model`.
- At the end, close every connection pool and child process; delete the isolated home and the package
  snapshot unless `--keep`. The snapshot is a copy of the folder as handed in, credentials included
  when the folder carries any.

### Layer A -- activation

- A1 `discover_product_rows(<home>/agents)` returns exactly one row with the agent's name, and it is
  enabled; otherwise fail with the row's disabled reason (for an engine agent this is where a missing
  wheel shows).
- A2 the ACP `initialize` handshake succeeds through that row's command.
  Starting the launcher this way also makes it write its rendered config under the agent state root
  inside the isolated home; A3 to A5 read that file. A launcher that exits before answering with
  `_API_KEY is not set` or `no provider key to inherit from` on stderr is an environment failure
  (exit 3), the heuristic `raven agents new` applies to its own smoke check.
- A3 the rendered config's model is `--model`, its provider is the one the isolated home named, and
  its memory backend is off; the report records all three.
- A4 in a child process loading the rendered config, every declared contribution is either
  registered or declined by its factory (returns `None`); no activation failure, no name conflict
  with another plugin active in the same environment.
- A5 for an engine-wheel plugin: with its `plugins.config["<id>"]` slice removed from a copy of the
  rendered config, no contribution registers (the gate in `agents/BUILDING.md` section 7). An engine
  loads into every raven process through its entry point; a directory plugin loads only through its
  own launcher's `plugins.dirs`, so A5 does not apply to it.
- A rule that needs an earlier one is not reported when that one fails: A2 to A5 need the discovered
  row, and A3 to A5 need the running launcher.

### Layer E -- end to end

- For each case: create a fresh working directory, write the case files, construct `AcpAgentBackend` from the
  discovered row with its own connection pool, and `await backend.run(task, task_id=...,
  workspace=..., executor=None, session_key=...)` under the case timeout. Cases run one at a time.
- Record the reply `run()` returns (the last message, which is what the host sees), the full streamed
  text, and every file created or changed in the case's working directory (text up to a size cap;
  binaries by name, size and hash). Raven's own `.raven/` Checkpoint repository at its root is bookkeeping, not
  output, and is left out.
- E1 the turn completes without transport error, timeout or empty turn (`AcpEmptyTurnError`); a case
  that fails E1 goes to the report as a runtime failure, is not judged, and counts as a failed case in
  J1's pass rate. E1 failures end the run only when no case completed.

### Layer J -- judge

- The judge sees: task text, the list of input files (contents for small text files), criteria,
  reference, the final reply, and the delivered files (truncated with an explicit marker). It does
  not see the agent's streamed transcript.
- It returns JSON: `{"criteria": [{"id", "verdict": "pass" | "fail" | "not_applicable", "reason"}]}`
  covering every criterion id exactly once. Malformed output gets one retry; still malformed makes
  the case a judge error, which fails the case.
- Temperature 0. With `--judge-repeats n`, the per-criterion verdict is the majority; a tie fails.
- J1 a case passes when no criterion fails and at least one passes (all `not_applicable` fails). The
  run passes J when the passing fraction of all suite cases is at least `--min-pass-rate`.
- The judge call goes through Raven's provider stack (`raven.providers.factory`) with the isolated
  home's config, so credentials and routing match the agent's.
- The prompt is an English template file next to the code.

### Verdict and report

Overall PASS requires F, D, C and A to pass and J1 to pass. The JSON report holds: folder and suite
digests, Raven version and commit, configured agent model and judge model, the rendered model,
provider and memory backend, per-layer rule results with evidence, and per case: case id, timings,
final reply, delivered files, every raw judge output, the parsed verdicts, plus an `error` when an
environment failure or an exception ended the run. Stdout prints one line per layer and one per case.

## 7. Constraints

- Code lives in `evolver/validator/`, in the main repository, outside the wheel like the rest of
  `evolver/`. The command is a `validate` subcommand of `python -m evolver` (`evolver/cli.py`); the
  existing `check` subcommand validates an evolution run spec and stays as it is.
- The validator does not import the rest of `evolver` (orchestrator, judge, scheduler, ...), so
  `python -m evolver validate` loads only what it uses and can later be lifted out on its own.
- This change edits nothing under `raven/`. The direction is already guarded: the `pyproject.toml`
  contract "the runtime does not import the evolver" forbids `raven` importing `evolver`; the validator
  importing `raven` is the allowed direction.
- The validator uses public Raven seams only: `raven.config.schema.ThirdPartyAcpSubagentConfig`,
  `raven.plugins.manifest.PluginManifest`, `raven.agent.subagent.vendored_agents.discover_product_rows`,
  `raven.acp_client.acp_agent.AcpAgentBackend` and its connection pool, `raven.providers.factory`, and
  the config loader. The private helpers in `raven/cli/agents_commands.py` (`_doctor`,
  `_smoke_handshake`) are the reference for what scaffold self-checks cover today; read them, do not
  import them.
- `raven agents new` keeps its own self-check. Agreement between the two is held by a test (section 8):
  a fresh scaffold must pass the validator's static layers.
- The runtime does not import agent folders, and neither does the validator's own process: every piece
  of agent-folder code -- launcher, factories, plugins -- runs only in child processes.
- Subprocess use follows the existing `evolver/` ruff per-file ignores in `pyproject.toml` (S607 and
  similar), added for the new files.
- Tests: `tests/test_evolver_validator_<aspect>.py` (for example `_rules`, `_e2e`, `_judge`); the
  real-model run in `tests/integration/test_evolver_validator_real_llm.py`. Run with `uv run pytest`,
  without `-n 8`.
- Real model calls in tests use `deepseek/deepseek-flash`.
- Source, prompts and docs in English (AGENTS.md section 1.3). Document the subcommand in
  `evolver/README.md` (new section and a documentation-map row), list `validator` in the package layout
  of `evolver/__init__.py`, describe it in `docs-site/docs/evolver.md` with its `.zh.md` counterpart, and
  add a one-line pointer in `agents/BUILDING.md`.
- Add **Acceptance suite** to `CONTEXT.md`.
- Commits use the `evolver` scope, as earlier evolver changes did.
- The agent runs with the permissions its own `config.json` grants (several shipped agents disable the
  sandbox). Run the check on a development box or container, never against production credentials.

## 8. Verification of the check itself

- Calibration: a test runs layers F, D, C and A on each of the five shipped agents (the two engine
  agents with their engine directories) and requires all to pass. The engine agents need their wheels
  importable in the test environment; if not, that test reports the gap explicitly instead of
  skipping silently.
- Scaffold agreement: `raven agents new <name> --here --no-smoke` into a temp directory, fill
  `description` and `owns`, and require layers F, D and C to pass (A as well when the test environment
  has credentials).
- Mutants: for every failing rule from F1 to A5, a fixture derived from that scaffold with exactly one
  violation; the test asserts that rule id, and only it, fails.
- E and J without a model: a stub ACP agent folder whose launcher answers `initialize` and one
  `session/prompt` with scripted text and a scripted file write, plus a stub judge provider. Cover:
  pass, wrong answer (J1 fails), empty turn (E1 fails), timeout, malformed judge output.
- Real model (opt-in integration): a two-case suite against `raven-code` (write a small file with a
  checkable property; one case designed to fail a criterion) on deepseek-flash, asserting one pass and
  one fail.

## 9. Left to the builder

- Module layout under `evolver/validator/`, report field names, and the judge prompt wording.
- How layer A observes registration (registry APIs in a child process; see
  `tests/test_plugin_registry.py` and `raven plugins` for existing seams).
- Default timeouts, text size caps, and how the run directory is named.

## 10. Later

- Routing check: does the host model pick this agent for the suite's tasks.
- Advisory metrics: private-member access into `raven`, prompt size, tool count.
- Parallel cases, cost caps, and a repair loop that consumes the report's rule ids.
