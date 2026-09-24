# EverOS role configuration - implementation plan

> To whoever executes this: work it as `dev-workflow` stage 4; tick steps with `- [ ]`.
> For anything this document does not cover, read Global Constraints first, then
> `unattended-run` section 7's three tiers. Do not guess.

**Goal**: changing an EverOS role model anywhere -- settings page or wizard -- is stored in
raven's own config, delivered to the server as environment variables, and in force within
seconds, with the server's own file untouched.

**Design document**: `docs/specs/2026-09-21-everos-role-config-design.md` (acceptance items
A1..A22 live there).

**Acceptance cases**: `docs/specs/2026-09-21-everos-role-config-acceptance.md` (T1.1..T6.5).

**Approach**: raven stores `{model, provider}` per role in `plugins.config['everos-memory']`,
resolves it to an address and a key at spawn time, and emits all four roles as
`EVEROS_<SECTION>__*` -- held ones with values, unheld ones empty, which suppresses the file.
A single `restart_for_config_change()` applies a save; the wizard awaits it, the RPC
backgrounds it and reports through `memory.health`. A versioned migration carries existing
`everos.toml` values across, creating a provider row where raven has none.

**Stack**: python 3.12, pydantic v2 / pydantic-settings (everos's own), `uv` for everything.
Frontend: the existing `ui-web` React island, `npm run gen` for the RPC client.

**Change map**: the table under "Change map" in the design document. Task numbers map onto
its rows: Store = T2, Deliver = T3, In-process = T10, Gate = T4, Read = T6, Write = T5,
Frontend = T7, Apply = T8, Migrate = T9. T1 (vendor table) feeds T3, T7 and T9; T11 is
cleanup.

## Global Constraints

Copied verbatim from the design document's constraint table. Every task inherits all of them.

- **C1** Ownership stays the plugin's judgement. Nothing under `raven/` decides, or re-derives, whether raven owns the root. -- check: `tests/test_rpc_settings.py` asserts `"everos_owned" not in Path("raven").rglob("*.py") read` and that `settings.everos` gets `owned` from the plugin's `describe_roles()`
- **C2** A role block names a model and a provider and holds no credential. -- check: `raven-plugin.toml` declares `llm` / `rerank` / `multimodal` as `object` in `[plugin.config_schema]` (otherwise every key logs "not in its config_schema"); a test asserts the written slice has no `api_key` / `base_url` key. A7 checks the file
- **C3** No new runtime dependency, in the host or the plugin. -- check: `git diff pyproject.toml plugins-dist/everos-memory/pyproject.toml uv.lock` adds nothing under `[project.dependencies]`
- **C4** The installed `everos` package is unchanged. -- check: `uv pip show everos` reports the same version before and after; no file under `site-packages/everos/` is modified (it is outside the repo, so `git diff` cannot say)
- **C5** The host forwards; the plugin owns the keys. `console.py` holds no role field-name constants and does not assemble the slice. -- check: `_EVEROS_FIELDS` and `_EVEROS_REQUIRED` are gone from `console.py`; it calls the plugin's `set_role()` / `describe_roles()` and nothing else
- **C6** Every subprocess raven spawns receives a filtered environment, so binding `EVEROS_*` in-process does not hand credentials to a model-run command. -- check: `raven/sandbox/direct_executor.py:_ENV_ALLOWLIST` (verified 2026-09-21: locale and runtime basics only, no `EVEROS_*`); the other spawn sites -- `agent/subagent/backends/cli_agent.py`, `agent/tools/media_gen.py`, `importer/scanners/hermes.py`, `utils/office.py` -- are enumerated and each either allowlists or is documented as inheriting
- **C7** raven keeps creating `everos.toml` from the template and keeps owning `[api]`; `_child_env()` keeps deleting every `EVEROS_API__*`. Where a server listens is the file's decision. -- check: A13
- **C8** raven writes none of `[llm]`, `[embedding]`, `[rerank]`, `[multimodal]` in `everos.toml`, and `set_everos_section` can no longer address them. -- check: `WRITABLE_SECTIONS == ("api",)`; a test asserts `set_everos_section("llm", ...)` raises `KeyError`; A7
- **C9** On a root raven owns, all four roles are emitted on every spawn -- held ones with values, unheld ones empty. -- check: A9
- **C10** The runtime knobs in `everos.toml` survive a model change. -- check: A8, and `.work_context/everos_role_config_moves_to_raven/spikes/env_merge.py`
- **C11** The spawn precheck runs before the running server is stopped, so a restart that cannot succeed leaves the old server up. -- check: A5
- **C12** Every restart outcome reaches the page, success included. -- check: A4, A6
- **C13** On a user-managed root raven neither writes the role config nor starts or stops the server. -- check: A12, A14
- **C14** Migration runs automatically on config load, and is the plugin's code -- `loader.py` schedules it and passes the raw config, it does not read `everos.toml` itself. -- check: A10; C1's test also covers the loader
- **C15** The gate `everos_role_configured(section)` reads raven's config and answers: a model, a provider, and that provider resolving to a usable credential -- the same rule `api_key_set` reports. -- check: A11, A18
- **C16** EverOS resolves settings as `init_args > env_vars > everos.toml > default.toml`. -- check: Verified 2026-09-21: `everos/config/settings.py` module docstring and `settings_customise_sources`
- **C17** Environment variables merge **per key**: sending `EVEROS_RERANK__MODEL` leaves `timeout_seconds` from the file intact. -- check: Verified 2026-09-21 by `spikes/env_merge.py` (re-runnable): `timeout_seconds=99.0`, `batch_size=77`, `provider='vllm'`, `max_concurrency=11` all survived
- **C18** An **empty** environment value suppresses the file's value, so a cleared role is really cleared. -- check: Verified 2026-09-21 by the same spike: with `EVEROS_RERANK__MODEL=""` the factory raises `Rerank model is not configured`, while `timeout_seconds=99.0` still survives
- **C19** The `memory.health` push reaches the settings page. -- check: Verified statically 2026-09-21: `organ_glue._emit_mcp_event` -> `rpc/bootstrap.py:_mcp_event` -> `send_frame`, documented there as "broadcast rather than conversation-scoped"; received by `ui-web/src/app/install.ts` (`NOTIFICATION_METHODS`, not the ACP `SIDE_CHANNEL_METHODS`) and rendered by `state/banner.ts`. A4 confirms it on a real host
- **C20** **Falsified.** The reverse lookup does *not* name a provider for every value raven wrote. -- check: Measured 2026-09-21 on the author's machine: `provider_serving_at` returns `None` for `[embedding]` (`api.deepinfra.com/v1/openai`) and `[rerank]` (`.../v1/inference`), because raven carries no `deepinfra` provider row at all. Two of four roles. The design accounts for this (see Migration) rather than assuming it away
- **C21** LiteLLM knows `deepinfra`, `vllm` and `dashscope`, so raven can create a real provider row for a vendor it carries no spec for. -- check: Verified 2026-09-21: `_litellm_knows` returns True for all three; `_provider_schema_cls` documents this path ("mistral and xai are supported that way")
- **C22** The wizard's vendor table is the correct mapping from a vendor to the rerank protocol and rerank base url. -- check: Verified 2026-09-21 for DeepInfra against this machine's wizard-written `[rerank]`. **`siliconflow` -> `vllm` and `dashscope` -> `dashscope` are unverified**; A15 covers one of them

### Authorisation for this run

Baseline in `unattended-run` section 1. This flow adds, pre-authorised: rebasing onto
`refactor/ui_web_architecture`; running real-host cases with real credentials; creating
temporary directories and processes (cleaned up at teardown and recorded in
`deviations.md`). Never pre-authorised: editing committed team-convention files
(`CLAUDE.md` / `AGENTS.md`).

`commit`: pre-authorised. `push`: pre-authorised while the user is away or a goal is
running; asked item by item while they are present (`dev-workflow` section 4).

Project-specific: AGENTS.md section 3.4 -- do not commit unprompted; section 4 -- `uv` only,
never `pip` or a hand-edited lockfile; section 5.4 -- when changing a CLI command update the
matching `test_cli_<module>_commands.py`, never a new file.

### Limits and goal

Rounds: 40. Time: 48 hours.

Goal text, to be used verbatim with `/goal`:

> Every real-host case in `docs/specs/2026-09-21-everos-role-config-acceptance.md` passes and
> its commands can be re-run as written; the PR is open; every blocker from a self-dispatched
> review is handled; every external review comment that has arrived is handled or recorded in
> `deviations.md`; **the user confirms acceptance in the conversation**. Or `deviations.md`
> gains a stop-tier entry. Or 40 rounds. Or 48 hours.

---

## Task 1: the vendor table moves out of the wizard

**Delivers**: the data source A15, A16 and A17 need. No acceptance item on its own.

**Files**:
- Modify: `plugins-dist/everos-memory/raven_everos/config.py` (append)
- Modify: `plugins-dist/everos-memory/raven_everos/onboard.py:361-404` (delete the literal, import instead)
- Test: `tests/test_everos_config.py`

**Interfaces**:
- Consumes: nothing.
- Produces:
  ```python
  VENDORS: tuple[dict[str, Any], ...]          # the rows, unchanged in content
  def vendor(name: str) -> dict[str, Any] | None
  def vendor_supports(name: str, role: str) -> bool
  def rerank_protocol(name: str) -> str | None   # "deepinfra" | "vllm" | "dashscope" | None
  def rerank_base_url(name: str, default: str) -> str
  ```

- [ ] **Step 1: move the literal**

Cut `_EVEROS_PROVIDERS` from `onboard.py` into `config.py` as `VENDORS`, content byte-identical.
In `onboard.py` replace the literal with `from raven_everos.config import VENDORS as _EVEROS_PROVIDERS`.

- [ ] **Step 2: write the accessors**

```python
def vendor(name: str) -> dict[str, Any] | None:
    """The vendor row for ``name``, or None for a vendor this table does not carry."""
    return next((v for v in VENDORS if v["name"] == name), None)


def vendor_supports(name: str, role: str) -> bool:
    """Whether this vendor can serve ``role`` at all.

    False for a vendor the table does not carry: offering a role a vendor cannot
    serve is how the rerank slot came to list OpenAI.
    """
    row = vendor(name)
    return bool(row and role in row.get("supports", ()))


def rerank_protocol(name: str) -> str | None:
    """Which client implementation EverOS must build for this vendor.

    EverOS's ``rerank.provider`` names a request shape, not a vendor -- DeepInfra
    posts to ``{base}/{model}`` while vLLM posts to ``{base}/rerank``. Returning
    None means "say nothing", which leaves whatever the file holds.
    """
    row = vendor(name)
    return row.get("rerank_provider") if row else None


def rerank_base_url(name: str, default: str) -> str:
    """The address rerank goes to, which is not always the chat address.

    DeepInfra serves rerank from ``/v1/inference`` and chat from ``/v1/openai``;
    borrowing the chat one is why rerank configured from the settings page has
    never worked against it.
    """
    row = vendor(name)
    return (row or {}).get("rerank_base_url") or default
```

- [ ] **Step 3: test**

```python
def test_vendor_table_knows_deepinfra_serves_rerank_from_its_own_address():
    assert vendor_supports("deepinfra", "rerank") is True
    assert rerank_protocol("deepinfra") == "deepinfra"
    assert rerank_base_url("deepinfra", "https://api.deepinfra.com/v1/openai") == (
        "https://api.deepinfra.com/v1/inference"
    )


def test_vendor_table_refuses_a_role_a_vendor_cannot_serve():
    assert vendor_supports("deepseek", "rerank") is False
    assert vendor_supports("openai", "rerank") is False


def test_unknown_vendor_says_nothing_rather_than_guessing():
    assert rerank_protocol("not-a-vendor") is None
    assert rerank_base_url("not-a-vendor", "https://x/v1") == "https://x/v1"
```

- [ ] **Step 4: run and prove it goes red**

Run: `uv run pytest tests/test_everos_config.py -q`
Expected: `(not yet run)`
Mutation: change `rerank_base_url` to return `default` unconditionally -> expect FAIL on
`test_vendor_table_knows_deepinfra_serves_rerank_from_its_own_address`'s third assertion.
Restore, then `git diff` clean.

- [ ] **Step 5: record**

`onboard.py` still owns `_probe_rerank`, which dispatches on the same protocol string. If it
turns out to disagree with `rerank_protocol`, that is an assumption mismatch -- record it.

---

## Task 2: role blocks in raven's config, and the resolver

**Delivers**: C2; the store A19 and A7 depend on.

**Files**:
- Modify: `plugins-dist/everos-memory/raven_everos/raven-plugin.toml:40-46` (config_schema)
- Modify: `plugins-dist/everos-memory/raven_everos/config.py` (append)
- Test: `tests/test_everos_config.py`

**Interfaces**:
- Consumes: Task 1's `rerank_base_url`.
- Produces:
  ```python
  ROLES: tuple[str, ...] = ("llm", "embedding", "rerank", "multimodal")

  @dataclass(frozen=True)
  class RoleEndpoint:
      model: str
      base_url: str
      api_key: str
      dimensions: int | None = None

  def role_pin(section: str) -> tuple[str, str] | None:      # (model, provider)
  def resolve_role(section: str) -> RoleEndpoint | None
  def set_role(section: str, *, model: str, provider: str) -> None   # raises EverosRootNotOwnedError
  def clear_role(section: str) -> None
  ```

  The writers live here rather than in Task 5 because the ownership guard belongs at the
  write primitive -- `_require_owned`'s own docstring says "enforced at the write primitives
  so a new caller cannot opt out", and this change was very nearly that new caller. Tasks 3,
  4 and 6 consume them in their tests.

- [ ] **Step 1: declare the blocks in the manifest**

```toml
[plugin.config_schema]
base_url = { type = "string" }
root     = { type = "string" }
owned    = { type = "boolean" }
port     = { type = "integer" }
agent_id = { type = "string" }
user_id  = { type = "string" }
llm        = { type = "object" }
rerank     = { type = "object" }
multimodal = { type = "object" }
```

Without this every new key logs "not in its config_schema; passing through"
(`raven/config/admission.py:108`); `object` is a supported type (`admission.py:31`).

- [ ] **Step 2: write the pin reader and the resolver**

```python
def role_pin(section: str) -> tuple[str, str] | None:
    """What raven records for ``section``: a model and the vendor that serves it.

    The embedding pin is raven's own top-level block rather than this plugin's
    slice, because knowledge bases embed with it too; the other three live in the
    slice. One function so callers do not have to know which is which.
    """


def resolve_role(section: str) -> RoleEndpoint | None:
    """The pin turned into an address and a key, or None when nothing is pinned.

    Resolved on every call rather than cached: a value written a second ago has to
    reach the next spawn without restarting raven. None -- not a raise -- because a
    role nobody configured is an ordinary state that the caller turns into "set this
    up first".

    Rerank asks the vendor table for its address: the endpoint that serves reranking
    is not always the one that serves chat.
    """


def set_role(section: str, *, model: str, provider: str) -> None:
    """Record what serves ``section``.

    **Embedding lands in raven's top-level block, the other three in this plugin's
    slice** -- the same split ``role_pin`` reads back. Writing all four into the
    slice would save fine and never take effect, because nothing reads embedding
    there.

    The guard is here, not at the caller: ``_require_owned`` sits at the write
    primitive so a new caller cannot opt out.
    """
    if section not in ROLES:
        raise KeyError(f"unknown everos role {section!r}; roles: {ROLES}")
    _require_owned(f"configure the {section} role")
    ...


def clear_role(section: str) -> None:
    """Forget what serves ``section``; the next spawn emits it empty."""
```

- [ ] **Step 3: test, including the shape C2 forbids**

```python
def test_a_role_block_holds_no_credential(tmp_config):
    set_role("llm", model="m", provider="deepseek")
    slice_ = read_slice(tmp_config)["llm"]
    assert set(slice_) == {"model", "provider"}


def test_rerank_resolves_to_the_rerank_address_not_the_chat_one(tmp_config):
    set_role("rerank", model="BAAI/x", provider="deepinfra")
    assert resolve_role("rerank").base_url.endswith("/v1/inference")


def test_an_unpinned_role_resolves_to_none(tmp_config):
    assert resolve_role("multimodal") is None


def test_embedding_is_written_where_it_is_read_from(tmp_config):
    set_role("embedding", model="m", provider="openrouter")
    raw = read_raw(tmp_config)
    assert raw["embedding"] == {"model": "m", "provider": "openrouter"}
    assert "embedding" not in raw["plugins"]["config"]["everos-memory"]


def test_a_write_to_a_root_raven_does_not_own_is_refused_by_name(unowned_root):
    with pytest.raises(EverosRootNotOwnedError) as e:
        set_role("llm", model="m", provider="deepseek")
    assert str(unowned_root) in str(e.value)
```

- [ ] **Step 4: run and prove it goes red**

Run: `uv run pytest tests/test_everos_config.py -q`
Expected: `(not yet run)`
Mutation 1: make `resolve_role` ignore `rerank_base_url` and always use the provider's own
address -> expect FAIL on `test_rerank_resolves_to_the_rerank_address_not_the_chat_one`.
Mutation 2: make `set_role` write embedding into the plugin slice like the other three ->
expect FAIL on `test_embedding_is_written_where_it_is_read_from`'s second assertion.
Mutation 3: remove `_require_owned` -> expect FAIL on
`test_a_write_to_a_root_raven_does_not_own_is_refused_by_name` at `pytest.raises`.
Restore after each, `git diff` clean.

- [ ] **Step 5: record**

If `resolve_provider_credentials` returns None for a provider the page offered, that is the
gate's problem, not this resolver's -- note it for Task 4 rather than papering over it here.

---

## Task 3: `everos_env()` and the spawn wiring

**Delivers**: A8, A9, A13, A16, A19, C9, C10.

**Files**:
- Modify: `plugins-dist/everos-memory/raven_everos/config.py` (append)
- Modify: `plugins-dist/everos-memory/raven_everos/server.py:673-709` (`_child_env`; the two lines to change are 708-709)
- Test: `tests/test_everos_server.py`

**Interfaces**:
- Consumes: Task 2's `resolve_role`, `ROLES`; Task 1's `rerank_protocol`.
- Produces: `def everos_env() -> dict[str, str]`

- [ ] **Step 1: write it**

```python
def everos_env() -> dict[str, str]:
    """The binding all four roles earn, from raven's own config.

    Every role is emitted on every spawn. A role raven does not hold is emitted
    **empty**, which suppresses whatever the file says -- that is what makes
    clearing a role in the UI mean anything now that raven no longer edits that
    file, and what stops an upgraded install's stale section from reviving.

    Only the model and credential keys are sent, so the runtime knobs in the file
    (timeouts, batch sizes, the multimodal file-uri allowlist) keep applying:
    EverOS merges per key, not per section.
    """
    env: dict[str, str] = {}
    for section in ROLES:
        prefix = f"EVEROS_{section.upper()}__"
        ep = resolve_role(section)
        env[f"{prefix}MODEL"] = ep.model if ep else ""
        env[f"{prefix}BASE_URL"] = ep.base_url if ep else ""
        env[f"{prefix}API_KEY"] = ep.api_key if ep else ""
        if section == "embedding":
            env[f"{prefix}DIMENSIONS"] = str(ep.dimensions) if ep and ep.dimensions else ""
        if section == "rerank":
            pin = role_pin("rerank")
            proto = rerank_protocol(pin[1]) if pin else None
            if proto:
                env[f"{prefix}PROVIDER"] = proto
    return env
```

`rerank`'s protocol is the one key deliberately **not** emitted empty: saying nothing leaves
the file's value, and there is no sensible empty protocol.

- [ ] **Step 2: rewire `_child_env`**

Replace `env.update(host_embedding_env())` with `env.update(everos_env())`. Leave the
`EVEROS_API__*` deletion exactly as it is (C7).

- [ ] **Step 3: test**

```python
def test_every_role_is_emitted_even_when_raven_holds_none(tmp_config):
    env = everos_env()
    for s in ("LLM", "EMBEDDING", "RERANK", "MULTIMODAL"):
        assert env[f"EVEROS_{s}__MODEL"] == ""


def test_an_unheld_role_is_emitted_empty_rather_than_omitted(tmp_config):
    set_role("llm", model="m", provider="deepseek")
    env = everos_env()
    assert env["EVEROS_LLM__MODEL"] == "m"
    assert "EVEROS_RERANK__MODEL" in env and env["EVEROS_RERANK__MODEL"] == ""


def test_child_env_still_strips_the_api_section(monkeypatch):
    monkeypatch.setenv("EVEROS_API__PORT", "19999")
    assert "EVEROS_API__PORT" not in _child_env()
```

- [ ] **Step 4: run and prove it goes red**

Run: `uv run pytest tests/test_everos_server.py -q`
Expected: `(not yet run)`
Mutation: make `everos_env` skip a role it does not hold (the shape the design rejected)
-> expect FAIL on `test_an_unheld_role_is_emitted_empty_rather_than_omitted` at the
`in env` assertion. Restore, `git diff` clean.

- [ ] **Step 5: record**

Re-run the spike after this lands: `/Users/admin/Raven/.venv/bin/python
.work_context/everos_role_config_moves_to_raven/spikes/env_merge.py`. If C17 or C18 no
longer holds against the installed everos version, that is a stop-tier entry -- the design
rests on both.

---

## Task 4: the gate changes its source

**Delivers**: C15; the rule A18 reports.

**Files**:
- Modify: `plugins-dist/everos-memory/raven_everos/config.py` (`everos_role_configured`)
- Modify: `plugins-dist/everos-memory/raven_everos/server.py:246` (the error text)
- Test: `tests/test_everos_config.py`, `tests/test_everos_backend.py`

**Interfaces**:
- Consumes: Task 2's `role_pin` and `resolve_role`.
- Produces: `everos_role_configured(section: str) -> bool`, same name and signature, new source.

Nine call sites share it and need no edit: `server.py:243`, `onboard.py:40` (wrapping `:58`,
`:1022`, `:1143`, `:1717`, `:1878`), `backend.py:939`, `backend.py:1012`.

- [ ] **Step 1: change the body**

```python
def everos_role_configured(section: str) -> bool:
    """Whether this install has ``section`` set up well enough to use.

    Three things, not two: a model, a vendor, and that vendor resolving to a usable
    credential. The old reading -- model AND api_key in the file -- cannot be
    computed now that raven's blocks hold no key, and "a model pinned to a vendor
    with no key" is exactly the state that would spawn a server doomed to die in its
    lifespan.

    ``llm`` gates long-term memory outright, so a wrong answer here is memory
    silently switched off. That is why the rule is the same one ``api_key_set``
    reports: one rule, two readers, no drift.
    """
    return resolve_role(section) is not None
```

- [ ] **Step 2: fix the error text it feeds**

`server.py:246` names `everos.toml`, which stops being where that value lives:

```python
raise EverosNotConfiguredError(
    "EverOS memory LLM is not configured: pick a model and a provider for it in "
    "settings, or run `raven onboard`."
)
```

- [ ] **Step 3: test the three-way rule**

```python
def test_a_model_without_a_provider_is_not_configured(tmp_config):
    set_role("llm", model="m", provider="")
    assert everos_role_configured("llm") is False


def test_a_provider_with_no_usable_credential_is_not_configured(tmp_config):
    set_role("llm", model="m", provider="a-provider-with-no-key")
    assert everos_role_configured("llm") is False


def test_the_gate_and_the_page_report_the_same_rule(tmp_config):
    set_role("llm", model="m", provider="deepseek")
    assert everos_role_configured("llm") is describe_roles()["sections"]["llm"]["api_key_set"]
```

- [ ] **Step 4: run and prove it goes red**

Run: `uv run pytest tests/test_everos_config.py tests/test_everos_backend.py -q`
Expected: `(not yet run)`
Mutation: make the gate return True whenever a model is set -> expect FAIL on
`test_a_provider_with_no_usable_credential_is_not_configured`. Restore, `git diff` clean.

- [ ] **Step 5: record**

`raven doctor` calls this eight times and advertises itself as fast. Time it
(`time uv run raven doctor`) before and after; if the provider resolution makes it visibly
slower, record a fallback-tier entry and memoise per process.

---

## Task 5: the host forwards, and the file loses the roles

**Delivers**: A7, A20, C5, C8.

**Files**:
- Modify: `plugins-dist/everos-memory/raven_everos/config.py` (`WRITABLE_SECTIONS`)
- Modify: `raven/rpc/methods/console.py:1213-1214, 1305-1430`
- Modify: `raven/rpc/models.py:3397` (`SettingsEverosSetParams.borrow_from`)
- Modify: `rpc-schema/openrpc.json:4841-4940` (both `settings.everosSet` and the
  `settings.everos_set` alias carry a `borrow_from` parameter)
- Regenerate: `ui-web/src/rpc/generated.ts` **and `ui-tui/src/rpc/generated.ts`** -- the TUI
  generates the same params type (`:4342`), and `lint:rpc` fails on a stale one. The previous
  PR was sent back by CI for regenerating only the web client
- Modify: `ui-web/src/features/settings/source.ts:223-232`, `types.ts:163-167`
- Modify: `ui-web/src/features/settings/providers/Roles.tsx:150`, `:169`
- Modify: `ui-web/src/test/settingsHarness.ts:155` (the `everosSet` stub's signature)
- Test: `tests/test_rpc_settings.py` (about ten cases pass `fields` / `borrow_from`),
  `tests/test_rpc_contract_shapes.py:192-199`, `tests/test_everos_config.py`,
  `ui-web/src/features/settings/providers/Roles.test.tsx:112-118`

**Interfaces**:
- Consumes: Task 2's `ROLES`, `set_role`, `clear_role`.
- Produces: the RPC's new parameter shape, consumed by Task 7:
  ```
  settings.everosSet  { section, model, provider }  |  { section, clear: true }
  ```

- [ ] **Step 1: shrink what the file can address**

```python
WRITABLE_SECTIONS = ("api",)
```

- [ ] **Step 2: decide each of the four behaviours `settings_everos_set` carries today**

The current handler is not a forwarder, and "forward and nothing else" would silently drop
working behaviour. Each of these gets an answer, not an omission:

| Today | After | Why |
|---|---|---|
| `borrow_from` copies the lender's key into `everos.toml` | **Gone; the provider name replaces it.** The parameter becomes `provider`. | `lend_provider_credentials`'s own docstring says it copies because "this file is read by EverOS as well as by raven, and a field only raven resolves is a field EverOS reads as an endpoint it cannot reach". That premise is exactly what this change removes: raven's config is not read by EverOS any more. The docstring even names this alternative and its benefit -- it "would follow a later key change on its own", which is A19. |
| `embedding_is_env_managed()` refuses a save the exported variables would outrank | **Kept, unchanged.** | A design non-goal says so in as many words. |
| `_EVEROS_REQUIRED = ("llm", "embedding")` forbids clearing those two | **Kept**, moved into the plugin beside `ROLES`. | Clearing llm turns long-term memory off; that should stay a deliberate act, not a stray click. |
| Saving an llm role records `memory.backend` when absent | **Kept, unchanged.** | It is what makes a web-only setup select the backend at all. |

- [ ] **Step 3: the host forwards**

Delete `_EVEROS_FIELDS` and `_EVEROS_REQUIRED` from `console.py`. `settings_everos_set`
becomes: check `embedding_is_env_managed`, refuse a clear of a required role, call
`set_role` / `clear_role`, record `memory.backend` for llm, hand off to Task 8's restart,
return.

- [ ] **Step 4: follow the parameter rename through the frontend**

`source.ts:223-232` sends `{section, model, provider}` instead of `{section, fields,
borrow_from}`; the comment there ("the server copies it across") stops being true and says
instead that the name is stored and resolved at spawn. `types.ts:163-167` drops the
`fields` / `borrowFrom` signature. `Roles.tsx:150` calls `src.setRole(r.everos, model,
provider)`. `Roles.test.tsx:112-118` asserts the new call shape.

- [ ] **Step 5: test**

```python
def test_the_file_can_no_longer_address_a_role_section():
    with pytest.raises(KeyError):
        set_everos_section("llm", {"model": "m"})


def test_a_required_role_still_cannot_be_cleared():
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_everos_set({"section": "llm", "clear": True})


def test_an_env_managed_embedding_is_still_refused(env_managed):
    with pytest.raises(ConfigValidationError) as e:
        await rpc_console.settings_everos_set({"section": "embedding", "model": "m", "provider": "p"})
    assert "EVEROS_EMBEDDING__MODEL" in str(e.value)


def test_nothing_under_raven_decides_ownership():
    hits = [p for p in Path("raven").rglob("*.py")
            if "everos_owned" in p.read_text(encoding="utf-8")]
    assert hits == []
```

- [ ] **Step 6: run and prove it goes red**

Run: `uv run pytest tests/test_rpc_settings.py tests/test_everos_config.py tests/test_rpc_contract_shapes.py -q`,
`npm --prefix ui-web test -- Roles`, `npm --prefix ui-web run gen:check`,
`npm --prefix ui-tui run lint:rpc`
Expected: `(not yet run)`
Mutation: drop the `embedding_is_env_managed` check -> expect FAIL on
`test_an_env_managed_embedding_is_still_refused`. Restore, `git diff` clean.

---

## Task 6: the read contract

**Delivers**: A18, C1's second half.

**Files**:
- Modify: `plugins-dist/everos-memory/raven_everos/config.py` (`describe_roles`)
- Modify: `raven/rpc/methods/console.py:1235-1302` (`settings_everos`)
- Modify: `rpc-schema/openrpc.json`, then regenerate `ui-web/src/rpc/generated.ts`
- Test: `tests/test_rpc_settings.py`, `tests/test_rpc_contract_shapes.py`

**Interfaces**:
- Consumes: Task 2's `role_pin`, Task 4's gate rule.
- Produces:
  ```python
  def describe_roles() -> dict[str, Any]
  # {"owned": bool, "available": bool, "config_path": str,
  #  "sections": {"<role>": {"model": str, "provider": str, "api_key_set": bool}},
  #  "supports": {"<provider>": ["llm", "rerank", ...]}}
  ```

  `supports` comes from Task 1's `vendor_supports`, one entry per configured provider. It
  rides along rather than getting a call of its own: the page needs it at exactly the moment
  it renders these slots, and a second round trip would let the two answers disagree.

`provider` is now the vendor. EverOS's rerank protocol is **not** returned: it is derived
from the vendor table, not chosen by anyone, so nothing on the wire needs it.

- [ ] **Step 1: write `describe_roles`**, reading pins, asking Task 4's rule for `api_key_set`,
and building `supports` from Task 1's `vendor_supports` over the configured providers.

- [ ] **Step 2: make the RPC a forwarder.** `settings_everos` returns `describe_roles()`.

- [ ] **Step 3: regenerate the client**

Run: `npm --prefix ui-web run gen`
Expected: `(not yet run)`
Then: `npm --prefix ui-web run gen:check` must pass -- CI fails on a stale file.

- [ ] **Step 4: test**

```python
async def test_the_response_no_longer_carries_a_request_shape_as_provider(seeded):
    out = await rpc_console.settings_everos({})
    assert out["sections"]["rerank"]["provider"] == "deepinfra"       # the vendor
    assert "vllm" not in json.dumps(out)                              # not the protocol


async def test_api_key_set_follows_the_provider_credential(seeded):
    assert (await rpc_console.settings_everos({}))["sections"]["llm"]["api_key_set"] is True
    remove_key("deepseek")
    assert (await rpc_console.settings_everos({}))["sections"]["llm"]["api_key_set"] is False


async def test_supports_says_which_roles_each_provider_can_serve(seeded):
    out = await rpc_console.settings_everos({})
    assert "rerank" in out["supports"]["deepinfra"]
    assert "rerank" not in out["supports"]["deepseek"]
```

- [ ] **Step 5: run and prove it goes red**

Run: `uv run pytest tests/test_rpc_settings.py tests/test_rpc_contract_shapes.py -q`
Expected: `(not yet run)`
Mutation: make `api_key_set` report `bool(model)` -> expect FAIL on
`test_api_key_set_follows_the_provider_credential`'s second assertion. Restore, `git diff` clean.

---

## Task 7: the role slots

**Delivers**: A14 (visible), A17, A18 (visible).

**Files**:
- Modify: `ui-web/src/features/settings/providers/Roles.tsx:97-103` (the reverse lookup), `:117` (the candidate filter), `:150` (the write call)
- Modify: `ui-web/src/features/settings/types.ts:58-70` (`EverosSection`, `EverosInfo`)
- Test: `ui-web/src/features/settings/providers/Roles.test.tsx`

**Interfaces**:
- Consumes: Task 6's `describe_roles` shape; the vendor table's `supports`, carried in the
  same response.
- Produces: nothing downstream.

- [ ] **Step 1: stop guessing the vendor**

```ts
if (r.everos) {
  const sec = snap.everos?.sections?.[r.everos]
  if (!sec || !sec.model) return null
  return { model: sec.model, provider: sec.provider ?? '' }
}
```

The reverse lookup at `:87-102` goes: the provider is stored now.

- [ ] **Step 2: filter candidates by capability, not only by key**

```ts
if (r.everos) return on.filter((p) => p.kind === 'key' && p.acceptsKey !== false
                                   && (snap.everos?.supports?.[p.id] ?? []).includes(r.everos))
```

- [ ] **Step 3: disable the slots on a root raven does not own**, with the sentence the
backend already uses for a foreign root: raven does not start or stop it, so its config is
edited in its own file.

- [ ] **Step 4: test**

```tsx
it('renders the stored provider for a role whose address matches no provider', ...)
it('offers only providers that can serve this role', ...)      // openai absent from rerank
it('disables the everos slots when the root is not ravens', ...)
```

- [ ] **Step 5: run and prove it goes red**

Run: `npm --prefix ui-web test -- Roles`
Expected: `(not yet run)`
Mutation: drop the `supports` clause from the filter -> expect FAIL on the second test.
Restore, `git diff` clean.

---

## Task 8: the restart

**Delivers**: A1, A2, A3, A4, A5, A6, A21, A22, C11, C12.

**Files**:
- Modify: `plugins-dist/everos-memory/raven_everos/server.py` (add, and delete `_may_spawn`)
- Modify: `plugins-dist/everos-memory/raven_everos/onboard.py:1155-1192` (`_stop_for_reload`)
- Modify: `plugins-dist/everos-memory/raven_everos/backend.py:212, 626-633` (delete)
- Modify: `raven/rpc/methods/console.py` (background it, emit `memory.health`)
- Test: `tests/test_everos_server.py`, `tests/test_rpc_settings.py`

**Interfaces**:
- Consumes: `lock_holder`, `stop_pid`, `StopOutcome`, `ensure_everos_server`, `_inotify_gate`,
  `_require_llm_configured` (the last two promoted to module-public `precheck_spawn()`).
- Produces:
  ```python
  def precheck_spawn() -> str | None            # None = a spawn could succeed
  def stop_for_reload(root) -> StopOutcome | None
  async def restart_for_config_change(root, base_url, *, on_result: Callable[[bool, str | None], None]) -> None
  ```

- [ ] **Step 1: promote the precheck and the stop**

`precheck_spawn()` returns the diagnosis or None; `stop_for_reload(root)` is
`lock_holder` + `stop_pid` with no UI. `onboard._stop_for_reload` keeps its printing and
calls them.

- [ ] **Step 2: write the chain**

```python
async def restart_for_config_change(root, base_url, *, on_result) -> None:
    """Apply a configuration that was just written.

    Order is load-bearing: the precheck runs before the stop, so a restart that
    cannot succeed leaves the old server serving. Stopped-and-not-started is the
    one genuinely bad state -- spawning happens only in ``EverosBackend.start()``,
    so nothing in this session would bring it back.

    A stop that does not reach STOPPED must not fall through to the spawn:
    ``ensure_everos_server`` would find the old server answering, adopt it, and
    report success for a configuration that never took -- this function's own bug,
    arriving by a different door.
    """
```

- [ ] **Step 3: delete what guards nothing**

`_may_spawn` has no production caller and `_SPAWNABLE_STATES` has no reader but it. Delete
both, and `tests/test_everos_backend.py:1377`.

- [ ] **Step 4: wire the RPC**

Background the chain; `on_result` emits `memory.health` with `{ok, error}` -- **including
`{ok: True, error: None}`**, because `install.ts` only clears the banner on a success frame.
An in-flight flag coalesces a second save into one more run. When `_safe_loop` yields no
loop there is nowhere to report, so refuse synchronously with `applied: false`.

- [ ] **Step 5: test the four outcomes**

```python
async def test_a_failing_precheck_leaves_the_old_server_running(fake_holder):
    ...
    assert stop_pid_calls == []


async def test_a_draining_stop_does_not_fall_through_to_the_spawn(draining):
    ...
    assert ensure_calls == []
    assert result == (False, "still finishing memory work")


async def test_success_pushes_ok_true_so_the_banner_can_clear(...):
    assert frames[-1] == ("memory.health", {"ok": True, "error": None})


async def test_no_loop_means_a_synchronous_refusal(no_loop):
    out = await rpc_console.settings_everos_set({"section": "llm", "model": "m", "provider": "p"})
    assert out["applied"] is False
```

- [ ] **Step 6: run and prove it goes red**

Run: `uv run pytest tests/test_everos_server.py tests/test_rpc_settings.py -q`
Expected: `(not yet run)`
Mutation: let the chain continue to step 4 on `STILL_DRAINING` -> expect FAIL on
`test_a_draining_stop_does_not_fall_through_to_the_spawn` at `ensure_calls == []`.
Restore, `git diff` clean.

---

## Task 9: migration

**Delivers**: A10, A11, A12, C14.

**Files**:
- Modify: `plugins-dist/everos-memory/raven_everos/config.py` (add `migrate_roles`)
- Modify: `raven/config/update_providers.py` (`provider_serving_at` widened)
- Modify: `raven/config/loader.py:1038-1039` (append one branch to the versioned chain; 1041 onward is unrelated)
- Modify: `raven/cli/doctor_commands.py:355-400` (keep as manual remedy, report overrides)
- Test: `tests/test_everos_config.py`, `tests/test_config_migrations.py`

**Interfaces**:
- Consumes: Task 1's vendor table, Task 2's `set_role`.
- Produces: `def migrate_roles(raw: dict) -> list[str]` -- mutates `raw`, returns notices.

- [ ] **Step 1: widen the reverse lookup**

```python
def provider_serving_at(base_url: str, *, api_key: str | None = None,
                        config_path: Path | None = None) -> str | None:
    """Which configured provider answers at ``base_url``, if any.

    Three levels, because one is not enough: the full address, then the host, then
    the key. The host level exists for DeepInfra, whose rerank section deliberately
    holds a different path from its chat endpoint, so a full-address comparison
    misses a vendor that is plainly the same one.
    """
```

- [ ] **Step 2: write the migration, in the plugin**

Per role: read the section, name the vendor (three levels), **create the provider row from
the vendor table when raven carries none** and move the key into it, then `set_role`. On a
miss, leave the role unset and return a notice. Skip entirely when the slice says the root
is not raven's. Idempotent: a second run changes nothing.

- [ ] **Step 3: schedule it from the host**

```python
if from_version < _EVEROS_ROLES_MIGRATION:
    _migrate_everos_roles(data, notify=True)
```

`_migrate_everos_roles` imports the plugin's `migrate_roles` and passes `data`. The host
does not read `everos.toml` and does not judge ownership (C1, C14).

- [ ] **Step 4: test on the measured shape**

```python
def test_a_vendor_raven_has_no_row_for_gets_one(legacy_config):
    migrate_roles(legacy_config)
    assert "deepinfra" in legacy_config["providers"]
    assert legacy_config["plugins"]["config"]["everos-memory"]["rerank"]["provider"] == "deepinfra"


def test_deepinfras_rerank_address_still_resolves_by_host(legacy_config):
    # the section holds /v1/inference while the provider serves /v1/openai
    assert provider_serving_at("https://api.deepinfra.com/v1/inference") == "deepinfra"


def test_running_it_twice_changes_nothing(legacy_config):
    migrate_roles(legacy_config)
    once = json.dumps(legacy_config, sort_keys=True)
    migrate_roles(legacy_config)
    assert json.dumps(legacy_config, sort_keys=True) == once


def test_an_unnameable_endpoint_leaves_the_role_unset_and_says_so(orphan_config):
    notices = migrate_roles(orphan_config)
    assert "llm" not in orphan_config["plugins"]["config"]["everos-memory"]
    assert any("llm" in n for n in notices)
```

- [ ] **Step 5: run and prove it goes red**

Run: `uv run pytest tests/test_everos_config.py tests/test_config_migrations.py -q`
Expected: `(not yet run)`
Mutation: drop the host level from `provider_serving_at` -> expect FAIL on
`test_deepinfras_rerank_address_still_resolves_by_host`. Restore, `git diff` clean.

- [ ] **Step 6: record**

Before writing the test fixture, dump the author's real shape once for reference (redacted)
so `legacy_config` matches reality rather than an imagined file.

---

## Task 10: `understand_media` keeps working

**Delivers**: the multimodal half of A2.

**Files**:
- Modify: `plugins-dist/everos-memory/raven_everos/backend.py:780-800` (`start`)
- Test: `tests/test_everos_backend.py`

**Interfaces**:
- Consumes: Task 3's `everos_env`.
- Produces: nothing.

- [ ] **Step 1: bind into raven's own process**

`EverosBackend.start()` already calls `configure_embedding_env`; it now calls
`os.environ.update(everos_env())` instead, so the in-process everos settings see all four
roles. `understand_media` reads them through everos's cached `load_settings()`.

- [ ] **Step 2: test**

```python
async def test_start_binds_all_four_roles_into_this_process(seeded, monkeypatch):
    await backend.start()
    assert os.environ["EVEROS_MULTIMODAL__MODEL"] == "the-pinned-model"
```

- [ ] **Step 3: run and prove it goes red**

Run: `uv run pytest tests/test_everos_backend.py -q`
Expected: `(not yet run)`
Mutation: bind only embedding, as today -> expect FAIL. Restore, `git diff` clean.

- [ ] **Step 4: record**

Nothing resets everos's `_multimodal_client` singleton, so a live change does not reach
`understand_media` -- a stated non-goal, not a deviation. If a reviewer reads it as a bug,
point at the non-goal rather than reaching into a third-party private global.

---

## Task 11: terms, stale text, and the suite

**Delivers**: closes C4's paperwork; no acceptance item of its own.

**Files**:
- Modify: `CONTEXT.md` (Memory section)
- Modify: `plugins-dist/everos-memory/raven_everos/config.py:1-10` (module docstring)

- [ ] **Step 1: the terms the spec introduced**

Add to `CONTEXT.md`: **EverOS role**, **role block**, **embedding pin** (with `_Avoid_:
Pinned, which is a Manifest marker), **rerank protocol** (with `_Avoid_: provider, which
names a vendor). AGENTS.md section 6 requires this in the same change.

- [ ] **Step 2: the docstring that stops being true**

`config.py` calls itself "the ONLY write path for llm / embedding / rerank / multimodal".
After Task 5 it is the only write path for `[api]`.

- [ ] **Step 3: the gates, all of them**

```bash
uv run pytest tests/test_everos_backend.py tests/test_everos_server.py tests/test_rpc_settings.py -q
# baseline before this change, copied 2026-09-21: 341 passed, 1 skipped in 7.62s
make check-source-language
make check-large-files
npm --prefix ui-web run gen:check
npm --prefix ui-web run lint
npm --prefix ui-web test
```

Then the full suite once, with the same extras CI uses.

---

## Self-review before dispatching the reviewer

- [ ] Every A1..A22 points at a task's **Delivers** line.
- [ ] Every C1..C22 points at a task or a gate.
- [ ] No placeholder text: no TBD, no "add appropriate error handling", no "write tests for
      the above" without the test body.
- [ ] Names match across tasks: `resolve_role` / `role_pin` / `everos_env` / `set_role` /
      `describe_roles` / `migrate_roles` / `restart_for_config_change` / `precheck_spawn` /
      `stop_for_reload` are spelled the same everywhere they appear.
- [ ] Each task climbed the ponytail ladder. Task 1 is a move, not new code. Task 3 reuses
      the existing `_child_env` seam. Task 9 reuses `adopt_legacy_endpoint`'s shape and
      `loader.py`'s existing migration chain. Nothing here introduces an abstraction with one
      implementation.
- [ ] Authorisation list and goal text are both filled in above.
- [ ] `deviations.md` exists and is empty.
