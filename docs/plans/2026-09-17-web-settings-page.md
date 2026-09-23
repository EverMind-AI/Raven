# Web settings page rebuilt on the v0.2 prototype - implementation plan

> For the executor: follow `dev-workflow` stage 4; tick steps as `- [ ]`; when
> a situation is not covered here, check Global Constraints first, then the
> three tiers of `unattended-run` section 7 (stop / fallback / assumption
> mismatch), and record before acting. Do not guess.

**Goal**: when this plan is done, the settings dialog served by `raven serve`
on `refactor/ui_web_architecture` shows the prototype's eight pages, every
enabled control reads a real value and writes one, and the cases of
`docs/specs/2026-09-17-web-settings-page-acceptance.md` pass on a real host.

**Design document**: `docs/specs/2026-09-17-web-settings-page-design.md`
(acceptance items A1..A55, constraints C1..C21, decisions).

**Approach**: backend first, as one commit series (whitelist, usage, sessions,
skills, provider writes, OAuth, plugins), each method declared in the RPC
schema before any client code and covered by a pytest that a reverted handler
fails. Then the front-end domain skeleton beside the old page, eight pages,
the switch-over, the deletion of the old page. Then the real-host acceptance
run and the PR. The first front-end commit waits for
`refactor/ui_web_conventions` for as long as the backend batch takes (C20).

**Stack**: Python 3 / pydantic v2 / aiohttp gateway (`raven/`), pytest via
`uv run`; React 19 + TypeScript + Vite lib IIFE (`ui-web/`), vitest,
`playwright-cli` for the real browser; `rpc-schema/openrpc.json` as the
contract, `npm run gen` for the client.

**Change map**: the table "Change map" in the design document. Task numbers
below follow its rows top to bottom: backend rows are Tasks 1-10, the
front-end rows Tasks 11-20, the acceptance run Task 21, the PR Task 22.

**Baselines (copied from real runs on this branch, 2026-09-17)**:

```
uv run pytest tests/test_rpc_settings.py tests/test_rpc_model.py \
  tests/test_rpc_plughub.py tests/test_rpc_skills.py tests/test_rpc_session.py \
  tests/test_rpc_console.py tests/test_rpc_schema_match.py \
  tests/test_rpc_contract_shapes.py -q -p no:cacheprovider
919 passed in 13.30s

cd ui-web && npm test -s
 Test Files  152 passed (152)
      Tests  2494 passed (2494)

cd ui-web && npm run -s gen:check
generated.ts matches the contract (178 methods)
```

## Global Constraints

Copied from the design document's constraint tables; each line carries its
check. `BASE` is `$(git merge-base HEAD refactor/ui_web_architecture)`.

- C1 `features/settings/` follows the domain skeleton of the architecture note: `types.ts`, `source.ts`, `store.ts`, `SettingsApp.tsx`, `wire.ts` (today `chrome.ts`), plus `styles.css` and `manifest.ts` when their mechanisms land. Every `gateway().call` for this domain lives in `source.ts`, including the version check and the session-scoped permission write that `chrome.ts` makes today. Check: `grep -rln "gateway()" ui-web/src/features/settings --include=*.ts --include=*.tsx | grep -v "\.test\."` prints only `source.ts`.
- C2 Cross-domain reach is through another domain's `source.ts` or `types.ts` only. No `islands.*`, no assignment onto `window`, no import from `src/shell/`. Shared components live in `src/components/`; URLs are opened through `src/lib/openUrl.ts`. Check: `grep -rnE "from '\.\./[a-z]+/(store|[A-Z])" ui-web/src/features/settings` prints nothing; `grep -rnE "islands\.|window\.[a-zA-Z_]+ *=|from '\.\./\.\./shell" ui-web/src/features/settings` prints nothing.
- C3 Stores expose `get() / set(next) / subscribe(fn) / _resetForTests()`; components read through `useSyncExternalStore`; the only way to force a repaint is the store's `redraw()`. Check: `store.test.ts` asserts the four names; `grep -rnE "getState|snapshot\(|draw\(|langRedraw|forceUpdate" ui-web/src/features/settings` prints nothing.
- C4 Every class the island introduces is `settings-` prefixed and every DOM id it owns is `settings-` prefixed. Rules go into `src/styles/page.css` until the per-domain `styles.css` import (S8) exists. Check: `grep -rhoE "className=\"[^\"]+\"" ui-web/src/features/settings | tr ' ' '\n'` yields only `settings-` names and names in the shared vocabulary of `scripts/check-class-namespace.mjs`; `grep -rhoE "id=\"[^\"]+\"" ui-web/src/features/settings` yields only `settings-` names.
- C5 All text is `t(key)` with keys under `gui.settings.<leaf>` in `i18n/messages.json`, both locales filled. No `T()`, no `lang.text()`. Check: `grep -rnE "\bT\(|lang\.text\(" ui-web/src/features/settings` prints nothing; the locale script of Task 19 passes; `make check-source-language`.
- C6 Contract first: a new or changed RPC field is edited in `rpc-schema/openrpc.json`, then `npm run gen`, then `source.ts`, `store.ts`, component. Every method the settings source calls has a reply in `rpc/fixtures/settings.ts`, typed with `: Type`, never `as`. Check: `npm run gen:check`; `tests/test_rpc_schema_match.py`; the fixture test of Task 20; `grep -n " as " ui-web/src/rpc/fixtures/settings.ts` prints nothing.
- C7 Backend writes go through `atomic_update` on the existing config file and the existing session files. No new store, no new file format; a new writer sits in the module that owns that file. Check: review against this line; `git diff BASE..HEAD --stat -- raven` touches no new directory.
- C8 Not modified: `App.tsx`, `state/settings.ts`, `state/lang`, `state/toast.ts`, `chrome/`, `app/`, `lib/notifications.ts`, the look store, the gate scripts. Exemptions: `main.tsx` changes only the settings island's import and render lines; `scripts/check-class-namespace.mjs` gains the settings entry if the conventions branch has not made it a ratchet. Check: `git diff BASE..HEAD --stat -- ui-web/src/App.tsx ui-web/src/state/settings.ts ui-web/src/state/lang ui-web/src/state/toast.ts ui-web/src/chrome ui-web/src/app ui-web/src/lib/notifications.ts ui-web/scripts/gates` is empty.
- C9 A reload-only key returns `warning` from `settings.set`, and the toast shows that text instead of "saved". Check: A46.
- C10 Secrets never round-trip: keys and header values read back as bullets plus the last four characters, plugin credentials as set / not set; the page never sends a masked value as a new value. Check: A10, A17, A37.
- C11 The old page goes: `SettingsPage.tsx`, its test, its snapshot and `ImageModelPicker.tsx` are deleted; every `gui.set.*` key left in `i18n/messages.json` is referenced outside `ui-web/src/features/settings/`. Check: A50; the script of Task 19.
- C12 `permissions.mode`, channel, exec and memory keys stay in the whitelist with their tests. Check: `tests/test_rpc_settings.py` passes with no test removed.
- C13 Skill on/off applies on the next turn without a restart. Check: A25.
- C14 (verified) media generation is OpenRouter-only. C15 (verified) listing sessions already scans every file; metadata saves append. C16 (verified) a catalog-installed MCP server is named by its entry id and recorded in the ledger. C17 (unverified) each OAuth device flow yields a URL and a code without a console: spike S1 in Task 8. C18 (verified) `gateway.reload` is not on `/rpc`. C19 (verified) `.install-meta.json` carries `installed_at`, `version`, `trigger`, `source`; `skillhub.install` does not stamp today. C20 (unverified) `refactor/ui_web_conventions` lands during the backend batch; fallback in Task 11. C21 (verified) `ext.list` carries the skill and MCP fields the pages need; `model.options` carries `context_window`.

### Authorization (this run)

Baseline: `unattended-run` section 1. Pre-authorized on top of it: rebasing
onto `refactor/ui_web_architecture` (and onto `refactor/ui_web_conventions`
once it lands); running the real-host cases with the operator's provider
credentials copied into a temporary home; creating temporary directories and
processes, torn down at the end and recorded in the deviations ledger. Never
pre-authorized: editing committed team-rule files (`AGENTS.md`,
`CLAUDE.md`, `.github/`).

Project rules that bind this run (from the operator's project section):
- Commits and pushes happen only on the operator's explicit word (repository
  AGENTS.md 3.4 and 3.6); a finished task is reported, not committed.
- The full test suite is slow: run only the touched files during a task; one
  full `uv run pytest` and one full `npm test` at wrap-up, and ask before the
  Python one.
- All git writes go through `git -C /Users/admin/Raven-b`; verify
  `git -C /Users/admin/Raven-b branch --show-current` prints
  `feat/ui_web_settings_page` before any commit.
- `rm`, `cp`, `mv` are aliased interactive on this machine: use `command rm -f`
  and friends in scripts. Background processes get `trap ... EXIT` and a hard
  lifetime cap.

### Limits and goal

Rounds: 40. Time: 48 hours. Goal text (paste verbatim into `/goal`):

> All real-host cases in `docs/specs/2026-09-17-web-settings-page-acceptance.md` pass and their commands rerun as written; the PR to `refactor/ui_web_architecture` is open; every blocker of the self-review is handled; every external review comment that has arrived is handled or recorded in the deviations ledger; **the operator confirms acceptance in the conversation**. Or the deviations ledger gains a stop-tier entry. Or 40 rounds. Or 48 hours.

Models: executor and task-level review at the second tier (`opus`);
completeness review at the top tier (`fable`); fact lookups at the cheapest
(`haiku`).

---

## Task 1: config and registry additions

**Delivers**: A10 (the key link), A43 (the config key), groundwork for A12, A42.

**Files**:
- modify: `raven/config/raven.py` (a `SessionsConfig` next to `SessionTitleConfig` at line 1395; attach in `RavenConfig` next to line 1574)
- modify: `raven/providers/registry.py` (`ProviderSpec.key_url` after `homepage` at line 44; fourteen entries)
- modify: `raven/rpc/methods/model.py` (provider row at lines 359-372 gains `key_url`)
- modify: `rpc-schema/openrpc.json` (`ModelOptionProvider` schema gains `key_url`), `raven/rpc/models.py` (`ModelOptionProvider.key_url: str | None = None`)
- tests: `tests/test_config_raven_loader.py`, `tests/test_rpc_model.py`

**Interfaces**:
- produces: `RavenConfig.sessions.auto_archive_after_days: int | None` (Task 5 reads it); `ProviderSpec.key_url: str` and `model.options` provider rows carrying `key_url` (Task 14 reads it).

- [ ] **Step 1: write the tests**

```python
def test_sessions_config_defaults_off():
    cfg = load_raven_config_from({"sessions": {}})
    assert cfg.sessions.auto_archive_after_days is None

def test_sessions_config_reads_camel_case():
    cfg = load_raven_config_from({"sessions": {"autoArchiveAfterDays": 30}})
    assert cfg.sessions.auto_archive_after_days == 30

async def test_model_options_carries_key_url(dispatch):
    rows = (await dispatch("model.options", {}))["providers"]
    by = {r["slug"]: r for r in rows}
    assert by["deepseek"]["key_url"] == "https://platform.deepseek.com/api_keys"
    assert by["hosted_vllm"]["key_url"] is None
```

- [ ] **Step 2: implement**

```python
# raven/config/raven.py
class SessionsConfig(_Base):
    """Session housekeeping the settings page controls."""

    auto_archive_after_days: int | None = None
    """``session.list`` archives an unpinned session whose last message is
    older than this many days. None turns the pass off."""

class RavenConfig(_Base):
    ...
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
```

```python
# raven/providers/registry.py, ProviderSpec
    homepage: str = ""
    # Where the vendor hands out API keys. Empty for OAuth and local providers
    # and for vendors the registry carries no console link for.
    key_url: str = ""
```

Fourteen values, one per spec: anthropic `https://console.anthropic.com/settings/keys`, openai `https://platform.openai.com/api-keys`, deepseek `https://platform.deepseek.com/api_keys`, gemini `https://aistudio.google.com/apikey`, zai `https://z.ai/manage-apikey/apikey-list`, dashscope `https://bailian.console.aliyun.com/?apiKey=1`, moonshot `https://platform.moonshot.cn/console/api-keys`, minimax `https://platform.minimaxi.com/user-center/basic-information/interface-key`, groq `https://console.groq.com/keys`, azure_openai `https://portal.azure.com/`, openrouter `https://openrouter.ai/settings/keys`, aihubmix `https://aihubmix.com/token`, siliconflow `https://cloud.siliconflow.cn/account/ak`, volcengine `https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey`.

```python
# raven/rpc/methods/model.py, the provider row dict
        "homepage": (spec.homepage or None) if spec else None,
        "key_url": (spec.key_url or None) if spec else None,
```

Schema: add `"key_url": {"type": ["string", "null"]}` to `ModelOptionProvider` in `openrpc.json` (not required); `raven/rpc/models.py` `ModelOptionProvider` gains `key_url: str | None = None`.

- [ ] **Step 3: run and prove it can fail**

Run: `uv run pytest tests/test_config_raven_loader.py tests/test_rpc_model.py tests/test_rpc_schema_match.py -q -p no:cacheprovider`
Expected: all pass (copy the summary line into the report).
Mutation: blank the deepseek `key_url` in the registry -> `test_model_options_carries_key_url` fails on the equality; restore, `git diff` clean.

- [ ] **Step 4: record** anything that did not match (deviations ledger).

## Task 2: `settings.set` whitelist, reload-only warning, blocklist raw list

**Delivers**: A20 (writes and warning), A22 (speech / video keys), A23 (cap, window, effort), A25 (the write half), A43, A46.

**Files**:
- modify: `raven/rpc/methods/console.py` (`_SETTINGS_SIMPLE_KEYS` at line 684; the raw-list tuple at line 572; helpers next to `_chk_int` at line 634; the return path at lines 575-588)
- modify: `raven/i18n/zh.py` (the zh line for the new warning key)
- modify: `rpc-schema/openrpc.json` (nothing: `warning` was declared by the previous change)
- tests: `tests/test_rpc_settings.py`

**Interfaces**:
- consumes: nothing new.
- produces: `settings.set` accepts the keys below and answers `{"applied": true, "previous": ..., "warning"?: str}`; `settings.set {key: "skillForge.blocklist", value: [str]}` writes the list. Task 3 reads that list live; Tasks 13-18 call these keys from `source.ts`.

- [ ] **Step 1: write the tests** (one per key; two shown)

```python
@pytest.mark.parametrize("key,good,bad", [
    ("agents.defaults.maxToolIterations", 120, 0),
    ("agents.defaults.contextWindowTokens", 65536, 512),
    ("context.curatorModel", "deepseek-chat", 7),
    ("sessionTitle.provider", "deepseek", 7),
    ("sessions.autoArchiveAfterDays", 30, 0),
])
async def test_settings_set_new_scalar_keys(dispatch, config_file, key, good, bad):
    r = await dispatch("settings.set", {"key": key, "value": good})
    assert r["applied"] is True
    assert dotted(config_file.read(), key) == good
    with pytest.raises(ConfigValidationError):
        await dispatch("settings.set", {"key": key, "value": bad})

async def test_settings_set_warns_only_for_reload_only_keys(dispatch, config_file):
    warned = await dispatch("settings.set", {"key": "context.curatorModel", "value": "m"})
    live = await dispatch("settings.set", {"key": "sessionTitle.model", "value": "m"})
    assert warned["warning"] and "reload" in warned["warning"].lower()
    assert "warning" not in live

async def test_settings_set_blocklist_is_a_raw_list(dispatch, config_file):
    await dispatch("settings.set", {"key": "skillForge.blocklist", "value": ["codeword"]})
    assert config_file.read()["skillForge"]["blocklist"] == ["codeword"]
```

- [ ] **Step 2: implement**

```python
# raven/rpc/methods/console.py
def _chk_int_or_null(key: str, lo: int, hi: int):
    inner = _chk_int(key, lo, hi)
    return lambda v: None if v is None else inner(v)

def _chk_str_or_null(key: str, max_len: int = 200):
    inner = _chk_str(key, max_len)
    return lambda v: None if v is None else inner(v)

def _chk_media_selection_for(kind: str):
    def check(value):
        if not isinstance(value, dict):
            raise ConfigValidationError(f"tools.media.{kind} must be an object")
        return {k: _SETTINGS_SIMPLE_KEYS[f"tools.media.image.{k}"](v) for k, v in value.items()}
    return check

_RELOAD_ONLY_KEYS = frozenset({
    "agents.defaults.maxToolIterations", "agents.defaults.contextWindowTokens",
    "context.curatorModel", "context.curatorProvider",
    "skillForge.llmGateModel", "skillForge.llmGateProvider",
})

_SETTINGS_SIMPLE_KEYS.update({
    "agents.defaults.maxToolIterations": _chk_int("agents.defaults.maxToolIterations", 1, 200),
    "agents.defaults.contextWindowTokens": _chk_int_or_null("agents.defaults.contextWindowTokens", 1024, 100_000_000),
    "context.curatorModel": _chk_str_or_null("context.curatorModel"),
    "context.curatorProvider": _chk_str_or_null("context.curatorProvider"),
    "sessionTitle.model": _chk_str_or_null("sessionTitle.model"),
    "sessionTitle.provider": _chk_str_or_null("sessionTitle.provider"),
    "skillForge.llmGateModel": _chk_str_or_null("skillForge.llmGateModel"),
    "skillForge.llmGateProvider": _chk_str_or_null("skillForge.llmGateProvider"),
    "tools.media.speech": _chk_media_selection_for("speech"),
    "tools.media.video": _chk_media_selection_for("video"),
    "sessions.autoArchiveAfterDays": _chk_int_or_null("sessions.autoArchiveAfterDays", 1, 3650),
})
```

In `settings_set`: the raw-list tuple becomes
`("plugins.disabled", "tools.disabledTools", "skillForge.blocklist")`; after
`written = _write_raw_key(...)` add

```python
    if key in _RELOAD_ONLY_KEYS:
        from raven.i18n import t
        written["warning"] = t("settings.applies_after_reload")
```

with the English default and the zh line ("applies after the next gateway
reload" / its translation) added the way `embedding_model_change` adds its
sentence (`raven/config/update.py:576`).

- [ ] **Step 3: run and prove it can fail**

Run: `uv run pytest tests/test_rpc_settings.py -q -p no:cacheprovider`
Expected: all pass (copy).
Mutation: remove `"context.curatorModel"` from `_RELOAD_ONLY_KEYS` -> the warning test fails on `warned["warning"]`; remove the blocklist entry from the raw-list tuple -> the blocklist test fails on the refusal; restore, `git diff` clean.

- [ ] **Step 4: record.**

## Task 3: the skill blocklist reads live

**Delivers**: A25 (the live half), C13.

**Files**:
- modify: `raven/config/live.py` (a reader next to `disabled_tool_names` at line 313)
- modify: `raven/memory_engine/skill_forge/catalog.py` (`__init__` at line 34, `_blocklist` at lines 73-88, `_is_blocked` at line 112, `_BlocklistRegistryView` at line 513)
- modify: `raven/agent/context/builder.py` (`ContextBuilder.__init__` at lines 27-40 passes a reader through), `raven/agent/loop/main.py` (line 364 passes `lambda: skill_blocklist(self._live_config)`; `self._live_config` is created at line 383, so the lambda must not be called before it exists, which it is not: the catalog only reads on `is_blocked` and pool builds)
- tests: `tests/test_skill_forge_catalog*.py` (the existing catalog tests) plus one new case

**Interfaces**:
- produces: `raven.config.live.skill_blocklist(live: LiveConfig) -> frozenset[str]`; `LocalSkillCatalog(..., blocklist_reader: Callable[[], frozenset[str]] | None = None)`.

- [ ] **Step 1: write the test**

```python
def test_blocklist_changes_on_disk_apply_without_rebuild(tmp_path, skill_dirs):
    live = LiveConfig(tmp_path / "config.json")
    write_config(tmp_path / "config.json", {"skillForge": {"blocklist": []}})
    cat = LocalSkillCatalog(skill_dirs.workspace, blocklist_reader=lambda: skill_blocklist(live), start_watcher=False)
    assert not cat._is_blocked("codeword")
    write_config(tmp_path / "config.json", {"skillForge": {"blocklist": ["codeword"]}})
    assert cat._is_blocked("codeword")
    assert "codeword" not in {m.name for m in cat.pool_registry.list_all()}
```

- [ ] **Step 2: implement**

```python
# raven/config/live.py
def skill_blocklist(live: LiveConfig) -> frozenset[str]:
    """The operator's skill off switches, read on every ask."""
    from raven.skill_hub.policy import normalize_blocklist
    value = live.get("skillForge.blocklist")
    if not isinstance(value, list):
        return frozenset()
    return normalize_blocklist([str(x) for x in value if isinstance(x, str)])
```

```python
# raven/memory_engine/skill_forge/catalog.py
    def __init__(self, workspace, config=None, builtin_skills_dir=None, *,
                 start_watcher=True, blocklist_reader=None):
        ...
        frozen = normalize_blocklist(getattr(config, "blocklist", None))
        self._blocklist_reader = blocklist_reader or (lambda: frozen)
        pool_registry = _BlocklistRegistryView(self._registry, self._blocklist_reader)
        self._local_pool = LocalPool(pool_registry)

    def _current_blocklist(self) -> frozenset[str]:
        return self._blocklist_reader()

    def _is_blocked(self, name: str) -> bool:
        return is_blocked(self._current_blocklist(), name)

class _BlocklistRegistryView:
    def __init__(self, registry, reader):
        self._registry, self._reader = registry, reader
    # every filtering method calls self._reader() instead of a stored set
```

The "hiding blocked skill(s)" log line moves into the view's first filtered
call per distinct list (log once per change, not per ask).

- [ ] **Step 3: run and prove it can fail**

Run: `uv run pytest tests/test_skill_forge_catalog*.py -q -p no:cacheprovider`
Expected: all pass (copy).
Mutation: make `_current_blocklist` return the frozen set -> the new test fails on the second `_is_blocked`; restore, `git diff` clean.

- [ ] **Step 4: record.** This reverses row 10 of the hot-reload design for this key; the decision is in the design document, so the ledger gets no row unless the change misbehaves.

## Task 4: `settings.usage` range and daily buckets

**Delivers**: A4, A5, A6, A7, A8.

**Files**:
- modify: `raven/rpc/methods/console.py` (`settings_usage` at lines 715-897)
- modify: `rpc-schema/openrpc.json` (`settings.usage` params gain `from`, `to`; result gains `from`, `to`, `daily`), `raven/rpc/models.py` (`SettingsUsageParams`, `SettingsUsageResult`, a `DailyUsage` model)
- tests: `tests/test_rpc_settings.py`

**Interfaces**:
- produces: `settings.usage {from?: "YYYY-MM-DD", to?: "YYYY-MM-DD", days?: int}` -> result plus `from`, `to`, `daily: [{date, calls, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd}]`. Task 13 reads it.

- [ ] **Step 1: write the tests**

```python
async def test_usage_daily_buckets_cover_the_range_with_zero_days(dispatch, telemetry):
    telemetry.write(days_ago=1, model="a", cost=1.0)
    telemetry.write(days_ago=3, model="a", cost=2.0)
    r = await dispatch("settings.usage", {"from": iso(days_ago=4), "to": iso(days_ago=1)})
    assert [d["date"] for d in r["daily"]] == [iso(4), iso(3), iso(2), iso(1)]
    assert [d["cost_usd"] for d in r["daily"]] == [None, 2.0, None, 1.0]
    assert r["llm"]["total"]["cost_usd"] == 3.0

async def test_usage_from_is_clamped_and_reversed_range_refused(dispatch):
    r = await dispatch("settings.usage", {"from": iso(days_ago=400), "to": iso(0)})
    assert r["from"] == iso(days_ago=89)
    with pytest.raises(ConfigValidationError):
        await dispatch("settings.usage", {"from": iso(0), "to": iso(days_ago=1)})

async def test_usage_from_to_win_over_days(dispatch, telemetry):
    telemetry.write(days_ago=10, model="a", cost=5.0)
    r = await dispatch("settings.usage", {"days": 30, "from": iso(2), "to": iso(0)})
    assert r["llm"]["total"]["calls"] == 0
```

- [ ] **Step 2: implement**

```python
    today = date.today()
    lo = today - timedelta(days=89)
    frm = _parse_day(params.get("from")); to = _parse_day(params.get("to"))
    if frm or to:
        to = to or today
        frm = max(frm or lo, lo)
        if frm > to:
            raise ConfigValidationError("from must not be after to")
    else:
        to = today
        frm = today - timedelta(days=days - 1)
    dates = [frm + timedelta(days=i) for i in range((to - frm).days + 1)]
    daily = {d.isoformat(): {"date": d.isoformat(), **empty_totals()} for d in dates}
    for d in dates:
        p = tel_dir / f"usage-{d.isoformat()}.jsonl"
        ...  # the existing per-row loop, accumulating into daily[d.isoformat()], acc and total
```

The tool scan uses the same `dates` list; `cutoff` for the transcript scan is
`frm` at midnight. Result adds `"from": frm.isoformat()`, `"to": to.isoformat()`,
`"daily": list(daily.values())`. `empty_totals` already yields `None` for a
day with no priced call, which is the zero-bar the page draws.

- [ ] **Step 3: run and prove it can fail**

Run: `uv run pytest tests/test_rpc_settings.py -q -p no:cacheprovider -k usage`
Expected: all pass (copy).
Mutation: drop the `max(..., lo)` clamp -> the clamp test fails on `r["from"]`; restore.

- [ ] **Step 4: record.**

## Task 5: archived sessions and the lazy auto-archive pass

**Delivers**: A41, A42, A43 (the pass half), A44.

**Files**:
- modify: `raven/session/manager.py` (a metadata-append helper next to `save` at line 614)
- modify: `raven/rpc/methods/session.py` (`session_list` at lines 594-625, `session_archive` at lines 783-806)
- modify: `rpc-schema/openrpc.json` (`session.list` params gain `archived: boolean`), `raven/rpc/models.py`
- tests: `tests/test_rpc_session.py`

**Interfaces**:
- consumes: `RavenConfig.sessions.auto_archive_after_days` (Task 1).
- produces: `SessionManager.append_metadata_patch(key: str, patch: dict) -> None`; `session.list {archived: true}`; a restore that writes `archived: false`. Task 18 reads them.

- [ ] **Step 1: write the tests**

```python
async def test_session_list_archived_true_returns_only_archived(dispatch, sessions):
    sessions.make("a", archived=True); sessions.make("b")
    assert [s["id"] for s in (await dispatch("session.list", {"archived": True}))["sessions"]] == ["a"]
    assert [s["id"] for s in (await dispatch("session.list", {}))["sessions"]] == ["b"]

async def test_auto_archive_marks_stale_unpinned_once(dispatch, sessions, raven_config):
    raven_config.write({"sessions": {"autoArchiveAfterDays": 30}})
    sessions.make("old", last_message_days_ago=40)
    sessions.make("pinned", last_message_days_ago=40, pinned=True)
    sessions.make("fresh", last_message_days_ago=1)
    await dispatch("session.list", {})
    assert sessions.last_meta("old")["metadata"] == {**sessions.last_meta("old")["metadata"], "archived": True, "archivedBy": "auto"}
    assert "archived" not in sessions.last_meta("pinned")["metadata"]
    assert sessions.line_count("old") == sessions.baseline_lines("old") + 1
    await dispatch("session.list", {})
    assert sessions.line_count("old") == sessions.baseline_lines("old") + 1

async def test_restored_session_is_never_auto_archived_again(dispatch, sessions, raven_config):
    raven_config.write({"sessions": {"autoArchiveAfterDays": 30}})
    sessions.make("old", last_message_days_ago=40, archived=True)
    await dispatch("session.archive", {"session_id": "old", "archived": False})
    await dispatch("session.list", {})
    assert sessions.last_meta("old")["metadata"]["archived"] is False

async def test_auto_archive_never_loads_transcripts(dispatch, sessions, raven_config, monkeypatch):
    raven_config.write({"sessions": {"autoArchiveAfterDays": 30}})
    for i in range(50): sessions.make(f"s{i}", last_message_days_ago=40)
    calls = []
    monkeypatch.setattr(SessionManager, "_load", lambda self, key: calls.append(key) or real_load(self, key))
    await dispatch("session.list", {})
    assert calls == []
```

- [ ] **Step 2: implement**

```python
# raven/session/manager.py
    def append_metadata_patch(self, key: str, patch: dict[str, Any]) -> None:
        """Append one metadata record with ``patch`` folded in, without
        reading the transcript. The last metadata record wins on load, which
        is why appending is enough."""
        path = self.session_path(key)
        last = self._last_metadata_record(path)  # the reader list_sessions already uses
        if last is None:
            return
        merged = {**(last.get("metadata") or {}), **patch}
        locked_append(path, [json.dumps({**last, "metadata": merged}, ensure_ascii=False)])
        cached = self._cache.get(key)
        if cached is not None:
            cached.metadata.update(patch)
```

```python
# raven/rpc/methods/session.py, session_list
    entries = mgr.list_sessions(channels=channels)
    after_days = load_raven_config().sessions.auto_archive_after_days
    if after_days:
        cutoff = (datetime.now() - timedelta(days=after_days)).isoformat()
        for e in entries:
            meta = e.get("metadata") or {}
            if "archived" in meta or meta.get("pinned"):
                continue
            last = e.get("last_message_at") or e.get("updated_at") or ""
            if last and last < cutoff:
                mgr.append_metadata_patch(e["key"], {"archived": True, "archivedBy": "auto"})
                meta["archived"] = True
                e["metadata"] = meta
    want_archived = bool(params.get("archived", False))
    entries = [e for e in entries if bool((e.get("metadata") or {}).get("archived")) is want_archived]
```

`session_archive`: on restore, `session.metadata["archived"] = False` instead
of `pop`.

- [ ] **Step 3: run and prove it can fail**

Run: `uv run pytest tests/test_rpc_session.py -q -p no:cacheprovider`
Expected: all pass (copy).
Mutation: drop the `"archived" in meta` guard -> the restored-session test fails; make the helper call `self.get_or_create` -> the no-load test fails on `calls == []`; restore.

- [ ] **Step 4: record.** If `list_sessions` entries turn out not to carry `key`, record an assumption mismatch and read it from the metadata record instead.

## Task 6: skill detail, file open, install stamp

**Delivers**: A26, A27; A28 unchanged (`skillhub.remove`).

**Files**:
- modify: `raven/rpc/methods/skills.py` (`_action_inspect` at lines 73-95; new `_action_open`; `_ACTIONS`)
- modify: `raven/rpc/methods/console.py` (extract the opener at lines 1720-1730 into `_open_with_system(target: Path) -> None`, used by `fs_open` and by skills)
- modify: `raven/skill_hub/hub.py` (`install` at line 488 stamps `write_install_meta(target, slug=skill_id, version=..., trigger="rpc")` where `target` is the extracted directory at line 274)
- modify: `rpc-schema/openrpc.json`, `raven/rpc/models.py` (`SkillsManageResult.info` gains the fields; `action` enum gains `open`; params gain `file`)
- tests: `tests/test_rpc_skills.py`, `tests/test_skill_hub*.py`

**Interfaces**:
- produces: `skills.manage {action: "inspect", query}` -> `info` plus `body`, `files`, `always`, `hub`, `hub_id`, `install: {installed_at, version, trigger, source, score_safety} | null`; `skills.manage {action: "open", query, file}` -> `{opened: true}`. Task 15 reads them.

- [ ] **Step 1: write the tests**

```python
async def test_inspect_carries_body_files_and_install_meta(dispatch, skill_dirs):
    skill_dirs.hub_skill("weather", body="# Weather", files=["helper.py"], meta={"installed_at": "2026-09-01T00:00:00+00:00", "version": "v2", "trigger": "use_skill", "source": "hub"}, audit_safety=0.9)
    info = (await dispatch("skills.manage", {"action": "inspect", "query": "weather"}))["info"]
    assert info["body"].startswith("# Weather")
    assert info["files"] == ["SKILL.md", "helper.py"]
    assert info["install"] == {"installed_at": "2026-09-01T00:00:00+00:00", "version": "v2", "trigger": "use_skill", "source": "hub", "score_safety": 0.9}
    assert info["hub"] is True

async def test_inspect_without_meta_reports_null_install(dispatch, skill_dirs):
    skill_dirs.workspace_skill("codeword")
    assert (await dispatch("skills.manage", {"action": "inspect", "query": "codeword"}))["info"]["install"] is None

async def test_open_refuses_paths_outside_the_skill(dispatch, skill_dirs, opener_spy):
    skill_dirs.workspace_skill("codeword")
    await dispatch("skills.manage", {"action": "open", "query": "codeword", "file": "SKILL.md"})
    assert opener_spy.calls == [skill_dirs.path("codeword") / "SKILL.md"]
    with pytest.raises(ConfigValidationError):
        await dispatch("skills.manage", {"action": "open", "query": "codeword", "file": "../../config.json"})
```

- [ ] **Step 2: implement**

```python
# raven/rpc/methods/skills.py
def _install_meta(skill_dir: Path, slug: str) -> dict | None:
    meta_path = skill_dir / ".install-meta.json"
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return {
        "installed_at": meta.get("installed_at"),
        "version": meta.get("version", ""),
        "trigger": meta.get("trigger", ""),
        "source": meta.get("source", ""),
        "score_safety": _last_audit_safety(slug),  # last record for the slug in the audit jsonl the two install paths write
    }

async def _action_inspect(query, _page, factory):
    ...  # existing lookup
    skill_dir = Path(meta.path)
    marker = _hub_marker_name()
    return {"info": {
        "name": meta.name, "description": meta.description, "category": meta.source,
        "path": str(skill_dir),
        "body": (skill_dir / "SKILL.md").read_text(encoding="utf-8") if (skill_dir / "SKILL.md").is_file() else "",
        "files": sorted(p.name for p in skill_dir.iterdir() if p.is_file() and not p.name.startswith(".")),
        "always": bool(getattr(meta, "always", False)),
        "hub": bool(marker and (skill_dir / marker).is_file()),
        "hub_id": _hub_id(skill_dir, marker),
        "install": _install_meta(skill_dir, meta.name),
    }}

async def _action_open(query, _page, factory, *, file: str = "") -> dict:
    meta = _registry(factory).get(query)
    if meta is None:
        raise ConfigValidationError(f"unknown skill {query!r}")
    root = Path(meta.path).resolve()
    target = (root / file).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ConfigValidationError("file must be inside the skill directory")
    _open_with_system(target)
    return {"opened": True}
```

`_hub_marker_name` and the `hub_id` derivation are lifted from `ext_list`
(`console.py:65-112`) into a small helper both call.

- [ ] **Step 3: run and prove it can fail**

Run: `uv run pytest tests/test_rpc_skills.py tests/test_skill_hub*.py -q -p no:cacheprovider`
Expected: all pass (copy).
Mutation: drop the `is_relative_to` check -> the traversal test fails on the missing refusal; restore.

- [ ] **Step 4: record.**

## Task 7: provider field writes, display names, batch add

**Delivers**: A12, A15, A16, A17, A18.

**Files**:
- modify: `raven/rpc/methods/model.py` (new `model_set_fields`, `model_add_models`; `model_add_model` gains `description`; registrations)
- modify: `raven/config/update_providers.py` (`add_provider_model` overlay merge at lines 905-918 treats `""` as delete; new `add_provider_models(name, models, *, config_path=None) -> list[str]` next to it)
- modify: `rpc-schema/openrpc.json`, `raven/rpc/models.py` (`ModelSetFieldsParams`, `ModelSetFieldsResult`, `ModelAddModelsParams`, `ModelAddModelParams.description`, `METHOD_MODELS` entries)
- tests: `tests/test_rpc_model.py`

**Interfaces**:
- produces: `model.set_fields {slug, fields: {api_base?, deployment?, api_version?, extra_headers?: {name: str | null}}}` -> `{previous: {...masked}}`; `model.add_models {slug, models: [str]}` -> `{provider}`; `model.add_model` accepts `description`. Task 14 calls them.

- [ ] **Step 1: write the tests**

```python
async def test_set_fields_patches_headers_and_masks_reply(dispatch, config_file):
    await dispatch("model.set_fields", {"slug": "moonshot", "fields": {"extra_headers": {"X-App": "alpha"}}})
    await dispatch("model.set_fields", {"slug": "moonshot", "fields": {"extra_headers": {"X-Env": "beta"}}})
    assert config_file.read()["providers"]["moonshot"]["extraHeaders"] == {"X-App": "alpha", "X-Env": "beta"}
    r = await dispatch("model.set_fields", {"slug": "moonshot", "fields": {"extra_headers": {"X-App": None}}})
    assert config_file.read()["providers"]["moonshot"]["extraHeaders"] == {"X-Env": "beta"}
    assert "alpha" not in json.dumps(r)

async def test_set_fields_refuses_the_key(dispatch):
    with pytest.raises(ConfigValidationError):
        await dispatch("model.set_fields", {"slug": "moonshot", "fields": {"api_key": "x"}})

async def test_set_fields_writes_azure_deployment_without_a_key(dispatch, config_file):
    await dispatch("model.set_fields", {"slug": "azure_openai", "fields": {"deployment": "gpt-4o-eu", "api_version": "2024-10-21"}})
    az = config_file.read()["providers"]["azureOpenai"]
    assert (az["deployment"], az["apiVersion"]) == ("gpt-4o-eu", "2024-10-21")

async def test_add_model_description_and_empty_string_clears(dispatch, config_file):
    await dispatch("model.add_model", {"slug": "deepseek", "model": "deepseek-v4-flash", "label": "Flash", "description": "cheap"})
    ov = lambda: config_file.read()["providers"]["deepseek"]["modelOverlay"]["deepseek-v4-flash"]
    assert ov() == {"label": "Flash", "description": "cheap"}
    await dispatch("model.add_model", {"slug": "deepseek", "model": "deepseek-v4-flash", "label": ""})
    assert ov() == {"description": "cheap"}

async def test_add_models_is_one_write(dispatch, config_file, write_spy):
    r = await dispatch("model.add_models", {"slug": "deepseek", "models": ["a", "b", "a"]})
    assert config_file.read()["providers"]["deepseek"]["models"][-2:] == ["a", "b"]
    assert write_spy.count == 1
```

- [ ] **Step 2: implement**

```python
# raven/rpc/methods/model.py
_SETTABLE_FIELDS = {"api_base", "deployment", "api_version", "extra_headers"}

async def model_set_fields(params: dict) -> dict:
    parsed = _parse(ModelSetFieldsParams, params)
    bad = set(parsed.fields) - _SETTABLE_FIELDS
    if bad:
        raise ConfigValidationError(f"fields not settable here: {sorted(bad)}", data={"slug": parsed.slug})
    fields = dict(parsed.fields)
    if "extra_headers" in fields:
        current = await asyncio.to_thread(_current_headers, parsed.slug)  # raw section, unmasked
        for name, value in fields["extra_headers"].items():
            if value is None:
                current.pop(name, None)
            else:
                current[name] = value
        fields["extra_headers"] = current
    previous = await asyncio.to_thread(set_provider_fields, parsed.slug, fields)
    if "extra_headers" in previous:
        previous["extra_headers"] = _redact_headers(previous["extra_headers"])
    return {"previous": previous}

async def model_add_models(params: dict) -> dict:
    parsed = _parse(ModelAddModelsParams, params)
    await asyncio.to_thread(add_provider_models, parsed.slug, list(dict.fromkeys(parsed.models)))
    return {"provider": await _entry_off_loop(parsed.slug, current_provider)}
```

`add_provider_models` is `add_provider_model`'s loop body run once inside one
`atomic_update`: append every id whose `merge_key` is not present, write once.
In `add_provider_model`'s overlay merge, a stated `""` for `label` or
`description` pops that field instead of storing it.

- [ ] **Step 3: run and prove it can fail**

Run: `uv run pytest tests/test_rpc_model.py tests/test_rpc_schema_match.py -q -p no:cacheprovider`
Expected: all pass (copy).
Mutation: replace the header merge with assignment -> the patch test fails on the second header; drop the `""` pop -> the clear test fails; restore.

- [ ] **Step 4: record.**

## Task 8: provider OAuth from the page

**Delivers**: A13.

**Files**:
- new: `raven/providers/oauth_login.py` (the device-flow starter shared by the three vendors)
- modify: `raven/rpc/methods/model.py` (`model_oauth_login`, registration)
- modify: `rpc-schema/openrpc.json`, `raven/rpc/models.py` (`ModelOauthLoginParams`, `ModelOauthLoginResult`)
- tests: `tests/test_rpc_model.py`, `tests/test_providers_oauth_login.py`

**Interfaces**:
- produces: `model.oauth_login {slug}` -> `{verification_uri, user_code, expires_in}`; `model.options` reports `authenticated: true` once the token lands.

- [ ] **Step 1: real dependency first (spike S1, C17)**

For each of `minimax_global`, `openai_codex`, `github_copilot`, run the
existing login path from Python with its console replaced by a capture and
record whether a URL and a code come back before the flow blocks on the
person:

Run: `uv run python -c "from raven.providers.minimax_oauth import _login_locked, oauth_config; ..."` (the exact call after reading `_login_locked`'s signature at line 247); the same for the Codex driver in `raven/providers/chatgpt_token.py` and the Copilot flow in `raven/cli/provider_commands.py:181`.
Expected: three lines of the form `<slug>: url=<...> code=<...> blocks_after=yes`, copied. A vendor whose flow cannot yield the pair without a console is recorded in the deviations ledger (fallback tier: that vendor keeps the CLI instruction on the page) and skipped below.

- [ ] **Step 2: write the tests**

```python
async def test_oauth_login_returns_the_pair_and_finishes_in_background(dispatch, fake_device_flow):
    r = await dispatch("model.oauth_login", {"slug": "minimax_global"})
    assert r == {"verification_uri": fake_device_flow.uri, "user_code": fake_device_flow.code, "expires_in": 600}
    fake_device_flow.approve()
    await fake_device_flow.settled()
    assert token_path("global").exists()

async def test_oauth_login_drops_the_attempt_when_the_code_expires(dispatch, fake_device_flow):
    fake_device_flow.expires_in = 1
    await dispatch("model.oauth_login", {"slug": "minimax_global"})
    await asyncio.sleep(1.5)
    assert not token_path("global").exists()
    assert oauth_login.pending() == {}
```

- [ ] **Step 3: implement**

```python
# raven/providers/oauth_login.py
"""Start a vendor's device-code login without a console and finish it in the
background, so a page can show the URL and the code and wait for the token."""

_PENDING: dict[str, asyncio.Task] = {}

async def start(slug: str) -> dict:
    starter = _STARTERS[slug]          # one adapter per vendor from the spike
    handoff: asyncio.Future = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(asyncio.to_thread(starter, handoff))
    _PENDING[slug] = task
    task.add_done_callback(lambda _t: _PENDING.pop(slug, None))
    return await asyncio.wait_for(handoff, 30)   # {"verification_uri", "user_code", "expires_in"}

def pending() -> dict[str, asyncio.Task]:
    return dict(_PENDING)
```

Each starter calls the vendor flow with a `print_fn` / hook that resolves
`handoff` on the first line carrying the URL and code (the shapes recorded in
step 1), then keeps polling until the vendor's `expires_in` passes, after
which it returns without writing a token.

```python
# raven/rpc/methods/model.py
async def model_oauth_login(params: dict) -> dict:
    parsed = _parse(ModelOauthLoginParams, params)
    spec = find_by_name(parsed.slug)
    if not spec or not spec.is_oauth:
        raise NotSupportedError(f"{parsed.slug} does not use OAuth", data={"slug": parsed.slug})
    from raven.providers import oauth_login
    return await oauth_login.start(parsed.slug)
```

- [ ] **Step 4: run and prove it can fail**

Run: `uv run pytest tests/test_providers_oauth_login.py tests/test_rpc_model.py -q -p no:cacheprovider -k oauth`
Expected: all pass (copy).
Mutation: remove the expiry stop -> the expiry test fails on `pending() == {}`; restore.

- [ ] **Step 5: record** the spike's three lines and any vendor that fell back.

## Task 9: plugin status fields, retry, revoke, configure

**Delivers**: A34, A35 (the credentialed half), A36, A37, A38, A39.

**Files**:
- modify: `raven/rpc/methods/console.py` (`ext_list` MCP rows at lines 118-152 gain `auth`, `credentialed`)
- modify: `raven/market/connect.py` (new `retry(name, loop)`, `revoke(name, loop)`, `configure(name, form, loop)`)
- modify: `raven/rpc/methods/plughub.py` (three handlers, registrations)
- modify: `rpc-schema/openrpc.json`, `raven/rpc/models.py` (`ExtListMcpRow` fields; `PlugRetryParams`, `PlugRevokeParams`, `PlugConfigureParams`, results)
- tests: `tests/test_rpc_plughub.py`, `tests/test_rpc_console.py`

**Interfaces**:
- consumes: `read_ledger`, `read_ledgers` (`raven/market/ledger.py`), `catalog_detail` (`raven/market/catalog.py`), `entry_auth_mode`, `entry_required_fields` (`connect.py:69,77`), `_build_mcp_config` (`install.py:86`), `has_stored_tokens`, `delete_credentials` (`raven/mcp/oauth.py:288,302`), `manager.connect(name, cfg, executor_provider=loop.mcp_executor_provider)` (`mcp/manager.py:297`), `kick_sync`.
- produces: `ext.list` MCP rows with `auth: "oauth" | "apikey" | "none"`, `credentialed: bool`; `plug.retry {name}`, `plug.revoke {name}`, `plug.configure {name, form}` -> `{name, mcp}`. Task 17 reads them.

- [ ] **Step 1: write the tests**

```python
async def test_ext_list_reports_auth_and_credential_state(dispatch, mcp_fixture):
    mcp_fixture.catalog_server("github", auth="apikey", field_into="headers.Authorization", value="")
    mcp_fixture.catalog_server("asana", auth="oauth", token=False)
    mcp_fixture.hand_written("local")
    rows = {r["name"]: r for r in (await dispatch("ext.list", {}))["mcp"]}
    assert (rows["github"]["auth"], rows["github"]["credentialed"]) == ("apikey", False)
    assert (rows["asana"]["auth"], rows["asana"]["credentialed"]) == ("oauth", False)
    assert (rows["local"]["auth"], rows["local"]["credentialed"]) == ("none", True)

async def test_retry_calls_connect_for_the_one_server(dispatch, mcp_fixture, manager_spy):
    mcp_fixture.hand_written("dead", state="error")
    await dispatch("plug.retry", {"name": "dead"})
    assert manager_spy.connect_calls == ["dead"]

async def test_revoke_deletes_the_token_and_disconnects(dispatch, mcp_fixture, manager_spy):
    mcp_fixture.catalog_server("asana", auth="oauth", token=True)
    await dispatch("plug.revoke", {"name": "asana"})
    assert not credentials_path("asana").exists()
    assert manager_spy.disconnect_calls == ["asana"]

async def test_configure_rewrites_only_the_templated_field(dispatch, mcp_fixture, config_file):
    mcp_fixture.catalog_server("github", auth="apikey", field_into="headers.Authorization", value="Bearer old", extra={"url": "https://x/mcp"})
    await dispatch("plug.configure", {"name": "github", "form": {"token": "new"}})
    srv = config_file.read()["tools"]["mcpServers"]["github"]
    assert srv["headers"]["Authorization"] == "Bearer new" and srv["url"] == "https://x/mcp"
    with pytest.raises(ConfigValidationError):
        await dispatch("plug.configure", {"name": "local", "form": {}})
```

- [ ] **Step 2: implement**

```python
# raven/market/connect.py
async def retry(name: Any, loop) -> dict:
    server = server_name(name)
    manager = getattr(loop, "mcp_manager", None)
    cfg = load_config().tools.mcp_servers.get(server)
    if manager is None or cfg is None:
        raise PlugConnectError(f"'{server}' is not configured")
    await manager.connect(server, cfg, executor_provider=loop.mcp_executor_provider)
    return {"name": server, "mcp": installed_overview(loop)}

async def revoke(name: Any, loop) -> dict:
    server = server_name(name)
    from raven.mcp.oauth import delete_credentials
    delete_credentials(server)
    manager = getattr(loop, "mcp_manager", None)
    if manager is not None:
        await manager.disconnect(server)
    return {"name": server, "mcp": installed_overview(loop)}

async def configure(name: Any, form: Any, loop) -> dict:
    server = server_name(name)
    if read_ledger(server) is None:
        raise PlugConnectError(f"'{server}' was not installed from the catalog")
    entry = await catalog_detail(server)
    contrib = next(c for c in entry["contributes"] if c.get("kind") == "mcp")
    rendered = _build_mcp_config(contrib, dict(form or {}))
    def _patch(current):
        payload = _parse_config(current); servers = _servers(payload)
        for bucket in ("headers", "env"):
            for k, v in (rendered.get(bucket) or {}).items():
                servers[server].setdefault(bucket, {})[k] = v
        return _dump_config(payload), None
    atomic_update(_config_path(), _patch)
    return {"name": server, "mcp": await kick_sync(loop, focus=server)}
```

`ext_list`: with `ledgers = read_ledgers()`, for each configured server
`entry = await catalog_detail(name) if name in ledgers else None`;
`auth = entry_auth_mode(entry) if entry else "none"`; `credentialed` is
`has_stored_tokens(name)` for oauth, the non-emptiness of the value at the
`into` path of the entry's required field for apikey, `True` for none.

- [ ] **Step 3: run and prove it can fail**

Run: `uv run pytest tests/test_rpc_plughub.py tests/test_rpc_console.py -q -p no:cacheprovider`
Expected: all pass (copy).
Mutation: make `configure` write the whole `rendered` dict -> the "only the templated field" test fails on `url`; restore.

- [ ] **Step 4: record.**

## Task 10: contract regeneration and schema parity

**Delivers**: C6 for every backend task; A53's mutation list.

**Files**: `rpc-schema/openrpc.json`, `raven/rpc/models.py`, `ui-web/src/rpc/generated.ts`.

- [ ] **Step 1: regenerate and check**

Run: `cd ui-web && npm run -s gen && npm run -s gen:check`
Expected: `wrote src/rpc/generated.ts (184 methods, ...)` then `generated.ts matches the contract (184 methods)` (178 + 6; copy the real line).

Run: `uv run pytest tests/test_rpc_schema_match.py tests/test_rpc_contract_shapes.py -q -p no:cacheprovider`
Expected: all pass (copy).

- [ ] **Step 2: the mutation list for T9.5**: write `docs/plans/mutations.txt` is not committed; instead the acceptance report lists, per new or changed handler, the one-line patch that reverts it and the test that went red. Record here the eleven handler names: `settings_set` (each new key), `settings_usage`, `session_list`, `session_archive`, `_action_inspect`, `_action_open`, `model_set_fields`, `model_add_model`, `model_add_models`, `model_oauth_login`, `ext_list`, `plug_retry`, `plug_revoke`, `plug_configure`, the catalog's `_is_blocked`.

## Task 11: the settings domain skeleton

**Delivers**: C1, C2, C3, C5 for the domain; groundwork for A1-A48.

Precondition (C20): if `refactor/ui_web_conventions` has landed, rebase first
(`git -C /Users/admin/Raven-b fetch github && git -C /Users/admin/Raven-b rebase github/refactor/ui_web_architecture`), read its rename mapping and use those names. If it has not landed by the time Tasks 1-10 are done, proceed with the names below and record C20's fallback in the ledger.

**Files** (all new unless noted, under `ui-web/src/features/settings/`):
- `types.ts` (modify: the `SettingsSnapshot` and `SettingsSource` shapes below replace the current ones)
- `store.ts` (rewrite)
- `source.ts` (rewrite)
- `wire.ts` (new; `chrome.ts` stays until Task 19 deletes it)
- `SettingsApp.tsx`, `fields.tsx`, `pages/*.tsx` empty shells that render their title, `providers/*.tsx` shells
- `store.test.ts`, `source.test.ts`
- modify: `ui-web/src/main.tsx` lines 26, 33, 156, 194 (import `SettingsApp` from `./features/settings/SettingsApp`, `wire` instead of `chrome`)
- modify: `i18n/messages.json` (the `gui.settings.*` keys the shells use)
- modify: `ui-web/src/styles/page.css` (the `settings-` block copied from the prototype stylesheet, classes prefixed)

**Interfaces**:
- produces (read by Tasks 12-18):

```ts
// store.ts
export interface SettingsState {
  snap: SettingsSnapshot | null
  section: SectionId            // 'general' | 'usage' | 'model' | 'skills' | 'tools' | 'plugins' | 'archive' | 'about'
  provider: string | null       // open provider detail
  drawer: { slug: string; mode: 'add' | 'list' } | null
  skill: string | null          // open skill detail
  usage: ResultOf<'settings.usage'> | null
  archived: ResultOf<'session.list'>['sessions'] | null
  detail: ResultOf<'skills.manage'>['info'] | null
  busy: Set<string>             // keys of writes in flight, for "connecting" chips
}
export function get(): SettingsState
export function set(next: SettingsState): void
export function subscribe(fn: () => void): () => void
export function _resetForTests(): void
export function redraw(): void   // set(get()) through flushSync
export function patch(p: Partial<SettingsState>): void

// source.ts
export const settingsSource: SettingsSource   // load(), and one function per write named after the RPC
export async function load(): Promise<SettingsSnapshot>
export async function setKey(key: string, value: unknown): Promise<void>      // settings.set; toasts warning || saved
export async function usage(from: string, to: string): Promise<ResultOf<'settings.usage'>>
export async function archived(): Promise<...>; restore(id); remove(id)
export async function inspectSkill(name); openSkillFile(name, file); uninstallSkill(id)
export async function saveKey(slug, key, base); setFields(slug, fields); disconnect(slug)
export async function fetchModels(slug); addModels(slug, ids); addModel(slug, id, label?, description?); removeModel(slug, id)
export async function oauthLogin(slug): Promise<{ verification_uri: string; user_code: string }>
export async function everosSet(section, model, borrowFrom); everosClear(section)
export async function pickModel(value, provider)   // config.set model
export async function toggleServer(name, on); retryServer(name); revokeServer(name); configureServer(name, form); authServer(name)
export function version(): string | null; checkUpdate(): Promise<void>; upgrade(): void   // injected by wire.ts
```

- [ ] **Step 1: write the store test**

```ts
import * as store from './store'
test('store exposes the four names and notifies on set', () => {
  store._resetForTests()
  let n = 0; const off = store.subscribe(() => n++)
  store.set({ ...store.get(), section: 'usage' })
  expect(store.get().section).toBe('usage'); expect(n).toBe(1)
  store.redraw(); expect(n).toBe(2)
  off()
  expect(typeof store._resetForTests).toBe('function')
})
```

- [ ] **Step 2: implement the store**

```ts
import { flushSync } from 'react-dom'
const initial = (): SettingsState => ({ snap: null, section: 'general', provider: null, drawer: null, skill: null, usage: null, archived: null, detail: null, busy: new Set() })
let state = initial()
const listeners = new Set<() => void>()
export function get(): SettingsState { return state }
export function set(next: SettingsState): void { state = next; flushSync(() => { for (const fn of listeners) fn() }) }
export function subscribe(fn: () => void): () => void { listeners.add(fn); return () => { listeners.delete(fn) } }
export function _resetForTests(): void { state = initial(); listeners.clear() }
export function redraw(): void { set({ ...state }) }
export function patch(p: Partial<SettingsState>): void { set({ ...state, ...p }) }
```

- [ ] **Step 3: implement source.ts and wire.ts**

`source.ts` keeps today's `loadSettings` / `loadEveros` / `loadPermMode` /
`settingsSnapshot` bodies, replaces `toast(t('gui.set.saved'))` with the
`r.warning || t('gui.settings.saved')` shape, adds one exported function per
line of the interface above (each: `gateway().call(...)`, reload the read that
changed, `store.patch`), and absorbs from `chrome.ts` the version check
(`system.version {check: true}`) and the session-scoped `permissions.mode`
write. `wire.ts` is `chrome.ts` minus those two calls: it keeps
`setChipPainter`, `setSettingsChrome`, `sources.settings = settingsSource`,
`sources.tier`, `sources.model`, `beforeSend`, `chip.install()`, and injects
`askUpgrade` from `app/updates.ts` into the source's `upgrade` slot (the one
`app/` import the domain has, in the wiring file the note designates).
Nothing in the domain imports `features/registry.ts` or `../model/store`.

- [ ] **Step 4: implement SettingsApp.tsx**

```tsx
export function SettingsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  useEffect(() => { void settingsSource.load().then((snap) => store.patch({ snap })) }, [])
  const navHost = document.getElementById('snavList')
  return (
    <>
      {navHost && createPortal(<Nav section={s.section} />, navHost)}
      <div className="settings-panel" data-section={s.section}>
        {s.snap ? <Page section={s.section} s={s} /> : <Skeleton />}
      </div>
    </>
  )
}
```

`Nav` renders eight `<button className="settings-nav-item">` with `t('gui.settings.nav.<id>')`; `Page` switches on `s.section` to the eight page components.

- [ ] **Step 5: run and prove it can fail**

Run: `cd ui-web && npx vitest run src/features/settings && npm run -s type-check`
Expected: the store and source tests pass; type-check silent (copy).
Mutation: make `set` skip the listener loop -> the store test fails on `n`; restore.

- [ ] **Step 6: record** C20's state (landed or fallback).

## Task 12: General and About pages

**Delivers**: A1, A2, A3, A46, A47, A48.

**Files**: `pages/General.tsx`, `pages/About.tsx`, their tests; `fields.tsx` (segmented pick, switch, row, card, copy button); `i18n/messages.json` keys `gui.settings.general.*`, `gui.settings.about.*`.

**Interfaces**: consumes `settingsSource.setKey`, the look store and notifications module (existing), `version()`, `checkUpdate()`, `upgrade()`.

- [ ] **Step 1: tests**: General renders three rows from the snapshot and the language pick calls `setKey('language', 'en')`; About renders the version and the two paths and `checkUpdate` flips the row to "up to date" or "vX available" from a fixture reply.
- [ ] **Step 2: implement** per the design's page contract. Theme and notifications call the existing modules exactly as `LookPage` / `NotifyPage` do today (lift those two handlers, rename classes).
- [ ] **Step 3: run**: `npx vitest run src/features/settings/pages/General src/features/settings/pages/About`; mutation: point the language pick at the wrong key -> the test fails on the call arguments.
- [ ] **Step 4: record.**

## Task 13: Usage page

**Delivers**: A4-A8.

**Files**: `pages/Usage.tsx`, its test; fixture reply for `settings.usage` with `daily` in `rpc/fixtures/settings.ts`.

- [ ] **Step 1: tests**: range buttons call `usage(from, to)` with the right dates (7 days = today and six before); custom inputs clamp to 90 days back and refuse `from > to` before calling; the bar list has one entry per `daily` row; the model table shows "no price" for `cost_usd: null`; the cache-write column appears only when some row has `cache_write_tokens`.
- [ ] **Step 2: implement** with `<input type="date">` (native, uncontrolled) and the prototype's `.settings-bars` markup.
- [ ] **Step 3: run** and mutate (drop the clamp -> the clamp test fails).
- [ ] **Step 4: record.**

## Task 14: Model page, the shared picker, the vendor table

**Delivers**: A9-A23.

**Files**:
- new: `providers/Providers.tsx`, `providers/ProviderDetail.tsx`, `providers/Roles.tsx`, `pages/Model.tsx`, tests
- new: `ui-web/src/components/ModelPicker.tsx` (+ test); `ui-web/src/features/model/vendors.ts` (the mark table lifted from `components/ProviderMark.tsx:86-212`, re-exported through `features/model/types.ts`)
- modify: `components/ProviderMark.tsx` imports the table from `features/model/vendors.ts`
- fixtures: `model.options` rows with `key_url`, `model.set_fields`, `model.add_models`, `model.oauth_login`, `settings.everosSet` in `rpc/fixtures/model.ts` and `settings.ts`

**Interfaces**:
- `ModelPicker` props: `{ anchor: HTMLElement; providers: Array<{ id: string; name: string; models: string[]; labels?: Record<string, { label?: string; description?: string; context_window?: number }> }>; current: { provider: string; model: string } | null; onPick(provider: string, model: string): void; onAddTyped?(provider: string, id: string): void; onClose(): void }`. Settings passes the providers a role may use; the composer will pass its own.
- Roles table (eleven rows) as data:

```ts
const ROLES: Role[] = [
  { id: 'chat', write: (p, m) => pickModel(m, p) },
  { id: 'curator', keys: ['context.curatorModel', 'context.curatorProvider'] },
  { id: 'gate', keys: ['skillForge.llmGateModel', 'skillForge.llmGateProvider'] },
  { id: 'title', keys: ['sessionTitle.model', 'sessionTitle.provider'] },
  { id: 'memllm', everos: 'llm' }, { id: 'embedding', everos: 'embedding', optional: true },
  { id: 'rerank', everos: 'rerank', optional: true }, { id: 'multimodal', everos: 'multimodal', optional: true },
  { id: 'image', media: 'image', tool: 'image_generate' }, { id: 'speech', media: 'speech', tool: 'text_to_speech' },
  { id: 'video', media: 'video', tool: 'video_generate' },
]
```

A role with `keys` writes both through `setKey`; `everos` writes `everosSet(section, model, provider)`; `media` writes `setKey('tools.media.<kind>', { model })` then `setKey('tools.disabledTools', without(tool))`; clearing writes null / `everosClear` / `{ model: '' }` then re-adds the tool. Providers a role may use: media -> `openrouter` only; everos -> `pv.on && pv.kind !== 'oauth' && pv.kind !== 'local'`; others -> `pv.on`.

- [ ] **Step 1: tests** (one per A9-A23; the guards A14 and A15 assert no source call and the role names in the message; A17 asserts a one-header patch per call; A22 asserts the two-call order).
- [ ] **Step 2: implement** the four components and the picker per the design's page contract; Azure's add block calls `saveKey` then `setFields({deployment, api_version})`.
- [ ] **Step 3: run**: `npx vitest run src/features/settings/providers src/components/ModelPicker src/features/settings/pages/Model && npm run -s type-check`; mutation: remove the roles-in-use guard -> A14's test fails.
- [ ] **Step 4: record.**

## Task 15: Skills page

**Delivers**: A24-A28.

**Files**: `pages/Skills.tsx` (list + detail), its test; fixture replies for `skills.manage inspect` (with `install`) and `open` in `rpc/fixtures/skillhub.ts` or `ext.ts` (whichever answers `skills.manage` today).

- [ ] **Step 1: tests**: split by `source === 'builtin'`; hub row shows uninstall; the switch writes the whole blocklist through `setKey('skillForge.blocklist', [...])`; the detail renders `body` through the existing markdown renderer the transcript uses (`lib/` or `components/`; do not write a second one), lists `files`, shows `install` fields with the trigger mapped `use_skill -> in conversation`, `auto_inject -> automatic`, `rpc -> from a client`, and hides the install line when `install` is null.
- [ ] **Step 2: implement.**
- [ ] **Step 3: run** and mutate (write the list without the toggled name -> the switch test fails).
- [ ] **Step 4: record.**

## Task 16: Tools page and the group table

**Delivers**: A29-A33.

**Files**: `toolGroups.json`, `pages/Tools.tsx`, its test; `tests/test_settings_tool_groups.py`.

```json
{ "file": ["read_file","write_file","edit_file","list_dir","grep","find","deliver_files"],
  "run": ["exec"], "net": ["web_search","web_fetch","deep_research"],
  "generate": ["image_generate","text_to_speech","video_generate"],
  "collab": ["message","spawn","run_subagent_dag","ask_user","cron","plugin"],
  "skills": ["read_skill","find_skill","use_skill","load_playbook","create_playbook"],
  "memory": ["understand_media"], "search": ["tool_search","tool_call"] }
```

```python
def test_tool_groups_cover_the_default_tools_exactly():
    groups = json.loads(Path("ui-web/src/features/settings/toolGroups.json").read_text())
    listed = {t for names in groups.values() for t in names}
    registered = default_tool_names()   # the names AgentLoop._register_default_tools registers (raven/agent/loop/wiring.py:789), collected on a bare loop
    assert listed == registered
```

- [ ] **Step 1: tests**: the pytest above; the page test asserts eight cards, the `search` group rendered `aria-disabled` with no switch, the counter over switchable rows, the "needs setup" badge rules from the snapshot, the web vendor select + key row writing the two keys.
- [ ] **Step 2: implement.**
- [ ] **Step 3: run**: `uv run pytest tests/test_settings_tool_groups.py -q` and the vitest; mutation: remove `exec` from the JSON -> the pytest fails on the set difference.
- [ ] **Step 4: record.**

## Task 17: Plugins page

**Delivers**: A34-A39.

**Files**: `pages/Plugins.tsx`, its test; fixtures for `plug.retry`, `plug.revoke`, `plug.configure` and `ext.list` rows with `auth`/`credentialed` in `rpc/fixtures/plughub.ts` and `ext.ts`.

- [ ] **Step 1: tests**: chip mapping (off -> none; on and `state === 'connected'` -> connected; `busy` -> connecting; `auth !== 'none' && !credentialed` -> needs setup; `state === 'error'` -> failed with retry); toggle on with `!credentialed` opens the panel and calls `toggleServer` but not `retryServer`; the apikey panel calls `configureServer(name, {token})` and `configureServer(name, {token: ''})` on clear; the oauth panel calls `authServer` / `revokeServer`; `auth === 'none'` renders no panel.
- [ ] **Step 2: implement.**
- [ ] **Step 3: run** and mutate (map `error` to connected -> the chip test fails).
- [ ] **Step 4: record.**

## Task 18: Archive page

**Delivers**: A41-A44 (the page half).

**Files**: `pages/Archive.tsx`, its test; fixtures for `session.list {archived: true}` in `rpc/fixtures/sessions.ts`, `sessions.autoArchiveAfterDays` in `settings.ts`.

- [ ] **Step 1: tests**: rows from `archived()` with title and `updated_at`; restore calls `session.archive {archived: false}` and reloads; delete asks confirmation through the existing confirm store (`state/confirm.ts`) then calls `session.delete`; the switch writes 30 / null.
- [ ] **Step 2: implement.**
- [ ] **Step 3: run** and mutate (write 0 instead of null -> the switch test fails).
- [ ] **Step 4: record.**

## Task 19: switch over, delete the old page, prune keys

**Delivers**: A49 (mount), A50, A51, A54, C11.

**Files**:
- modify: `ui-web/src/main.tsx` (the three lines of Task 11 now point at the new files only)
- delete: `features/settings/SettingsPage.tsx`, `SettingsPage.test.tsx`, `__snapshots__/SettingsPage.test.tsx.snap`, `ImageModelPicker.tsx`, `chrome.ts`
- modify: `i18n/messages.json` (delete unreferenced `gui.set.*` keys), `ui-web/src/styles/page.css` (delete the old settings block at lines 3648-3700 and the building blocks after 3701 that no other island uses; the shared vocabulary of `check-class-namespace.mjs` says which)

- [ ] **Step 1: the two scripts** (kept under `ui-web/scripts/gates/` only if the conventions branch has a slot for them; otherwise run from the plan and pasted into the report)

```bash
# orphan gui.set.* keys
node -e '
const m=require("./i18n/messages.json").ui; const {execSync}=require("child_process");
const dead=Object.keys(m).filter(k=>k.startsWith("gui.set.")).filter(k=>{
  try{execSync(`grep -rlF "${k}" ui-web/src ui-tui/src --exclude-dir=features/settings -q`);return false}catch{return true}});
console.log(dead.join("\n"))'
```

```bash
# both locales for every new key
node -e '
const m=require("./i18n/messages.json").ui;
const bad=Object.entries(m).filter(([k,v])=>k.startsWith("gui.settings.")&&!(v.en&&v.zh)).map(([k])=>k);
if(bad.length){console.error(bad.join("\n"));process.exit(1)}console.log("locales ok")'
```

- [ ] **Step 2: delete** the files and the dead keys the first script prints; run the second.
- [ ] **Step 3: run the gates**: `cd ui-web && npm run type-check && npm test && npm run build && python3 build.py`. Expected: type-check silent; `Test Files N passed`, copied; `build.py` reports both boot goldens matched, or names the changed one, whose change is explained in the commit body (OQ5).
- [ ] **Step 4: the C8 check**: `git -C /Users/admin/Raven-b diff $(git -C /Users/admin/Raven-b merge-base HEAD github/refactor/ui_web_architecture)..HEAD --stat -- ui-web/src/App.tsx ui-web/src/state/settings.ts ui-web/src/state/lang ui-web/src/state/toast.ts ui-web/src/chrome ui-web/src/app ui-web/src/lib/notifications.ts ui-web/scripts/gates`. Expected: no output.
- [ ] **Step 5: record.**

## Task 20: fixture completeness and the offline page

**Delivers**: A49, C6.

**Files**: `ui-web/src/rpc/fixtures/settings.test.ts` (new): reads `source.ts`, extracts every `gateway().call('<name>'` literal, asserts the merged fixture map answers each.

```ts
const src = readFileSync(resolve(__dirname, '../../features/settings/source.ts'), 'utf8')
const called = [...src.matchAll(/call\('([a-z]+\.[a-zA-Z_.]+)'/g)].map((m) => m[1])
const fixtures = buildAllFixtures(env)
for (const name of new Set(called)) expect(fixtures[name as RpcMethod], name).toBeDefined()
```

- [ ] **Step 1: write and run** it; mutation: delete the `plug.retry` fixture -> the test names it.
- [ ] **Step 2: open** `dist/index.html?stub=1` through `playwright-cli` and walk the eight pages (T9.1); paste the console log (expected empty) into the report.
- [ ] **Step 3: record.**

## Task 21: the real-host acceptance run

**Delivers**: every A through the cases of the acceptance document; the report.

- [ ] **Step 1**: chapter 0 of the acceptance document, with the process hygiene of the machine rules (trap, lifetime cap, port-holder check).
- [ ] **Step 2**: run T1.1 through T9.7 in order, collecting the rings named per case into `.work_context/ui_web_settings/acceptance/` (report page plus raw outputs); the starred cases are also run, then left for the person.
- [ ] **Step 3**: any case that fails is a finding: fix, rerun that case and the cases that share its page, record in the ledger what changed.
- [ ] **Step 4**: teardown; confirm no process of this run is alive (`lsof -nP -iTCP:$PORT -sTCP:LISTEN` empty; `pgrep -f "raven serve --port $PORT"` empty).

## Task 22: rebase, wrap-up suite, PR

- [ ] **Step 1**: `git -C /Users/admin/Raven-b fetch github` and rebase onto `github/refactor/ui_web_architecture` (and the conventions branch's renames if it landed after Task 11); rerun the touched suites.
- [ ] **Step 2**: ask the operator before the full `uv run pytest`; run the full `npm test`; paste both summaries.
- [ ] **Step 3**: `/ponytail-review` over the diff; `/ponytail-debt` harvests `# ponytail:` comments into the ledger as fallback-tier rows.
- [ ] **Step 4**: the four-dimension review of `dev-workflow` `references/review-prompts.md` section 3; merge, re-verify each blocker, fix.
- [ ] **Step 5**: on the operator's word, commit series (Conventional Commits, English, ASCII, the `Co-authored-by` trailer), push over SSH with `--force-with-lease`, draft the PR body from `.github/pull_request_template.md`, run the ASCII scan, show the preview, then `gh pr create --base refactor/ui_web_architecture` on the operator's confirmation.

---

## Self-review (done before G3, 2026-09-17)

1. Coverage: A1-A3, A46-A48 -> Task 12; A4-A8 -> Tasks 4, 13; A9-A23 -> Tasks 1, 2, 7, 8, 14; A24-A28 -> Tasks 2, 3, 6, 15; A29-A33 -> Task 16; A34-A39 -> Tasks 9, 17; A41-A44 -> Tasks 5, 18; A49 -> Tasks 19, 20; A50, A51, A54 -> Task 19; A52 -> Tasks 19, 22; A53 -> Task 10; A55 -> Task 21. C1-C6 -> Tasks 11, 19, 20; C7 -> Tasks 2-9; C8 -> Task 19; C9 -> Tasks 2, 12; C10 -> Tasks 7, 9, 14, 17; C11 -> Task 19; C12 -> Task 2; C13 -> Task 3; C17 -> Task 8; C20 -> Task 11.
2. Placeholders: none of the banned phrases; every implementation step carries code or an exact reference to a design table; expected outputs that can be copied today are copied (baselines), the rest name the line to copy.
3. Names: `settingsSource.setKey`, `store.patch`, `ROLES`, `toolGroups.json`, `skill_blocklist`, `append_metadata_patch`, `add_provider_models`, `_open_with_system`, `oauth_login.start` are defined in the task that first produces them and used by the same names afterwards.
4. Ladder: Task 3 reuses the `disabled_tool_names` pattern instead of a watcher; Task 5 appends instead of scheduling; Task 7 reuses `set_provider_fields` and `add_provider_model`; Task 9 reuses `_build_mcp_config`, `kick_sync`, `delete_credentials`; Task 14 lifts the vendor table rather than writing one; Task 15 reuses the transcript's markdown renderer; Task 16 is a JSON file and one test; Task 20 is one regex over one file. No task shrank to zero.
5. Authorization and goal: present above.
6. Deviations ledger: exists, holds D1.
