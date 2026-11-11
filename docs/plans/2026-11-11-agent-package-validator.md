# Agent Package Validator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `uv run python -m evolver validate <folder> --suite <dir>` decides whether an agent folder stays inside the product scope, runs end to end through the host's ACP dispatch path, and returns results a judge model accepts.

**Architecture:** A self-contained package `evolver/validator/` runs six layers in order (F folder, D declarations, C code, A activation, E end to end, J judge) and stops after the first failing layer. All agent-folder code runs in child processes started with `sys.executable`, under an isolated `RAVEN_HOME` and a scrubbed environment; the parent only reads files, starts children, and calls the judge model through Raven's provider stack.

**Tech Stack:** Python 3.12, stdlib (`ast`, `subprocess`, `asyncio`, `hashlib`, `shutil`, `argparse`), Raven public seams (`raven.config.schema`, `raven.plugins.manifest`, `raven.agent.subagent.vendored_agents`, `raven.agent.subagent.backends`, `raven.acp_client.pool`, `raven.core.plugin_stack`, `raven.config.loader`, `raven.config.raven`, `raven.providers.factory`), pytest.

**Spec:** `docs/specs/2026-11-11-agent-package-validator-design.md`

## Global Constraints

- All new code lives in `evolver/validator/`; nothing under `raven/` changes. The validator imports no other `evolver` module.
- Every new file starts with an English module docstring (AGENTS.md 1.1); all source, prompts and docs in English.
- Agent-folder code (launcher, factories, plugins) runs only in child processes started with `sys.executable`.
- Defaults: `--model deepseek/deepseek-flash`, `--judge-model deepseek/deepseek-flash`, `--judge-repeats 1`, `--min-pass-rate 1.0`, suite `timeout_s` 900. Real-model tests use `deepseek/deepseek-flash` only.
- Rule ids exactly: `F1`-`F5`, `D1`-`D7`, `C1`-`C3`, `A1`-`A5`, `E1`, `J1`. `F5` and `D7` are advisory and never fail.
- Exit codes: `0` PASS or STATIC-OK; `1` the folder failed a rule or a case (including a non-importable engine wheel); `2` usage or input error; `3` environment failure (provider unreachable, missing credentials).
- Credentials exist only under `<run_dir>/home/` (files mode 600), including any copy of a rendered product config; `home/` is removed in a `finally` unless `--keep`. `report.json` and the package snapshot stay.
- Unattended dispatch needs no extra permission settings: the host side approves `session/request_permission` except on deny rules (`raven/acp_client/permissions.py`). The isolated config sets none.
- Tests: `uv run --frozen --all-extras pytest <files> -q`, never `-n 8`. Unit files `tests/test_evolver_validator_<aspect>.py`; integration files `tests/integration/test_evolver_validator_<kind>.py`.
- Commits happen only on the owner's explicit word (AGENTS.md 3.4). Each task ends by stopping and reporting; when told to commit: header `<type>(evolver): <subject>`, ASCII, no trailer.

## Review Focus

- A suite case file path that is absolute or contains `..` -> exit 2 before anything is written (Task 1, `test_case_file_path_escape_is_input_error`).
- A manifest name that differs from the folder name (`Raven-Code` in `agents/raven-code/`): discovery matches the manifest name, the env prefix comes from the folder name (Task 6, `test_scrub_uses_folder_prefix_not_manifest_name`; Task 7, `test_discovery_matches_manifest_name`).
- Any exception after the isolated home exists still removes `home/` and every child process (Task 10, `test_home_removed_when_a_layer_raises`).
- A judge reply that omits, repeats or invents a criterion id -> one retry, then a judge error that fails the case (Task 9, `test_missing_criterion_id_retries_then_errors`, `test_duplicate_or_unknown_id_is_rejected`).
- A judge that marks every criterion `not_applicable` -> the case fails with "no applicable criterion" (Task 9, `test_all_not_applicable_fails_case`).

---

### Task 1: Report model and acceptance suite loader

**Files:**
- Create: `evolver/validator/__init__.py`, `evolver/validator/report.py`, `evolver/validator/suite.py`
- Test: `tests/test_evolver_validator_suite.py`

**Interfaces:**
- Produces (`report.py`):
  - `Status = Literal["pass", "fail", "advisory"]`
  - `@dataclass(frozen=True) class RuleResult: rule: str; status: Status; message: str; where: str = ""; fix: str = ""`
  - `@dataclass class LayerResult: layer: str; results: list[RuleResult]`, property `passed -> bool` (no `fail`)
  - `@dataclass(frozen=True) class FileRecord: path: str; size: int; sha256: str; text: str | None; truncated: bool`
  - `@dataclass class CaseRun: case_id: str; reply: str; full_text: str; files: list[FileRecord]; seconds: float; error: str | None`
  - `@dataclass(frozen=True) class CriterionVerdict: id: str; verdict: Literal["pass", "fail", "not_applicable"]; reason: str`
  - `@dataclass class CaseVerdict: case_id: str; passed: bool; criteria: list[CriterionVerdict]; raw: list[str]; error: str | None`
  - `@dataclass class Report:` folder_digest, suite_digest, raven_version, raven_commit, agent_model, judge_model, rendered (dict), layers, cases, verdicts, `outcome: Literal["PASS", "FAIL", "STATIC-OK"]`; `to_json() -> dict`
  - `class ValidatorInputError(Exception)`, `class ValidatorEnvironmentError(Exception)`, `def exit_code(report: Report) -> int`
- Produces (`suite.py`):
  - `@dataclass(frozen=True) class Criterion: id: str; statement: str`
  - `@dataclass(frozen=True) class Case: id: str; task: str; files: dict[str, bytes]; criteria: tuple[Criterion, ...]; reference: str; timeout_s: int`
  - `@dataclass(frozen=True) class Suite: name: str; version: str; target: str | None; cases: tuple[Case, ...]; digest: str`
  - `def load_suite(path: Path, *, agent_name: str) -> Suite` (raises `ValidatorInputError`)

- [ ] **Step 1: Write the failing tests**

```python
def test_loads_cases_with_text_and_base64_files(tmp_path): ...
    # suite.json {"name":"s","version":"1","target":null,"timeout_s":900}
    # cases/a.json files {"in.txt":"hi","b.bin":{"base64":"AAE="}}
    s = load_suite(root, agent_name="Demo")
    assert [c.id for c in s.cases] == ["a"]
    assert s.cases[0].files == {"in.txt": b"hi", "b.bin": b"\x00\x01"}
    assert s.cases[0].timeout_s == 900 and len(s.digest) == 64

def test_case_timeout_overrides_suite_default(tmp_path): ...  # case "timeout_s": 30 -> 30
def test_target_naming_another_agent_is_input_error(tmp_path): ...  # target "Other" -> ValidatorInputError
def test_case_file_path_escape_is_input_error(tmp_path): ...  # "../x" and "/etc/x" -> ValidatorInputError
def test_duplicate_case_or_criterion_ids_are_input_errors(tmp_path): ...
def test_case_without_criteria_is_input_error(tmp_path): ...
def test_digest_changes_when_a_case_changes(tmp_path): ...
def test_exit_code_maps_outcomes(): ...  # PASS->0, STATIC-OK->0, FAIL->1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen --all-extras pytest tests/test_evolver_validator_suite.py -q`
Expected: FAIL (ImportError: `evolver.validator.suite`)

- [ ] **Step 3: Implement `report.py` and `load_suite`**

Cases are `cases/*.json` sorted by file name; digest is sha256 over `suite.json` and each case file's relative path and bytes in that order.

- [ ] **Step 4: Run to verify pass** -- same command, expected PASS.

- [ ] **Step 5: Stop and report; commit on the owner's word** (`feat(evolver): add validator report model and suite loader`).

---

### Task 2: Package snapshot and layer F (folder)

**Files:**
- Create: `evolver/validator/snapshot.py`, `evolver/validator/scope.py`
- Test: `tests/test_evolver_validator_rules.py`

**Interfaces:**
- Consumes: `RuleResult`, `LayerResult` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class Snapshot: source: Path; root: Path; engine: Path | None; digest: str; build_output: tuple[str, ...]`
  - `def take_snapshot(folder: Path, run_dir: Path, *, engine: Path | None) -> Snapshot` -- copies to `run_dir/package/<folder.name>` and `run_dir/engine/<engine.name>`, symlinks preserved, `__pycache__` / `*.pyc` left out and listed in `build_output`.
  - `def tree_digest(root: Path) -> str`
  - `def check_folder(snap: Snapshot) -> LayerResult` (F1-F5)

Values: F2 allowed top level = `subagent.json`, `run.py`, `config.json`, `install.py`, `.env.example`, `README.md`, `soul.md`, any `*.md`, `modes/`, `skills/`, `plugins/`. F3 credential pattern: a `.env` file anywhere; a `KEY=value` line with a non-empty value whose key ends in `API_KEY`, `TOKEN`, `SECRET` or `PASSWORD`; a JSON string value under a key matching `apiKey|api_key|token|secret|password` (case-insensitive) that is non-empty. F4 size limit `1 * 1024 * 1024` bytes.

- [ ] **Step 1: Write the failing tests**

```python
def scaffold(tmp_path, monkeypatch) -> Path:
    # RAVEN_HOME=tmp_path/home; CliRunner().invoke(app, ["agents","new","demo-agent","--no-smoke"])
    # then replace the TODO description/owns in subagent.json with real text; return the folder

def test_snapshot_excludes_build_output_and_records_it(tmp_path, monkeypatch): ...
    # add plugins/x/__pycache__/m.pyc -> not in snap.root, listed in snap.build_output

def test_folder_layer_passes_on_scaffold(tmp_path, monkeypatch):
    assert check_folder(snap).passed

@pytest.mark.parametrize("mutate, rule", [
    (rm("run.py"), "F1"), (add("data.bin", b"x"), "F2"), (add(".env", b"A=1"), "F3"),
    (add(".env.example", b"DEMO_AGENT_API_KEY=sk-live"), "F3"),
    (symlink_to("/etc/hostname"), "F4"), (add("big.md", b"x" * (1024 * 1024 + 1)), "F4"),
])
def test_folder_mutant_fails_exactly_its_rule(mutate, rule, tmp_path, monkeypatch):
    assert {r.rule for r in result.results if r.status == "fail"} == {rule}

def test_build_output_is_advisory_f5(tmp_path, monkeypatch): ...  # status "advisory", layer passes
```

- [ ] **Step 2: Run to verify failure** -- `uv run --frozen --all-extras pytest tests/test_evolver_validator_rules.py -q`, expected ImportError.
- [ ] **Step 3: Implement `take_snapshot`, `tree_digest`, `check_folder`** (digest = sha256 over sorted relative paths plus file bytes, symlink target text for links).
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Stop and report; commit on the owner's word.**

---

### Task 3: Layer D (declarations)

**Files:**
- Modify: `evolver/validator/scope.py`
- Test: `tests/test_evolver_validator_rules.py`

**Interfaces:**
- Produces: `def check_declarations(snap: Snapshot, *, shipped_root: Path | None) -> LayerResult` (D1-D7); `def package_manifests(snap: Snapshot) -> list[tuple[Path, PluginManifest]]` (directory plugins under `plugins/*/raven-plugin.toml`, engine manifest found as `<engine>/*/raven-plugin.toml`).

Values:
- D1 `ThirdPartyAcpSubagentConfig.model_validate`, `kind == "acp"`.
- D2 `description` and `owns` non-empty and not containing `TODO`.
- D3 `command` and `cwd` contain `{SUBAGENT_DIR}`, `command` starts with `{PYTHON}`, and no token is an absolute path.
- D4 `PluginManifest.from_toml_path` for each manifest.
- D5 write `config.json` to a temp file; both `raven.config.loader.load_config(path)` and `raven.config.raven.load_raven_config(path)` load without raising.
- D6 fail on: any non-empty string under a key matching the F3 key pattern; `plugins.dirs` present; `plugins.config[<distribution>][<key>]` for every `<key>` in the first tuple of `raven.config.product_render._MEMORY_SLICE_INHERITED[<distribution>]`.
- D7 advisory: a tool name not starting with `vendored_agents.env_prefix(folder.name).lower()`; `memory.userId` equal to that of another folder under `shipped_root` (`Path(raven.__file__).parents[1] / "agents"` when it exists).

- [ ] **Step 1: Write the failing tests**

```python
def test_declarations_pass_on_scaffold(...): assert check_declarations(snap, shipped_root=None).passed

@pytest.mark.parametrize("mutate, rule", [
    (set_json("subagent.json", "kind", "cli"), "D1"),
    (set_json("subagent.json", "description", "TODO: describe"), "D2"),
    (set_json("subagent.json", "command", "/usr/bin/python3 {SUBAGENT_DIR}/run.py"), "D3"),
    (replace_in_manifest('factory = "', 'factory = "not a ref'), "D4"),
    (merge_config({"agents": {"defaults": {"maxTokens": "lots"}}}), "D5"),
    (merge_config({"plugins": {"dirs": ["/x"]}}), "D6"),
    (merge_config({"providers": {"openrouter": {"apiKey": "sk-or-123"}}}), "D6"),
    (merge_config({"plugins": {"config": {"everos-memory": {"base_url": "http://127.0.0.1:1"}}}}), "D6"),
])
def test_declaration_mutant_fails_exactly_its_rule(...): ...

def test_unprefixed_tool_name_is_advisory_d7(...): ...
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `check_declarations` and `package_manifests`.**
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Stop and report; commit on the owner's word.**

---

### Task 4: Layer C (code)

**Files:**
- Modify: `evolver/validator/scope.py`
- Create: `evolver/validator/_import_probe.py` (child entry: imports the modules named on argv after prepending the given roots to `sys.path`; exit 0 on success, prints the traceback otherwise)
- Test: `tests/test_evolver_validator_rules.py`

**Interfaces:**
- Produces: `def check_code(snap: Snapshot, *, other_packages: frozenset[str]) -> LayerResult` (C1-C3); `def other_plugin_packages(shipped_root: Path | None, own: Snapshot) -> frozenset[str]`.

Values: C2 forbidden top-level import names = `{"evolver", "experimental", "benchmarks", "tests"} | other_packages`, where `other_packages` = top-level package directories under `<shipped_root>/*/plugins/*/` plus `raven_design` and `raven_ppt`, minus the snapshot's own packages. C3 runs `sys.executable -m evolver.validator._import_probe --root <plugins/<id> or engine dir> ... <module> ...` with `HOME` and `RAVEN_HOME` set to fresh temp dirs, `cwd` a temp dir, timeout 60 s; then `tree_digest(snap.root)` (and the engine copy) must be unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_code_layer_passes_on_scaffold(...): ...
@pytest.mark.parametrize("mutate, rule", [
    (append_py("plugins/*/*/plugin.py", "def broken(:\n"), "C1"),
    (append_py("plugins/*/*/plugin.py", "import evolver\n"), "C2"),
    (append_py("plugins/*/*/plugin.py", "from code_flow import flow\n"), "C2"),
    (append_py("plugins/*/*/plugin.py",
               "import pathlib; pathlib.Path(__file__).with_name('x.txt').write_text('x')\n"), "C3"),
    (append_py("plugins/*/*/plugin.py", "import no_such_module_xyz\n"), "C3"),
])
def test_code_mutant_fails_exactly_its_rule(...): ...
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `check_code`, `other_plugin_packages` and `_import_probe`.**
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Stop and report; commit on the owner's word.**

---

### Task 5: Calibration of F, D and C on the shipped agents

**Files:**
- Test: `tests/test_evolver_validator_rules.py`
- Modify: `evolver/validator/scope.py` only if a shipped agent exposes a wrong rule (then the rule is fixed or made advisory, per spec section 3)

**Interfaces:**
- Consumes: `take_snapshot`, `check_folder`, `check_declarations`, `check_code`, `other_plugin_packages`.

- [ ] **Step 1: Write the test**

```python
SHIPPED = [("raven-code", None), ("raven-research", None), ("raven-oncall", None),
           ("raven-design", "plugins-dist/design-engine"), ("raven-ppt", "plugins-dist/ppt-engine")]

@pytest.mark.parametrize("name, engine", SHIPPED)
def test_shipped_agent_passes_static_layers(name, engine, tmp_path):
    snap = take_snapshot(REPO / "agents" / name, tmp_path, engine=engine and REPO / engine)
    for layer in (check_folder(snap), check_declarations(snap, shipped_root=REPO / "agents"),
                  check_code(snap, other_packages=other_plugin_packages(REPO / "agents", snap))):
        assert layer.passed, [r for r in layer.results if r.status == "fail"]
```

- [ ] **Step 2: Run it.** `uv run --frozen --all-extras pytest tests/test_evolver_validator_rules.py -q -k shipped`. Any failure is analysed: a rule the shipped agents break is wrong for the product scope; change it (or make it advisory) and re-run the whole rules file, mutants included.
- [ ] **Step 3: Stop and report** the calibration result rule by rule; commit on the owner's word.

---

### Task 6: Isolated home and environment scrub

**Files:**
- Create: `evolver/validator/home.py`
- Test: `tests/test_evolver_validator_activation.py`

**Interfaces:**
- Consumes: `Snapshot`, `ValidatorEnvironmentError`.
- Produces:
  - `@dataclass(frozen=True) class IsolatedHome: path: Path; agent_dir: Path; env: dict[str, str]`
  - `def build_isolated_home(run_dir: Path, snap: Snapshot, *, host_config: Path, model: str) -> IsolatedHome` -- writes `run_dir/home/config.json` (mode 600) = `{"agents": {"defaults": {"model": model}}, "providers": <copied>, "memory": {"backend": None}}`; copies `snap.root` to `run_dir/home/agents/<snap.source.name>`; raises `ValidatorEnvironmentError` when `host_config` is missing or has no `providers`.
  - `def scrubbed_env(base: Mapping[str, str], *, folder_name: str, home: Path) -> dict[str, str]` -- drops every key starting with `RAVEN_` or with `vendored_agents.env_prefix(folder_name) + "_"`, then sets `RAVEN_HOME=str(home)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_home_config_carries_model_providers_and_memory_off(tmp_path): ...
    cfg = json.loads((home.path / "config.json").read_text())
    assert cfg["agents"]["defaults"]["model"] == "deepseek/deepseek-flash"
    assert cfg["memory"]["backend"] is None and "openrouter" in cfg["providers"]
    assert stat.S_IMODE((home.path / "config.json").stat().st_mode) == 0o600

def test_only_providers_are_copied_from_host_config(tmp_path): ...  # host "permissions"/"tools" absent
def test_missing_providers_is_environment_failure(tmp_path): ...
def test_scrub_removes_agent_prefix_and_raven_vars(): ...
    env = scrubbed_env({"CODE_API_KEY": "k", "RAVEN_HOME": "/real", "RAVEN_X": "1", "PATH": "/bin"},
                       folder_name="raven-code", home=Path("/iso"))
    assert env == {"PATH": "/bin", "RAVEN_HOME": "/iso"}
def test_scrub_uses_folder_prefix_not_manifest_name(): ...  # folder "raven-code", manifest "Raven-Code"
```

- [ ] **Step 2: Run to verify failure** -- `uv run --frozen --all-extras pytest tests/test_evolver_validator_activation.py -q`.
- [ ] **Step 3: Implement `home.py`.**
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Stop and report; commit on the owner's word.**

---

### Task 7: Layer A (activation)

**Files:**
- Create: `evolver/validator/activation.py`, `evolver/validator/_probe.py`
- Create: `tests/evolver_validator_stub.py` (test helper beside `tests/acp_stub_server.py`, not collected: `make_stub_agent(root: Path, *, mode: str, reply: str = "pong", extra_env: dict | None = None, render: bool = True) -> Path` writes `subagent.json` (name `Stub-Agent`), `config.json`, and a `run.py` that sets `ACP_STUB_MODE` / `ACP_STUB_REPLY` / extras; when `render` is true it writes, as a launcher does, `.config.rendered.<os.getpid()>.json` under `raven.config.product_render.product_state_root("stub-agent")` holding `$RAVEN_HOME/config.json` without its `providers`; then it `os.execv`s `sys.executable` on `tests/acp_stub_server.py`)
- Test: `tests/test_evolver_validator_activation.py`

**Interfaces:**
- Consumes: `IsolatedHome`, `Snapshot`, `package_manifests`.
- Produces:
  - `def check_activation(home: IsolatedHome, snap: Snapshot, *, model: str) -> tuple[LayerResult, dict]` -- returns the layer and the `rendered` summary `{"model": ..., "memory_backend": ...}`.
  - `def check_rendered(rendered: dict, *, model: str) -> list[RuleResult]` (A3)
  - `_probe.py` child: `python -m evolver.validator._probe --config <path> --plugin <id> ... --workspace <dir>` prints JSON `{"<id>": {"tools": {name: "registered"|"declined"|"failed: <msg>"}, "hooks": {...}, "services": {...}, "tool_gates": {...}, "session_observers": {...}}, "activation_failures": [{"plugin": id, "cause": str}]}`.

Behaviour:
- A1: `discover_product_rows(home.path / "agents")`; exactly one row whose `name` equals the snapshot's `subagent.json` name, `enabled` true; otherwise the failure message is `product_state(home.path / "agents")[name].detail`.
- A2: start `command_argv(row.command)` with `cwd=row.cwd`, `env=home.env`; write one `initialize` frame (as `_smoke_handshake` in `raven/cli/agents_commands.py` does); a first stdout line holding a JSON-RPC `result` within `row.ready_timeout_ms` passes. A launcher exiting with "no provider key to inherit from" on stderr raises `ValidatorEnvironmentError`. Keep the process alive until A3 has copied the rendered file, then terminate it.
- A3: find `home.path.rglob(f".config.rendered.{proc.pid}.json")`, copy it to `home.path / "rendered.json"`; pass when `agents.defaults.model == model` and `memory.backend is None`.
- A4: run `_probe` on `rendered.json` with the package's plugin ids. Inside the probe, mirror `build_plugin_tools` and its siblings (`raven/core/plugin_stack.py:334` onward) per contribution, calling `registry.build_tool` / `build_hook` / `build_service` / `build_tool_gate` / `build_session_observer` inside `try`: an exception is `failed`, `None` is `declined`. Pass when no package contribution is `failed` and no package id is in `activation_failures`.
- A5: same probe on a copy with `plugins.config[<id>]` removed for each package id; pass when no package contribution is `registered`.

- [ ] **Step 1: Write the failing tests**

```python
def test_discovery_matches_manifest_name(tmp_path): ...  # folder "stub-agent", manifest name "Stub-Agent" -> A1 pass
def test_engine_not_importable_fails_a1_with_reason(tmp_path): ...  # subagent.json "engine" names a missing package
def test_handshake_passes_against_stub(tmp_path): ...  # mode "ok"
def test_handshake_rejected_fails_a2(tmp_path): ...  # mode "reject_init"
def test_rendered_wrong_model_or_memory_on_fails_a3(): ...  # check_rendered unit cases
def test_probe_reports_scaffold_tool_registered_then_declined_without_slice(tmp_path, monkeypatch): ...
    # rendered = scaffold config.json + plugins.dirs=[<copy>/plugins] + model + memory off
    # A4 passes with "<prefix>_hello" registered; A5 passes with it declined
def test_factory_ignoring_its_slice_fails_a5(tmp_path, monkeypatch): ...  # mutant: tool factory never returns None
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `activation.py`, `_probe.py`, and the stub fixture helper.**
- [ ] **Step 4: Run to verify pass**, then confirm no stub process survives: `pgrep -f acp_stub_server.py` prints nothing.
- [ ] **Step 5: Stop and report; commit on the owner's word.**

---

### Task 8: Layer E (end to end)

**Files:**
- Create: `evolver/validator/dispatch.py`, `evolver/validator/_dispatch.py`
- Test: `tests/test_evolver_validator_e2e.py`

**Interfaces:**
- Consumes: `IsolatedHome`, `Suite`, `Case`, `CaseRun`, `FileRecord`, `make_stub_agent` (Task 7).
- Produces: `def run_cases(home: IsolatedHome, *, agent_name: str, suite: Suite, run_dir: Path, run_id: str) -> list[CaseRun]`; `def e2e_layer(runs: list[CaseRun]) -> LayerResult` (one `E1` result per case).

Behaviour:
- Parent writes each case's files into `run_dir/cases/<case_id>/` (fresh), records their hashes, writes `run_dir/dispatch-job.json` `{"agents_root", "agent_name", "run_id", "cases": [{"id", "task", "workspace", "timeout_s"}], "out"}` and runs `sys.executable -m evolver.validator._dispatch <job>` with `env=home.env`, overall timeout = sum of case timeouts + 120 s.
- Child: one `AcpConnectionPool()`; the row from `discover_product_rows(agents_root)`; `backend = build_third_party_backend(row, pool=pool, timeout=case.timeout_s)`; per case, sequentially, `reply = await backend.run(task, task_id=case_id, workspace=Path(ws), executor=None, session_key=f"validate:{run_id}:{case_id}", on_delta=<append to full_text>)` under `asyncio.wait_for(..., case.timeout_s + 30)`; any exception (including `AcpEmptyTurnError`, `TimeoutError`) is recorded as `error`; `await pool.close_all()` in `finally`; results JSON to `out`.
- Parent builds `FileRecord`s for files created or changed in each workspace: `text` when UTF-8 decodable, capped at 65536 characters with `truncated=True` beyond.
- `E1` fails for a case with `error` set or an empty `reply`.

- [ ] **Step 1: Write the failing tests** (stub agent from Task 7, isolated home from Task 6, a fake `providers` block)

```python
def test_reply_and_files_are_collected(tmp_path): ...  # mode "writes_file"
    assert run.reply == "wrote it" and run.error is None
    assert {f.path: f.text for f in run.files}["out/result.txt"] == "made by the agent\n"
def test_input_files_unchanged_are_not_reported(tmp_path): ...
def test_empty_turn_fails_e1(tmp_path): ...  # extra_env {"ACP_STUB_EMPTY_PROMPTS": "1"}
def test_timeout_fails_e1_and_leaves_no_process(tmp_path): ...  # ACP_STUB_PROMPT_DELAY_S=5, timeout_s=1
def test_large_text_file_is_truncated_and_hashed(tmp_path): ...
```

- [ ] **Step 2: Run to verify failure** -- `uv run --frozen --all-extras pytest tests/test_evolver_validator_e2e.py -q`.
- [ ] **Step 3: Implement `dispatch.py` and `_dispatch.py`.**
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Stop and report; commit on the owner's word.**

---

### Task 9: Layer J (judge)

**Files:**
- Create: `evolver/validator/judge.py`, `evolver/validator/judge_prompt.md`
- Test: `tests/test_evolver_validator_judge.py`

**Interfaces:**
- Consumes: `Case`, `CaseRun`, `CriterionVerdict`, `CaseVerdict`.
- Produces:
  - `class JudgeFormatError(ValueError)`
  - `def build_judge_messages(case: Case, run: CaseRun) -> list[dict]` -- system text from `judge_prompt.md`; user content holds task, input file names (contents for UTF-8 files up to 8192 characters), criteria with ids, reference, `run.reply`, delivered `FileRecord` texts with an explicit `[truncated]` marker. Never `run.full_text`.
  - `def parse_judgement(text: str, criterion_ids: list[str]) -> list[CriterionVerdict]` -- the first JSON object in `text`; `criteria` must cover every id exactly once, no unknown ids, verdict in the three values.
  - `def combine(votes: list[list[CriterionVerdict]]) -> list[CriterionVerdict]` -- majority per id; a tie is `fail`.
  - `async def judge_case(provider, *, model: str, case: Case, run: CaseRun, repeats: int) -> CaseVerdict` -- `provider.chat_with_retry(messages, model=model, temperature=0.0)`, `.content`; one retry per vote on `JudgeFormatError`, then `error` set and `passed=False`. A case passes when no verdict is `fail` and at least one is `pass`; all `not_applicable` fails with error "no applicable criterion".
  - `def judge_layer(verdicts: list[CaseVerdict], total: int, *, min_pass_rate: float) -> LayerResult` -- one `J1`.

- [ ] **Step 1: Write the failing tests** (a `ScriptedProvider` returning queued `LLMResponse(content=...)`)

```python
def test_prompt_contains_task_criteria_reply_and_files_not_transcript(): ...
def test_all_pass_case_passes(): ...
def test_missing_criterion_id_retries_then_errors(): ...  # two malformed replies -> error, passed False
def test_duplicate_or_unknown_id_is_rejected(): ...
def test_majority_of_three_and_tie_fails(): ...  # [pass,pass,fail] -> pass; [pass,fail] -> fail
def test_all_not_applicable_fails_case(): ...
def test_min_pass_rate_threshold(): ...  # 1 of 2 passed: 1.0 -> fail, 0.5 -> pass
```

- [ ] **Step 2: Run to verify failure** -- `uv run --frozen --all-extras pytest tests/test_evolver_validator_judge.py -q`.
- [ ] **Step 3: Implement `judge.py` and write `judge_prompt.md`** (judge only the listed criteria; a criterion the task did not ask for is `not_applicable`; answer with the JSON shape only).
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Stop and report; commit on the owner's word.**

---

### Task 10: Runner and the `validate` subcommand

**Files:**
- Create: `evolver/validator/runner.py`
- Modify: `evolver/cli.py` (new `validate` subparser; it does not use `common()`, which requires `--config`)
- Test: `tests/test_evolver_validator_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `@dataclass class ValidateOptions:` folder, suite (Path | None), engine, host_config, model, judge_model, judge_repeats, min_pass_rate, report_path, keep, static_only, `run_dir: Path | None = None`, `judge_provider_factory: Callable[[Path, str], Any] | None = None` (test seam; default `make_provider(load_config(<home>/config.json), model=...)`)
  - `def validate(opts: ValidateOptions) -> Report`
  - `def cli_main(args: argparse.Namespace) -> int` -- prints one line per layer and per case, writes the report, returns the exit code; maps `ValidatorInputError` -> 2 and `ValidatorEnvironmentError` -> 3.
- `evolver/cli.py`: `validate FOLDER --suite --engine --host-config --model --judge-model --judge-repeats --min-pass-rate --report --keep --static-only`; `--host-config` default `raven.config.paths` active config path; `main` dispatches `validate` to `cli_main`.

Order: inputs (folder exists; suite loads unless `--static-only`, else exit 2) -> snapshot -> F -> D -> C -> home -> A -> (`--static-only`: STATIC-OK) -> E -> J -> outcome. Stop after the first failing layer. `report.json` defaults to `<run_dir>/report.json`; `run_dir` defaults to a new `tempfile.mkdtemp(prefix="evolver-validate-")`. `home/` removed in `finally` unless `--keep`.

- [ ] **Step 1: Write the failing tests** (stub agent and scripted judge through `judge_provider_factory`)

```python
def test_missing_folder_exits_2(): ...  # main(["validate", "/nope", "--suite", s]) == 2
def test_missing_suite_without_static_only_exits_2(): ...
def test_first_failing_layer_stops_the_run(tmp_path): ...  # F1 mutant: report has only layer "F", no home dir
def test_static_only_reports_static_ok(tmp_path): ...  # stub agent, outcome "STATIC-OK", exit 0
def test_full_pass_with_stub_agent_and_judge(tmp_path): ...  # mode "ok", judge pass -> PASS, exit 0
def test_judge_fail_exits_1(tmp_path): ...
def test_home_removed_when_a_layer_raises(tmp_path, monkeypatch): ...  # run_cases raises -> home gone, report written
def test_keep_retains_home(tmp_path): ...
def test_report_json_has_digests_models_and_layers(tmp_path): ...
```

- [ ] **Step 2: Run to verify failure** -- `uv run --frozen --all-extras pytest tests/test_evolver_validator_cli.py -q`.
- [ ] **Step 3: Implement `runner.py` and the subparser.**
- [ ] **Step 4: Run all validator tests and the existing evolver CLI tests**: `uv run --frozen --all-extras pytest tests/test_evolver_validator_*.py tests/test_evolver_launch.py -q`, expected PASS.
- [ ] **Step 5: Stop and report; commit on the owner's word.**

---

### Task 11: Documentation and the domain term

**Files:**
- Modify: `evolver/README.md` (new "Validating an agent package" section; documentation-map row), `evolver/__init__.py` (package layout line for `validator`), `docs-site/docs/evolver.md` and `docs-site/docs/evolver.zh.md` (the subcommand, the six layers, the suite format), `agents/BUILDING.md` (one-line pointer in section 8), `CONTEXT.md` (**Acceptance suite** entry, definition checkable against `evolver/validator/suite.py`)

- [ ] **Step 1: Write the sections.** Describe the design as it stands; no history.
- [ ] **Step 2: Verify**: `make check-large-files check-source-language` exits 0; `uv run --frozen --all-extras pytest tests/test_scope_canon.py -q` still passes.
- [ ] **Step 3: Stop and report; commit on the owner's word** (`docs(evolver): document the agent package validator`).

---

### Task 12: Real-resource checks

**Files:**
- Create: `tests/fixtures/evolver_validator/raven_code_suite/suite.json`, `.../cases/hello.json`, `.../cases/mismatch.json`
- Create: `tests/integration/test_evolver_validator_real_llm.py`, `tests/integration/test_evolver_validator_smoke.py`

Values: `hello` asks the agent to create `hello.txt` containing exactly `hello validator`; criterion "hello.txt exists and its content is exactly `hello validator`". `mismatch` asks the same task with criterion "hello.txt contains the word goodbye". Use the opt-in marker and skip reason the existing `tests/integration/*real_llm*` files use.

- [ ] **Step 1: Write the tests**

```python
def test_raven_code_one_pass_one_fail_on_deepseek_flash(tmp_path):
    report = validate(ValidateOptions(folder=REPO / "agents/raven-code", suite=SUITE, model=FLASH,
                                      judge_model=FLASH, min_pass_rate=1.0, run_dir=tmp_path, ...))
    assert [v.passed for v in report.verdicts] == [True, False] and report.outcome == "FAIL"

@pytest.mark.parametrize("name, engine", SHIPPED)
def test_shipped_agent_passes_activation(name, engine, tmp_path): ...  # validate(..., static_only=True).outcome == "STATIC-OK"
```

- [ ] **Step 2: Run them with credentials**: `uv run --frozen --all-extras pytest tests/integration/test_evolver_validator_real_llm.py tests/integration/test_evolver_validator_smoke.py -q` (with the opt-in marker enabled). Expected PASS; any shipped agent failing A is analysed as in Task 5.
- [ ] **Step 3: Stop and report** the measured outcome and the model each turn used; commit on the owner's word.
