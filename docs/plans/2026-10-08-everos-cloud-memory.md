# EverOS Cloud memory plugin -- implementation plan

Status: plan; G3 passed 2026-10-08

> For the executor: run this under `dev-workflow` stage 4; tick steps with
> `- [ ]`; when something this plan does not cover comes up, read Global
> Constraints first, then the three tiers of `unattended-run` section 7 -- do
> not guess. Each task is self-contained: read only the task you are on, plus
> this header.

**Goal**: a person on `main` can install `everos-cloud-memory`, pick it in
`raven onboard`, and have recall, storage, the sub-agent handoff, the importer,
`raven doctor`, the memory page and the settings page all run against EverOS
Cloud, while a person on the local plugin sees nothing change.
**Design document**: `docs/specs/2026-10-08-everos-cloud-memory-design.md`
(A1-A39, C1-C18, H1-H16).
**Acceptance cases**: `docs/specs/2026-10-08-everos-cloud-memory-acceptance.md`
(T1.1-T39.2; stage 4 runs them).
**Approach**: a thin plugin distribution over the cloud's REST API, copied from
`raven_everos.backend` and stripped of the local service state machine; fifteen
small host edits and one ui-web card for parity; a fake cloud fixture that both
the unit tests and the real-host acceptance runs use.
**Stack**: Python 3.12, `httpx`, `uv` workspace member; `typer` / `questionary`
for the wizard; pydantic models mirrored in `rpc-schema/openrpc.json`;
React + vitest in `ui-web`; `i18n/messages.json` for every GUI string.
**Change map**: the spec's Overview diagram; the host-edit table (H1-H16) names
the files, and the task numbers below map onto it: Task 1 = H16, Task 2 = the
plugin + H11, Task 3 = H9 + H10 + H12, Task 4 = H1, Task 5 = H2-H5, Task 6 =
H6-H8 + H15 (+ the RPC contract files), Task 7 = H13, Task 8 = H14, Task 9 =
the live tests of C14-C18, Task 10 = the acceptance run.

Working tree: `/Users/admin/Raven-b`, branch `feat/everos_cloud_memory_plugin`
(base `3632e6040`). Every `git` command names the tree: `git -C /Users/admin/Raven-b ...`.
Every Python command runs through `uv run --frozen ...` from that directory.
Baselines measured there on 2026-10-08, before any code change:

```
tests/test_plugin_boundary.py                 4 tests collected
tests/test_release_plugin_list.py             6 tests collected
tests/test_everos_backend_contract.py        11 tests collected
tests/test_rpc_memory.py                     17 tests collected
tests/test_rpc_settings.py                  107 tests collected
tests/test_cli_onboard_commands.py          348 tests collected
grep -c "uv build --wheel plugins-dist/" .github/workflows/release.yml -> 3
```

`tests/test_cli_onboard_commands.py::test_onboard_cli_writes_the_keys_passed_as_flags`
is red when the file runs in the same xdist batch as `test_rpc_settings.py` and
green alone (deviations.md D1, recorded before any change); judge that file by
a solo rerun of a failing test.

## Global Constraints

Verbatim from the spec's Constraints table. Every task's requirements include
these.

| # | Kind | Constraint | How it is checked |
|---|---|---|---|
| C1 | architecture | The plugin imports the host only through `raven.contracts.memory`, `raven.plugins` and `raven.config.update` (to write its own slice); never `raven.cli`, `raven.config.loader`, `raven.config.raven`. | `tests/test_plugin_boundary.py::test_plugin_does_not_import_host_private_modules`, with `PLUGIN_DIR` widened to every `plugins-dist/*/raven_*` package. |
| C2 | architecture | The host imports `raven_everos_cloud` from `raven/rpc/methods/memory.py` and `raven/rpc/methods/console.py` only -- the two files already allow-listed as EverOS wire-protocol surfaces. | `test_plugin_boundary.py::test_host_does_not_import_plugin_internals`, regex widened to `raven_everos(_cloud)?\b`; `HOST_WIRE_PROTOCOL_SURFACES` unchanged. |
| C3 | architecture | The plugin's dependency table is `raven` and `httpx`; it never imports `everos` or `raven_everos`. | `plugins-dist/everos-cloud-memory/pyproject.toml`; a test imports every module of the package with `everos` and `raven_everos` blocked in `sys.modules`. |
| C4 | design | Owner ids come from `ServiceLocator` only. The cloud slice declares no `user_id` / `agent_id`; a stale one is warned about and ignored. | manifest `config_schema` has no identity key; unit test with a slice carrying `user_id` asserts the wire uses the locator's. |
| C5 | design | Every `/memory/add` carries `mode: "agent"`. | fake-transport test asserts the body of every add. |
| C6 | design | `/memory/flush` is sent only on an explicit end and in `stop()`. Twenty ordinary turns produce zero flushes. | fake-transport test counts flush calls across N plain `store` calls (0), one `flush` metadata store (1), one `is_final` store (1), one `stop()` after unflushed writes (1 per session). |
| C7 | design | Key resolution is config first, `EVEROS_CLOUD_API_KEY` second; a key that came from the environment is never written to `config.json`. | unit tests on the resolver; onboard test asserts the slice after an env-sourced run has no `api_key`. |
| C8 | design | No contract method raises. `recall` answers within 4.0 s on both tracks (the per-turn user-track caller abandons at 5.0 s, `raven/context_engine/segments/memory.py:27`; the skill router has no budget of its own, `raven/memory_engine/skill_forge/backend_source.py:95`, so this bound is the only one it gets). `recall_session` answers within 10.0 s (its caller allows 30.0 s, `raven/agent/subagent_memory.py:42`, and a read-back right after extraction is the slow case). `store` returns `False` on any failure. `health` answers within 5.0 s. | `tests/test_everos_cloud_backend_contract.py` inherits `MemoryBackendContractTests` and `LifecycleContractTests`; fault-injection tests per failure row with a hanging fake and a clock. |
| C9 | design | With `memory.backend` equal to `"everos"` or `None`, every host surface behaves exactly as before. A backend name that is neither (`"everos-cloud"` today, any future plugin, a stale name) is the one case the host edits change: role slots hidden, settings role writes refused, no EverOS restart. | existing suites unchanged and green: `test_cli_onboard_commands.py`, `test_rpc_settings.py`, `test_rpc_memory.py`, `test_everos_*`; one explicit regression test per surface for the `everos` and `None` branches. |
| C10 | design | Request bodies respect Cloud limits: at most 500 messages and under 300 KB per add. | unit test: a 600-message `store` issues two adds; a 400 KB slice splits by message. |
| C11 | design | Source language is English, including the plugin, tests and the `i18n/messages.json` `en` column; `zh` entries only where the catalogue already carries them. | `make check-source-language`. |
| C12 | design | GUI text goes through `t(key)` with entries in `i18n/messages.json`; no CJK literal enters a `.tsx`. | `ui-web/scripts/gates/first-frame-literals.test.mjs`; `npm run gen:check` in `ui-web` and `lint:rpc` in `ui-tui` for the regenerated RPC types. |
| C13 | design | The release plugin list stays at three distributions. | `tests/test_release_plugin_list.py` unchanged and green; `release.yml` keeps `-eq 3`. |
| C14 | assumption | Cloud `/api/v2/memory/{add,flush,get,search}` accept the request bodies the local plugin sends (`SearchRequest`, `MemorizeAddRequest`, get-by-type), differing only in `mode` being per request. | Read from docs.evermind.ai (`llms-full.txt`) on 2026-10-08. **Unverified against the live service**: `tests/integration/test_everos_cloud_real_cloud.py::test_add_flush_search_roundtrip` and `::test_get_by_session` run only with `EVEROS_CLOUD_API_KEY` set and are the first thing to run when a key exists. |
| C15 | assumption | `/memory/flush` exists on Cloud and answers `no_extraction` harmlessly when nothing is pending. | docs.evermind.ai 2026-10-08; EverOS's own `everos demo --live` client calls it against `api.evermind.ai`. Live: `test_everos_cloud_real_cloud.py::test_flush_with_nothing_pending_is_no_extraction`. |
| C16 | assumption | HTTP 401 means "invalid or missing token"; 403 means "account not authorized for v2". | docs.evermind.ai error table, 2026-10-08. Live: `test_everos_cloud_real_cloud.py::test_wrong_key_is_401` (a deliberately wrong key). The 403 half cannot be exercised without an un-entitled account; it stays documentation-backed and the spec words it as a hint, not a certainty ("may mean ..."). |
| C17 | assumption | Cloud extracts on its own after `add` ("you rarely need flush"), so a session that never sees an explicit end is still extracted eventually. | docs.evermind.ai 2026-10-08. Live: `test_everos_cloud_real_cloud.py::test_add_without_flush_is_searchable_within_two_minutes` (polls `/search`; skipped by default, run by hand -- it costs two minutes). The `stop()` sweep is the hedge. |
| C18 | assumption | `POST /memory/get` with `page_size: 1` is accepted with a key and refused without one, so it can stand as the health probe. | Live: `test_everos_cloud_real_cloud.py::test_get_page_one_with_and_without_key`. |

Repository rules that bind every task (AGENTS.md): source and docs in English
(section 1.3); `uv` only, never `pip`, never a hand edit of a dependency table
(section 4); tests through `uv run pytest`, CLI tests in the existing
`tests/test_cli_<module>_commands.py` (section 5); canonical terms from
`CONTEXT.md`, new ones defined there in the same change (section 6); no SVG,
HTML or files over 1 MiB (section 7).

### Authorization (this run)

Baseline: `unattended-run` section 1, narrowed by AGENTS.md section 3.4 / 3.6,
which wins in this repository: **no commit and no push without the owner's
word in chat**; "commit per task" below is a checkpoint the executor asks for,
not a pre-authorization. Pre-authorized: rebasing onto `github/main`; building
and running the fake cloud and isolated Raven instances on free ports under
`$SCRATCH`; creating temporary directories and processes (torn down and listed
in `deviations.md`); editing every file this plan names; adding workspace
members with `uv add`. Never pre-authorized: editing `AGENTS.md`, `CLAUDE.md`
or `CONTEXT-MAP.md`; touching the owner's `~/.raven`, the owner's running
gateway or EverOS, or any process not started by this run. Pushing goes to
GitHub over SSH (`git@github.com:EverMind-AI/Raven.git`), never to the GitLab
`origin`; a PR is offered after the push, and when the owner says "send", the
PR is created without a description preview (project memory).

### Caps and goal

Rounds 40, time 48 h (defaults). Goal text, to be pasted into `status.md` and
`/goal` when the owner starts one:

> Every `real` case in `docs/specs/2026-10-08-everos-cloud-memory-acceptance.md`
> passes with evidence under `.work_context/everos_cloud_memory/acceptance/evidence/`
> and its commands rerun as written; the PR is open against `main`; every CI
> job's final state is green on the current head; every blocker from the G4
> self-review is handled; every external review comment that has arrived is
> handled or answered in the PR with the reason it is not changed; **and the
> owner confirms acceptance in chat**. Or `deviations.md` gains a stop-tier
> entry. Or 40 rounds. Or 48 hours.

---

### Task 1: the fake cloud (H16)

**Delivers**: the fixture every later task tests against; the acceptance
document's section 1.2. No A of its own; A10-A18, A33, A34 and every `real`
case depend on it.

**Files**:
- new: `tests/_everos_cloud_fake.py`
- new: `tests/test_everos_cloud_fake.py`

**Interfaces**:
- produces `class FakeCloud` with `__init__(self, *, mode: str = "ok", hang_on: set[str] = frozenset(), ledger: list[dict] | None = None)`,
  `transport(self) -> httpx.MockTransport`, `set_mode(self, mode: str, hang_on: set[str] | None = None) -> None`,
  `ledger: list[dict]` (entries `{ts, method, path, headers, body, status}`),
  `serve(self, port: int, ledger_path: Path | None, with_health: bool) -> None` (blocking; `python -m tests._everos_cloud_fake --port N --mode M --ledger F [--with-health] [--hang-on PATH]...`),
  and the HTTP control route `POST /_fake/mode {"mode": str, "hang_on": [str]}`.
- sentinels: `EPISODE_SUBJECT = "ECM-EP-5c1d"`, `TOTALS = {"episode": 7, "profile": 1, "agent_case": 2, "agent_skill": 3}`, `REFUSED_MESSAGE = "ECM-REFUSED-401"`.
- the field table of the acceptance document section 1.2 is the behaviour contract: unknown field -> 400 naming it; `user_id` XOR `agent_id` else 422; `memory_type` must match the owner's track else 422; add body over 500 messages or 300 KB -> 413; every message needs `sender_id`, `role`, integer `timestamp`, `content`.

- [ ] **Step 1: write the tests first** (there is no real dependency to smoke; the fake *is* the stand-in, and the docs copy at `.work_context/everos_cloud_memory/everos-cloud-api-llms-full-20261008.txt` is its source)

```python
# tests/test_everos_cloud_fake.py
"""The fake cloud honours or refuses every field the backend can send."""
import httpx, pytest
from tests._everos_cloud_fake import FakeCloud, EPISODE_SUBJECT

async def _post(fake, path, body, key="k"):
    async with httpx.AsyncClient(transport=fake.transport(), base_url="http://fake") as c:
        return await c.post(path, json=body, headers={"Authorization": f"Bearer {key}"})

async def test_search_requires_exactly_one_owner():
    fake = FakeCloud()
    r = await _post(fake, "/api/v2/memory/search", {"query": "q", "top_k": 5})
    assert r.status_code == 422 and "user_id" in r.text and "agent_id" in r.text
    r = await _post(fake, "/api/v2/memory/search", {"query": "q", "top_k": 5, "user_id": "u", "agent_id": "a"})
    assert r.status_code == 422

async def test_agent_rows_only_for_an_agent_owner():
    fake = FakeCloud()
    r = await _post(fake, "/api/v2/memory/search", {"query": "q", "top_k": 5, "user_id": "u"})
    assert r.json()["data"]["agent_skills"] == [] and r.json()["data"]["episodes"][0]["subject"] == EPISODE_SUBJECT
    r = await _post(fake, "/api/v2/memory/search", {"query": "q", "top_k": 5, "agent_id": "a"})
    assert r.json()["data"]["episodes"] == [] and r.json()["data"]["agent_skills"]

async def test_unknown_field_is_refused_by_name():
    r = await _post(FakeCloud(), "/api/v2/memory/search", {"query": "q", "top_k": 5, "user_id": "u", "bogus": 1})
    assert r.status_code == 400 and "bogus" in r.text

async def test_add_limits_and_message_shape():
    fake = FakeCloud()
    msg = {"sender_id": "u", "role": "user", "timestamp": 1_700_000_000_000, "content": "x"}
    r = await _post(fake, "/api/v2/memory/add", {"session_id": "s", "mode": "agent", "messages": [msg] * 501})
    assert r.status_code == 413
    r = await _post(fake, "/api/v2/memory/add", {"session_id": "s", "mode": "agent", "messages": [{**msg, "timestamp": "2026"}]})
    assert r.status_code == 400 and "timestamp" in r.text

async def test_ledger_records_unknown_routes_too():
    fake = FakeCloud()
    await _post(fake, "/health", {})
    assert fake.ledger[-1]["path"] == "/health" and fake.ledger[-1]["status"] == 404

async def test_mode_switch_changes_the_answer():
    fake = FakeCloud()
    fake.set_mode("401")
    r = await _post(fake, "/api/v2/memory/get", {"user_id": "u", "memory_type": "episode", "page_size": 1})
    assert r.status_code == 401 and r.json()["error"]["message"] == "ECM-REFUSED-401"
```

- [ ] **Step 2: implement** `tests/_everos_cloud_fake.py` -- a pure-Python request handler `handle(method, path, headers, body) -> (status, json)` used by both faces (the `MockTransport` wraps it; the HTTP server wraps it with `http.server` and a `--ledger` JSONL writer); modes as a dict of answer builders; `hang` sleeps forever on the named routes (in the transport, `await asyncio.sleep(3600)`).

- [ ] **Step 3: run, then prove it can fail**

Run: `cd /Users/admin/Raven-b && uv run --frozen pytest tests/test_everos_cloud_fake.py -q -p no:cacheprovider`
Expected: `6 passed` (copy the actual line).
Mutation: delete the XOR check in `handle` -> `test_search_requires_exactly_one_owner` fails on its first assertion; restore; `git -C /Users/admin/Raven-b diff --stat -- tests/_everos_cloud_fake.py` prints nothing.

- [ ] **Step 4: smoke the HTTP face once**

Run (through the host's background runner, with the section 8 watchdog from the acceptance document 1.2): `python -m tests._everos_cloud_fake --port $FAKE_PORT --mode ok --ledger $SCRATCH/ecm-fake/requests.jsonl`, then
`curl -s -X POST http://127.0.0.1:$FAKE_PORT/api/v2/memory/get -H 'Authorization: Bearer k' -H 'Content-Type: application/json' -d '{"user_id":"u","memory_type":"episode","page_size":1}' | python3 -m json.tool | head -5`
Expected: `"total_count": 7` among the lines; `tail -1 $SCRATCH/ecm-fake/requests.jsonl` shows the request with its header. Kill the server by the pid from `lsof -tiTCP:$FAKE_PORT -sTCP:LISTEN`.

- [ ] **Step 5: record** -- nothing to record unless the docs copy and the fake disagree somewhere; a disagreement goes to `deviations.md` as assumption-mismatch against C14.

---

### Task 2: the plugin distribution (H11)

**Delivers**: A2, A4, A5, A6, A7, A28 (the screen), A10, A11, A12, A13, A14, A15,
A16, A17, A18, A27 (the health table), A33, A34 at the contract level; the
real-host halves land in Task 10.

**Files**:
- new: `plugins-dist/everos-cloud-memory/pyproject.toml`
- new: `plugins-dist/everos-cloud-memory/raven_everos_cloud/__init__.py`
- new: `plugins-dist/everos-cloud-memory/raven_everos_cloud/raven-plugin.toml`
- new: `plugins-dist/everos-cloud-memory/raven_everos_cloud/backend.py`
- new: `plugins-dist/everos-cloud-memory/raven_everos_cloud/onboard.py`
- modify: `pyproject.toml:385` (`[tool.ruff]` `src` list -- not a dependency table, a hand edit is allowed) and, through `uv add --dev everos-cloud-memory`, the dev-dependency list and `[tool.uv.sources]` (`:151-153`, `:222-224`), plus `uv.lock`
- new tests: `tests/test_everos_cloud_backend.py`, `tests/test_everos_cloud_backend_contract.py`, `tests/test_everos_cloud_onboard.py`, `tests/test_everos_cloud_discover.py`

**Interfaces**:
- consumes `FakeCloud` (Task 1); `raven.contracts.memory.{Memory, BackendHealth, HealthCheck}`; `raven.plugins.{PluginContext, OnboardUI, StepOutcome}`; `raven.config.update.set_plugin_config_fields(plugin_id, fields)`.
- produces, in `raven_everos_cloud.backend`: `DEFAULT_BASE_URL = "https://api.evermind.ai"`, `ENV_KEY = "EVEROS_CLOUD_API_KEY"`, `KEYS_URL = "https://everos.evermind.ai"`, `resolve_api_key(config: Mapping[str, Any]) -> tuple[str, str | None]`, `class EverosCloudBackend(ctx, *, client=None)` implementing the `MemoryBackend` Protocol, `make_backend(ctx) -> EverosCloudBackend`; constants `RECALL_TIMEOUT_S = 4.0`, `SESSION_TIMEOUT_S = 10.0`, `ADD_TIMEOUT_S = 30.0`, `FLUSH_TIMEOUT_S = float(os.environ.get("RAVEN_EVEROS_CLOUD_FLUSH_TIMEOUT_S", "360"))`, `HEALTH_TIMEOUT_S = 5.0`, `SHUTDOWN_FLUSH_BUDGET_S = 5.0`, `ADD_MAX_MESSAGES = 500`, `ADD_MAX_BYTES = 250_000`; log lines `"recall timed out after %.1f s; returning empty"` and `"flushed %d session(s) at stop"`.
- produces, in `raven_everos_cloud.onboard`: `class CloudKeyScreen(ctx, *, client_factory=None)` with `run(ui, *, step_no, non_interactive, main_model, warnings, skip_test) -> StepOutcome` and `configured() -> bool`; `make_onboard_step(ctx) -> CloudKeyScreen`. Screen strings exactly as the spec's onboard.py sketch, including the sharing hint naming `memory.userId`.
- manifest: id `everos-cloud-memory`, `memory_backends` name `everos-cloud`, `onboard` name `everos-cloud`, `config_schema` with `api_key` and `base_url` (strings) only.

- [ ] **Step 1: scaffold and register the member**

```bash
cd /Users/admin/Raven-b
mkdir -p plugins-dist/everos-cloud-memory/raven_everos_cloud
# write pyproject.toml (copy plugins-dist/everos-memory/pyproject.toml, name everos-cloud-memory, version 0.1.0,
#   dependencies = ["raven", "httpx>=0.28.0,<1.0.0"], entry point everos-cloud-memory = "raven_everos_cloud",
#   hatch include raven_everos_cloud/**/*.py and raven_everos_cloud/raven-plugin.toml)
# write raven-plugin.toml exactly as the spec's "The plugin's manifest" block
cat > plugins-dist/everos-cloud-memory/raven_everos_cloud/__init__.py <<'INIT'
"""EverOS Cloud memory backend for Raven.

Implements the host's :class:`raven.memory_engine.MemoryBackend` Protocol over
HTTPS against EverOS Cloud (``https://api.evermind.ai``); ``backend.make_backend``
is the factory the registry calls. Kept import-cheap: discovery resolves
``raven-plugin.toml`` through this module, so it imports neither ``httpx`` nor
``backend``.
"""

__version__ = "0.1.0"
INIT
uv add --dev everos-cloud-memory          # adds the dev dep and the workspace source
uv sync --frozen 2>/dev/null || uv sync   # a fresh lock is expected here
uv run --frozen python -c "import raven_everos_cloud, importlib.metadata as m; print(m.version('everos-cloud-memory'))"
```
Expected: `0.1.0`. Then add `"plugins-dist/everos-cloud-memory"` to the `src` list at `pyproject.toml:385`.

- [ ] **Step 2: write the tests first**

`tests/test_everos_cloud_backend.py` -- one test per row, each over `FakeCloud().transport()` injected as `client=httpx.AsyncClient(transport=...)` and a `PluginContext(config=..., services=ServiceLocator(workspace=tmp_path, user_id="ecm-user", agent_id="ecm-agent"), logger=...)`:
`test_key_resolution_prefers_the_file` (C7), `test_recall_sends_bearer_and_include_profile_and_sorts` (A10), `test_profile_is_capped_with_marker` (A10), `test_store_sends_mode_agent_and_owner_ids_and_no_flush` (A11, 20 turns -> 0 flush), `test_recall_hang_returns_empty_within_bound` (A12, patch `RECALL_TIMEOUT_S` to 0.3 and assert `[]` within 0.6 s; same for `recall_session` with `SESSION_TIMEOUT_S`), `test_recall_500_and_429_return_empty_and_store_false` (A12), `test_agent_track_maps_skills_and_cases` (A13), `test_both_or_neither_owner_is_empty_without_a_request` (A13), `test_flush_metadata_flushes_once_with_per_call_owners` (A14, snake), `test_camel_case_owner_override_also_fires` (A14), `test_recall_session_filters_by_session_and_track` (A15), `test_is_final_flushes_once_after_the_last_batch` (A16), `test_six_hundred_messages_split_into_two_adds` (A17), `test_oversize_message_fails_its_own_add_and_logs_index` (A17), `test_stop_flushes_only_unflushed_sessions_and_closes` (A18), `test_stop_hang_returns_within_budget_and_warns` (A18, `SHUTDOWN_FLUSH_BUDGET_S` patched to 0.5), `test_hung_flush_keeps_the_session_for_the_sweep` (A34, `RAVEN_EVEROS_CLOUD_FLUSH_TIMEOUT_S=0.3` via `monkeypatch.setenv` before import of the constant -- read it at call time, not import time, so the env hook works in a running process too), `test_health_classifies_every_answer` (A27: the seven rows of the health table, parametrised), `test_stale_identity_in_slice_is_ignored_with_a_warning` (C4), `test_no_key_means_no_request` (recall `[]`, store `False`, health `missing`).

`tests/test_everos_cloud_backend_contract.py` -- mirrors `tests/test_everos_backend_contract.py:1-30`: subclasses of `MemoryBackendContractTests` and `LifecycleContractTests` whose `make_backend` builds `EverosCloudBackend` over `FakeCloud`.

`tests/test_everos_cloud_onboard.py` -- a scripted `OnboardUI` (copy the shape `tests/test_cli_onboard_commands.py:50-70` builds): `test_typed_key_is_probed_and_recorded` (A2), `test_env_key_is_used_and_not_written` (A4), `test_401_and_403_get_their_own_sentences_and_offer_reenter` (A5), `test_unreachable_names_the_url` (A6), `test_back_returns_back` (A7), `test_hint_names_memory_user_id_and_the_locator_id` (A28), `test_configured_reads_file_then_env` (A29's `configured()`).

`tests/test_everos_cloud_discover.py` -- `test_manifest_is_discovered_through_the_entry_point` (mirrors `tests/test_everos_plugin_discovery.py`), `test_package_imports_with_everos_and_raven_everos_blocked` (C3: iterate `pkgutil.walk_packages(raven_everos_cloud.__path__)` with `sys.modules["everos"] = None`, `sys.modules["raven_everos"] = None`), `test_init_is_import_cheap` (`httpx` not in `sys.modules` after a fresh `import raven_everos_cloud` in a subprocess).

- [ ] **Step 3: implement** `backend.py` (copy `convert_messages`, `as_ms_epoch`, `_owner_override`, `_search_data_to_memories`, `_flatten_profile`, `_cap_profile_text`, `_flatten_profile_list`, `_PATH_SAFE_ID_RE` warning from `plugins-dist/everos-memory/raven_everos/backend.py`; write `EverosCloudBackend` per the spec sketch; `_search_data_to_memories` takes parsed JSON dicts, so adapt its `getattr` reads to `.get`) and `onboard.py` (the spec sketch verbatim, `_probe` building a backend on the candidate key with the test's `client_factory` when given).

- [ ] **Step 4: run and prove red**

Run: `uv run --frozen pytest tests/test_everos_cloud_backend.py tests/test_everos_cloud_backend_contract.py tests/test_everos_cloud_onboard.py tests/test_everos_cloud_discover.py -q -p no:cacheprovider`
Expected: all pass; the contract file collects `11 tests` (`uv run --frozen pytest --collect-only -q tests/test_everos_cloud_backend_contract.py | tail -1` -> `11 tests collected`, the same as the local plugin's).
Mutations (each: change, run the named test, see it fail on the named assertion, restore, `git diff` clean):
  - drop `"mode": "agent"` from the add body -> `test_store_sends_mode_agent_...` fails on the body assertion;
  - flush after every add -> `test_store_sends_mode_agent_and_owner_ids_and_no_flush` fails on the flush count;
  - read only `user_id` in `_owner_override` -> `test_camel_case_owner_override_also_fires` fails;
  - write the env key into the slice in `onboard.py` -> `test_env_key_is_used_and_not_written` fails;
  - `import raven_everos` at the top of `backend.py` -> `test_package_imports_with_everos_and_raven_everos_blocked` fails.

- [ ] **Step 5: record** -- `# ponytail:` on the oversize-message ceiling (`ADD_MAX_BYTES`: a single message over it is not stored; upgrade: chunk one message) goes into `deviations.md` as a fallback-tier row at G4.

---

### Task 3: gates that must see the new plugin (H9, H10, H12)

**Delivers**: A30, A31.

**Files**:
- modify: `tests/test_plugin_boundary.py:20-22` (`PLUGIN_DIR`, `_PLUGIN_IMPORT`), `:50` (`_plugin_files`), `:55` (`test_scan_roots_exist`)
- modify: `scripts/coverage_gate.py:26`; `Makefile:13` and `:18` (`--cov=raven_everos` -> add `--cov=raven_everos_cloud`)
- modify: `.github/workflows/release.yml:85-90` (one `uv build --wheel plugins-dist/everos-cloud-memory -o dist` line after `:85`)

**Interfaces**: none produced; consumes the package name `raven_everos_cloud`.

- [ ] **Step 1: boundary test** -- `PLUGIN_DIRS = sorted((REPO_ROOT / "plugins-dist").glob("*/raven_*"))`; `_plugin_files` walks all of them; `_PLUGIN_IMPORT = re.compile(r"^\s*(from|import)\s+raven_everos(_cloud)?\b", re.M)`; `test_scan_roots_exist` asserts each dir yields files.

Run: `uv run --frozen pytest tests/test_plugin_boundary.py -q -p no:cacheprovider`
Expected: `4 passed` (same count as the baseline; the widened scan must still pass -- `raven_ppt` and `raven_design` do not import the host's private modules).
Mutation: add `import raven_everos_cloud` to `raven/core/plugin_stack.py` -> `test_host_does_not_import_plugin_internals` fails naming that file; restore.

- [ ] **Step 2: coverage and release**

```bash
cd /Users/admin/Raven-b
# scripts/coverage_gate.py: add ":(glob)plugins-dist/everos-cloud-memory/**/*.py" after line 26
# Makefile:13 and :18: --cov=raven_everos --cov=raven_everos_cloud
# release.yml: after line 85 add
#           uv build --wheel plugins-dist/everos-cloud-memory -o dist
grep -c "uv build --wheel plugins-dist/" .github/workflows/release.yml   # 4
grep -c -- "-eq 3" .github/workflows/release.yml                          # 1
uv run --frozen pytest tests/test_release_plugin_list.py -q -p no:cacheprovider   # 6 passed
uv build --wheel plugins-dist/everos-cloud-memory -o $SCRATCH/dist && ls $SCRATCH/dist   # everos_cloud_memory-0.1.0-py3-none-any.whl
```

- [ ] **Step 3: record** -- nothing expected.

---

### Task 4: the memory backend chooser (H1)

**Delivers**: A1, A3, A8, A9, A29, A38 (contract level; A1/A3/A8/A9/A29/A38 real-host halves in Task 10).

**Files**:
- modify: `raven/cli/onboard_commands.py:2010-2026` (`_step4_memory`'s loop) and a new `_choose_memory_backend(steps) -> str | None` beside `_memory_steps` (`:1929`)
- modify: `tests/test_cli_onboard_commands.py` (append; AGENTS 5.4: no new file)

**Interfaces**:
- consumes `_memory_steps()`, `_selected_backend()`, `_require_questionary()`, `RAVEN_STYLE`, `_QMARK`, `t`, `set_memory_backend`.
- produces `_choose_memory_backend(steps: list[tuple[str, OnboardStep]]) -> str | None`: `questionary.select(t("Which memory backend?"), choices=[Choice(name, value=name) for name in names] + [Choice(t("Off"), value=_MEMORY_OFF)], default=current if current in names else None, style=RAVEN_STYLE, qmark=_QMARK).ask()`; `None` from `.ask()` (Esc / Ctrl-C) -> `raise typer.Exit(1)`; `_MEMORY_OFF` -> return `None`; else the name. `_MEMORY_OFF = object()` module constant.

- [ ] **Step 1: tests first** (append to `tests/test_cli_onboard_commands.py`; the file already stubs `questionary.select` at `:306-319` -- copy that pattern):
`test_two_memory_screens_ask_which_backend_and_run_only_the_chosen_one` (A1: fake steps `[("everos", s1), ("everos-cloud", s2)]`, select returns `"everos-cloud"`, only `s2.run` called, `set_memory_backend("everos-cloud")`), `test_choosing_off_writes_null_and_runs_no_screen` (A3), `test_one_memory_screen_runs_without_a_question` (A8: `questionary.select` patched to raise if called), `test_a_cancelled_choice_exits_one_and_writes_nothing` (A38: select returns `None` -> `typer.Exit` with code 1, `set_memory_backend` not called), `test_non_interactive_with_two_screens_never_asks` (A29).

- [ ] **Step 2: implement** per the spec's "Host: the chooser (H1), before and after" block.

- [ ] **Step 3: run and prove red**

Run: `uv run --frozen pytest tests/test_cli_onboard_commands.py -q -p no:cacheprovider`
Expected: `353 passed` (348 + 5; if `test_onboard_cli_writes_the_keys_passed_as_flags` is red, rerun it alone per D1 and record both outputs).
Mutation: make `_choose_memory_backend` return `None` on `.ask() is None` instead of raising -> `test_a_cancelled_choice_exits_one_and_writes_nothing` fails; restore.
Edit check for A9: `git -C /Users/admin/Raven-b diff 3632e6040 -- tests/test_cli_onboard_commands.py` changes only `test_step4_memory_first_configured_wins`, `test_step4_memory_all_disabled_clears_backend` and `test_step4_memory_back_returns_sentinel_without_writing` (they pinned the retired run-every-screen behaviour with two fake plugins; deviations.md D2) and adds tests; no other existing test body changes.

- [ ] **Step 4: record** -- nothing expected.

---

### Task 5: the memory page reads the configured backend (H2-H5)

**Delivers**: A19, A20, A21, A36, A37 (contract level; real-host halves in Task 10).

**Files**:
- modify: `raven/rpc/methods/memory.py:39` (`_EVEROS_BACKEND` -> `_WIRE_BACKENDS`, `_CLOUD_BACKEND`), `:55-69` (`_cfg`), `:72-78` (`_post`), `:81-102` (`_search_tuning`), `:171-195` (`_unavailable_note`), plus a new `_auth_headers()`
- modify: `tests/test_rpc_memory.py` (append only)

**Interfaces**:
- consumes `raven_everos_cloud.backend.{DEFAULT_BASE_URL, resolve_api_key}` (Task 2); `raven.config.raven.load_raven_config`.
- produces `_cfg() -> tuple[str, str, str]` (shape unchanged; slice by backend), `_auth_headers() -> dict[str, str]`, `_post(base_url, path, body)` (signature unchanged; passes `headers=_auth_headers()`), `_search_tuning(base_url, kind)` (returns `{}` + `include_profile` for the cloud backend without probing), `_unavailable_note()` (per-backend `find_spec`; names the missing distribution).

- [ ] **Step 1: tests first** (append; the existing `fake_cfg` fixture at `:19-23` monkeypatches `_cfg` with a 3-tuple and must keep working):
`test_cloud_backend_adds_bearer_header_and_skips_health` (monkeypatch `load_raven_config` to a cloud config with a key; capture the headers `httpx.AsyncClient.post` receives via a `MockTransport`; assert `Authorization: Bearer ...` and no `/health` request), `test_everos_backend_sends_no_header` (A21), `test_stats_say_ok_false_on_401_and_list_carries_the_server_message` (A37), `test_missing_cloud_distribution_note_names_it` (A36: `find_spec` patched to `None` for `raven_everos_cloud`), `test_empty_store_is_ok_true_with_zeros` (A20).

- [ ] **Step 2: implement** per the spec's refined H2 block (`_cfg` keeps three values; `_auth_headers` is new).

- [ ] **Step 3: run and prove red**

Run: `uv run --frozen pytest tests/test_rpc_memory.py -q -p no:cacheprovider`
Expected: `22 passed` (17 + 5).
Zero-edit check for A21: `git -C /Users/admin/Raven-b diff 3632e6040 -- tests/test_rpc_memory.py | grep '^-' | grep -v '^---'` prints nothing (no line removed or changed).
Mutation: have `_auth_headers` return `{}` for the cloud backend -> `test_cloud_backend_adds_bearer_header_and_skips_health` fails; restore.

- [ ] **Step 4: record** -- nothing expected.

---

### Task 6: settings RPC -- the reason flag, the slice route, the cloud status, the restart guard (H6, H7, H8, H15)

**Delivers**: A23, A25, A35, A39 (contract level), the RPC half of A22 and A24.

**Files**:
- modify: `raven/rpc/methods/console.py:1243-1264` (`settings_everos`), `:630-725` (`settings_set`, new arm before the final raise), `:1362-1398` (`everos_follows_provider`), `:1426-` (`settings_everos_set`), new `settings_everos_cloud`, registration beside `:2438`
- modify: `rpc-schema/openrpc.json` (method `settings.everos` result gains `reason`; new method `settings.everosCloud`)
- modify: `raven/rpc/models.py:3761` (`SettingsEverosResult.reason`), new `SettingsEverosCloudParams` / `SettingsEverosCloudResult`, `METHOD_MODELS` at `:5444`
- modify: `tests/test_rpc_settings.py` (append only)

**Interfaces**:
- produces RPC `settings.everosCloud` (params `{}`) -> `{"available": bool, "selected": bool, "api_key_set": bool, "key_source": "file" | "env" | null, "base_url": str, "status": "ok" | "degraded" | "missing" | null, "hint": str | null}`; `settings.everos` result gains `reason: "other_backend" | null`.
- produces `settings.set` arm for `plugins.config.<plugin_id>.<field>`: `_plugin_config_declaration(plugin_id) -> dict | None` reads `registry.manifest_for(plugin_id).config_schema` from `build_plugin_registry(load_raven_config())`; `_TYPE_FOR = {"string": str, "boolean": bool, "integer": int, "object": dict}`; write through `set_plugin_config_fields`; returns `{"applied": True, "previous": None}`.
- `everos_follows_provider` and `settings_everos_set` read `load_raven_config().memory.backend` first (spec H15).

- [ ] **Step 1: contract files first** -- add the `reason` property and the new method to `rpc-schema/openrpc.json` (copy the `settings.everos` entry at `:4851` as the shape), mirror in `models.py`, register in `METHOD_MODELS`.

Run: `uv run --frozen pytest tests/test_rpc_schema_match.py tests/test_rpc_registration.py -q -p no:cacheprovider`
Expected: `test_rpc_registration` red until the handler is registered in Step 3 (that red is the point: the contract names a method nobody serves yet); `test_rpc_schema_match` green.

- [ ] **Step 2: tests first** (append to `tests/test_rpc_settings.py`, reusing its `everos_cfg` fixture):
`test_everos_roles_answer_other_backend_for_the_cloud` (A23: `available False`, `reason "other_backend"`, note names `everos-cloud`), `test_everos_roles_unchanged_for_everos_and_none` (A23/C9: compare with the dict the base worktree produced -- Task 10 records it; here assert `available True` and `reason` absent/None), `test_stale_backend_name_also_hides_roles` (A39), `test_settings_set_writes_a_declared_plugin_field` (A24's write), `test_settings_set_refuses_undeclared_wrong_type_and_unknown_plugin` (A25), `test_everos_cloud_status_reports_key_source_and_health` (A22/A24's RPC: fake cloud `ok` -> `status ok`, `401` -> `missing` with the 401 hint, no key -> `api_key_set False`), `test_provider_save_does_not_restart_everos_for_the_cloud_backend` (A35: patch `_everos_applied` to record calls; `everos_follows_provider("openrouter", factory)` with backend `everos-cloud` -> not called; with `everos` -> called when a pin names it), `test_role_write_is_refused_for_the_cloud_backend` (A35).

- [ ] **Step 3: implement** -- H6 (reason), H7 (arm), H8 (`settings_everos_cloud`: build `PluginContext` from the slice and `ServiceLocator(workspace, user_id, agent_id)`, `make_backend`, `await health()`, `await stop()`), H15 (guards), registration `dispatcher.register("settings.everosCloud", bind(settings_everos_cloud))`.

- [ ] **Step 4: run and prove red**

Run: `uv run --frozen pytest tests/test_rpc_settings.py tests/test_rpc_schema_match.py tests/test_rpc_registration.py -q -p no:cacheprovider`
Expected: `test_rpc_settings.py` `115 passed` (107 + 8); the other two green.
Zero-edit check for A26/A35: `git -C /Users/admin/Raven-b diff 3632e6040 -- tests/test_rpc_settings.py | grep '^-' | grep -v '^---'` prints nothing.
Mutations: remove the backend check from `everos_follows_provider` -> `test_provider_save_does_not_restart_everos_for_the_cloud_backend` fails; let the `settings.set` arm skip the declaration lookup -> `test_settings_set_refuses_undeclared_wrong_type_and_unknown_plugin` fails; restore both.

- [ ] **Step 5: record** -- nothing expected.

---

### Task 7: the settings page (H13)

**Delivers**: A22, A24, A26 (page level; real-host halves in Task 10).

**Files**:
- regenerate: `ui-web/src/rpc/generated.ts` (`cd ui-web && npm run gen`), `ui-tui/src/rpc/generated.ts` (`cd ui-tui && npm run gen:rpc`), `ui-tui/src/i18n/messages.generated.ts` (`cd ui-tui && npm run gen:i18n`)
- modify: `i18n/messages.json` (`ui` section: `gui.settings.cloud.title`, `.status`, `.key`, `.endpoint`, `.restart_hint`, `.chip_connected`, `.chip_needs_key`, `.chip_rate_limited`, `.chip_probing` -- `en` and `zh` both, the catalogue requires both)
- modify: `ui-web/src/features/settings/types.ts:143` (`everosCloud: EverosCloudInfo | null` on `SettingsSnapshot`; `EverosCloudInfo = ResultOf<'settings.everosCloud'>`), `store.ts:146-149` (`everosCloud: null` in `emptySnap`), `source.ts:79-83` (`loadEverosCloud` beside `loadEveros`, both awaited where `loadEveros` is; snapshot field `everosCloud`), `providers/Roles.tsx:514-526` (filter) and the new `EverosCloudCard` (export) rendered by `Roles()` under the card
- modify: `ui-web/src/rpc/fixtures/settings.ts:211-215` (a `settings.everosCloud` fixture), `ui-web/src/test/settingsHarness.ts:87` (`everosCloud` seed)
- modify tests: `ui-web/src/features/settings/providers/Roles.test.tsx`, `ui-web/src/features/settings/source.test.ts` (append only)

**Interfaces**:
- consumes `settings.everosCloud` and the `reason` field (Task 6); `KeyRow` from `pages/Tools.tsx:53` (import it; it writes through `store.write(keyName, value)` which calls `settings.set`); `Card`, `Row`, `Rov`, `Chip`, `Spin` from `Fields.tsx`.
- produces `EverosCloudCard(): JSX.Element | null` as in the spec's ui-web block; `Roles()` maps `ROLES.filter((r) => !(r.everos && r.everos !== 'embedding' && s.snap.everos?.reason === 'other_backend'))` (spec H13; the embedding row stays for knowledge bases) -- keyed on the roles RPC's `reason`, not on the cloud card's `selected`, so a future third backend hides the rows too; one comment line says so.

- [ ] **Step 1: regenerate and seed**

```bash
cd /Users/admin/Raven-b/ui-web && npm run gen && npm run gen:check          # exit 0 after the regen
cd /Users/admin/Raven-b/ui-tui && npm run gen:rpc && npm run lint:rpc       # exit 0
grep -c "everosCloud" /Users/admin/Raven-b/ui-web/src/rpc/generated.ts      # >= 2 (params and result)
```

- [ ] **Step 2: tests first** (append):
`Roles.test.tsx`: `hides the four EverOS rows when the roles RPC says other_backend` (A22), `keeps the four rows and the not-installed pill when available is false without a reason` (A26 regression), `draws the cloud card only when everosCloud.selected` (A22), `the card's key row writes plugins.config.everos-cloud-memory.api_key` (A24: the harness records `set` calls).
`source.test.ts`: `loads settings.everosCloud beside settings.everos and seeds null on failure`.

- [ ] **Step 3: implement**, then strings:

```bash
cd /Users/admin/Raven-b && python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("i18n/messages.json"); d = json.loads(p.read_text())
d["ui"].update({
  "gui.settings.cloud.title": {"en": "Long-term memory: EverOS Cloud", "zh": "长期记忆：EverOS Cloud"},
  "gui.settings.cloud.status": {"en": "Status", "zh": "状态"},
  "gui.settings.cloud.key": {"en": "API key", "zh": "API 密钥"},
  "gui.settings.cloud.endpoint": {"en": "Endpoint", "zh": "服务地址"},
  "gui.settings.cloud.restart_hint": {"en": "A changed key is used by the running gateway after its next start.", "zh": "更换密钥后，运行中的网关下次启动才会使用新密钥。"},
  "gui.settings.cloud.chip_connected": {"en": "Connected", "zh": "已连接"},
  "gui.settings.cloud.chip_needs_key": {"en": "Needs a key", "zh": "需要密钥"},
  "gui.settings.cloud.chip_rate_limited": {"en": "Rate limited", "zh": "被限流"},
  "gui.settings.cloud.chip_probing": {"en": "Checking", "zh": "检查中"},
})
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
EOF
cd ui-tui && npm run gen:i18n && npm run lint:i18n
```
(`i18n/messages.json` already carries CJK, so the source-language gate's carrier pass admits the `zh` column -- AGENTS 1.3.)

- [ ] **Step 4: run and prove red**

```bash
cd /Users/admin/Raven-b/ui-web && npm test -- Roles source 2>&1 | tail -5     # all pass; record the counts
npm run lint && npm run gen:check && npx vitest run scripts/gates/first-frame-literals.test.mjs   # 5 passed on the base
cd /Users/admin/Raven-b && make build-ui 2>&1 | tail -3                      # the served page must carry the card
```
Mutation: remove the `.filter(...)` in `Roles()` -> `hides the four EverOS rows ...` fails; restore.

- [ ] **Step 5: record** -- nothing expected.

---

### Task 8: documentation and terms (H14)

**Delivers**: AGENTS section 6 for the new terms; the layout page; the plugin architecture record.

**Files**:
- modify: `docs/memory-plugin-architecture.md` (a section "EverOS Cloud as a second backend": the slice, the key order, the flush policy, the two host flags `reason` and the restart guard, the not-in-default-install decision)
- modify: `docs-site/docs/repo-layout.md:11` and `docs-site/docs/repo-layout.zh.md` (the `plugins-dist/` line names four distributions)
- modify: `CONTEXT.md` Memory section (the six terms of the spec's Terms table, verbatim definitions)

- [ ] **Step 1: write**; **Step 2**: `make check-source-language && make check-large-files` -> both exit 0 (the zh layout page is a `.md`, self-governing).

---

### Task 9: the live tests (C14-C18)

**Delivers**: A32.

**Files**:
- new: `tests/integration/test_everos_cloud_real_cloud.py`

**Interfaces**: six tests named exactly as C14-C18 name them; module-level `pytestmark = pytest.mark.skipif(not os.environ.get("EVEROS_CLOUD_API_KEY"), reason="EVEROS_CLOUD_API_KEY is not set; the live EverOS Cloud tests need a key")`; `test_add_without_flush_is_searchable_within_two_minutes` additionally `@pytest.mark.skipif(not os.environ.get("EVEROS_CLOUD_SLOW"), reason="costs two minutes; set EVEROS_CLOUD_SLOW=1 to run it by hand")`. Each test uses a fresh `session_id` / `user_id` pair (`ecm-live-<uuid12>`) so runs never read each other's memory, and prints the service's raw replies.

- [ ] **Step 1: write**; **Step 2**: `uv run --frozen pytest tests/integration/test_everos_cloud_real_cloud.py -rs -q -p no:cacheprovider` -> `6 skipped`, five reasons naming `EVEROS_CLOUD_API_KEY`, one naming `EVEROS_CLOUD_SLOW`; `grep -c "^def test_\|^async def test_" tests/integration/test_everos_cloud_real_cloud.py` -> `6`. **Step 3**: when the owner provides a key (`zsh -ic`), run with it and keep the raw output under the acceptance evidence as T32.2.

---

### Task 10: the acceptance run (stage 4 proper)

**Delivers**: every `real` case of the acceptance document, the ★ journeys with
screenshots, the G4 package.

- [ ] **Step 1**: before anything, `git -C /Users/admin/Raven-b branch --show-current` -> `feat/everos_cloud_memory_plugin`; run the acceptance document's section 1.1 sweep and build the isolated home (1.3); record the baselines (1.3 last bullet) on the detached base worktree.
- [ ] **Step 2**: run sections 2.1-2.5 in order; each case leaves its evidence directory; a red case is a finding, not a reason to touch the case (prove-it-works section 1).
- [ ] **Step 3**: the five ★ journeys once more with a screenshot per screen (`playwright-cli -s=ecm screenshot`, `tmux capture-pane -p`), filed under `acceptance/`.
- [ ] **Step 4**: gates the CI runs, locally, in the order the project memory lists: `make lint lint-types check-source-language check-large-files`, `cd ui-web && npx eslint .`, `make build-ui`, `make check-commits` on each new sha; then the G4 five-dimension review and the acceptance report page (`dev-workflow` section 5).
- [ ] **Step 5**: teardown per the acceptance document 1.5; `deviations.md` gains the fallback-tier rows for every `# ponytail:` comment (`/ponytail-debt`) and the assumption-mismatch rows for temporary resources.

---

## Self-review (done before dispatching the G3 reviewer)

1. Coverage: A1-A39 each appear in a task's Delivers (A1 A3 A8 A9 A29 A38 -> 4; A2 A4 A5 A6 A7 A27 A28 -> 2 + 10; A10-A18 A33 A34 -> 2 + 10; A19 A20 A21 A36 A37 -> 5 + 10; A22 A24 A26 -> 7 + 10; A23 A25 A35 A39 -> 6 + 10; A30 A31 -> 3; A32 -> 9). C1-C3 -> Task 3 and Task 2's discover tests; C4-C8, C10 -> Task 2; C9 -> Tasks 4-7 zero-edit checks; C11-C12 -> Tasks 7, 8; C13 -> Task 3; C14-C18 -> Task 9.
2. Placeholders: none of the patterns the plan template forbids; expected outputs that can be measured today are the measured numbers above; the rest are counts the step defines (N added tests) and are to be replaced by the actual line in `run.md`.
3. Names: `resolve_api_key`, `DEFAULT_BASE_URL`, `make_backend`, `_auth_headers`, `_choose_memory_backend`, `_MEMORY_OFF`, `settings_everos_cloud`, `settings.everosCloud`, `reason`, `EverosCloudCard`, `everosCloud` are spelled the same in every task that uses them.
4. Ponytail ladder per task: Task 1 exists because every later test needs a counterparty and no real one is reachable; Task 2 copies rather than re-derives; Task 3 is three one-line edits; Task 4 is the smallest change that keeps one plugin's path identical; Task 5 keeps `_cfg`'s shape to leave 17 tests untouched; Task 6 adds one generic arm instead of a per-plugin setter; Task 7 reuses `KeyRow`, `Card`, `Chip`; Task 8 and 9 are the minimum the spec promises.
5. Authorization and caps are in the header.
6. `deviations.md` exists with D1.
