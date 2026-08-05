# Session-scoped Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the chat page's model selector real — list models from raven's own provider config, store the choice per session in raven's session store, and have the gateway's turn actually run on it.

**Architecture:** The model never travels on the wire. A new RPC writes `Session.metadata["model"]` in the gateway process (the only safe place — see Global Constraint 6); `AgentLoop._process_message` reads it off the `Session` object it already holds and passes it down the existing `_run_agent_loop(model=...)` seam. A new `ResolvingProvider` picks the vendor adapter per call from the model string, so `AgentLoop.provider` is never mutated and sessions on different vendors can run concurrently.

**Tech Stack:** Python 3.12 + uv + pytest (raven core); Python 3.11 + FastAPI in conda env `ravenx` (`ui-webui/service`); React 19 + Vite + Tailwind v4 + shadcn (`ui-webui/frontend`, pnpm).

Design doc: `ui-webui/docs/specs/2026-07-28-session-scoped-model-design.md`. Decision IDs (D1-D7) and risk IDs (R1-R4) below refer to it.

## Global Constraints

1. **Gateway mode only** (D2). `RAVEN_BRIDGE` / `RavenBridgeAgent` must not be touched.
2. **Never write `agents.defaults.model` from the web UI** (D7). It stays the fallback for cron / IM / Sentinel.
3. **Two package managers, two gates.** Under `raven/` and `tests/`: `uv run pytest` only, never bare `pytest` (AGENTS.md §5.4). Under `ui-webui/`: pnpm only; gates are `pnpm -C frontend lint` (0 errors; pre-existing warnings ok) and `pnpm -C frontend build`. There is no JS unit-test runner.
4. **Do not create a new test file when one covers the module** (AGENTS.md §5.4). Specifically: extend `tests/test_web_rpc_config.py` and `tests/test_session_manager.py`; do NOT add `test_web_rpc_methods_config.py`.
5. **Comments:** English only, and only where the logic is non-obvious or a hidden constraint needs stating (AGENTS.md §1). Do not annotate edits.
6. **The web service must not write raven session files directly** (R2). `SessionManager.get_or_create` returns a cached `Session` and never re-reads the file (`raven/session/manager.py:271`), so an out-of-process write is invisible to the gateway AND gets overwritten by the gateway's next `save()` (last metadata record wins, `manager.py:307`). All session-model writes go through the RPC in Task 6. This holds even though `raven` *is* importable in the service env — `ui-webui/CLAUDE.md` says otherwise but `raven_providers_routes.py` already imports it directly; importability is not permission here.
7. **Prettier for frontend:** tabs, width 4, single quotes, semicolons, print width 100 (`.prettierrc`). A stray reformat is diff noise.
8. **i18n:** edit `ui-webui/frontend/src/i18n/locales/{en,zh}.json` with targeted text edits. Never rewrite the file via `json.dump` — it reformats compact objects.
9. **Committing is authorized for this plan** (operator, 2026-07-28), overriding the AGENTS.md §3.4 default. Each task commits with the message written in its final step. If a pre-commit hook fails, make a NEW commit; never `--amend`. Pushing and opening a PR are still NOT authorized -- they remain separate asks.
10. **Branch:** `main`, after the 83-commit P4 line was merged into it (operator, 2026-07-28). Branch name `feat/session_scoped_model`.
11. **Line numbers are navigational, not authoritative.** Every `file:line` in this plan was verified against commit `0af32c6`. The repo moved several times while this plan was written. Locate code by the symbol or code snippet quoted next to the anchor -- never by line number alone -- and if a symbol is missing where expected, stop and report rather than guessing.

---

## File Structure

**raven core (small, deliberate surface):**

| File | Responsibility | Change |
|---|---|---|
| `raven/cli/_helpers.py` | Provider construction from config | Parameterize `make_provider` / `check_provider_credentials` by model; add `make_resolving_provider` |
| `raven/providers/resolving_provider.py` | Dispatch each call to the vendor adapter the model resolves to | **New** |
| `raven/providers/litellm_provider.py` | LiteLLM vendor adapter | Drop the module-global `litellm.api_base` write (R1) |
| `raven/cli/gateway_commands.py` | Gateway bootstrap | Use `make_resolving_provider` |
| `raven/agent/loop/main.py` | Turn execution | `_process_message` prefers `session.metadata["model"]` over the router |
| `raven/web_rpc/methods_config.py` | Config-admin RPC surface | Add `raven.session.model.{get,set}` |
| `CONTEXT.md` | Domain glossary | Define `ResolvingProvider`; document the `Session.metadata["model"]` key |

**ui-webui:**

| File | Responsibility | Change |
|---|---|---|
| `ui-webui/service/raven_config_routes.py` | REST proxy to gateway RPC | Add `GET`/`PUT /raven/sessions/{session_key}/model` |
| `ui-webui/service/main.py` | App assembly | Install the `get_model` shim (D6) with a fail-fast import guard |
| `ui-webui/service/raven_gateway_agent.py` | Chat agent + WS client | Stop swallowing the `model` kwarg |
| `ui-webui/frontend/src/api/ravenSessionModel.ts` | Typed client for the new route | **New** |
| `ui-webui/frontend/src/hooks/useRavenModels.ts` | Group raven providers into picker options | **New** |
| `ui-webui/frontend/src/components/select/LlmSelect.tsx` | The picker | Accept raven-sourced groups |
| `ui-webui/frontend/src/pages/chat/ChatViewport.tsx` | Chat shell | Read/write the session model |

**Tests:** `tests/test_resolving_provider.py` (new), `tests/test_agent_loop_model_selection.py` (new), `tests/test_web_rpc_config.py` (extend), `tests/test_session_manager.py` (extend), `tests/test_litellm_provider_stream.py` (extend for R1).

---

### Task 1: Model-parameterize provider construction

`make_provider` hardcodes `config.agents.defaults.model` (`raven/cli/_helpers.py:100`), so it can only ever build the default vendor's adapter. Task 2 needs it to build *any* vendor's. One-line change each, all existing callers unaffected.

**Files:**
- Modify: `raven/cli/_helpers.py:64-70` (`check_provider_credentials`), `:92-100` (`make_provider`)
- Test: `tests/test_resolving_provider.py` (new)

**Interfaces:**
- Produces: `make_provider(config: Config, model: str | None = None) -> LLMProvider` — builds the adapter for `model`, defaulting to `config.agents.defaults.model`. `check_provider_credentials(config: Config, model: str | None = None) -> None` — same defaulting.

- [ ] **Step 1: Write the failing test**

Create `tests/test_resolving_provider.py`:

```python
"""ResolvingProvider: dispatches each call to the vendor the model resolves to."""

from __future__ import annotations

import pytest

from raven.cli._helpers import make_provider
from raven.config.schema import Config


def _config(default_model: str = "deepseek/deepseek-v3") -> Config:
    cfg = Config()
    cfg.agents.defaults.model = default_model
    cfg.providers.deepseek.api_key = "KD"
    cfg.providers.anthropic.api_key = "KA"
    return cfg


def test_make_provider_honours_explicit_model():
    cfg = _config()
    built = make_provider(cfg, "anthropic/claude-opus-4-5")
    assert built.get_default_model() == "anthropic/claude-opus-4-5"


def test_make_provider_defaults_to_config_model():
    cfg = _config()
    assert make_provider(cfg).get_default_model() == "deepseek/deepseek-v3"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resolving_provider.py -v`
Expected: FAIL — `test_make_provider_honours_explicit_model` errors with `TypeError: make_provider() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Add the parameter**

In `raven/cli/_helpers.py`, change the two signatures and their first model read:

```python
def check_provider_credentials(config: Config, model: str | None = None) -> None:
    """Fail-fast when the configured provider is missing required credentials.

    Cheap (no litellm import), so it can run at startup even when the real
    provider is built lazily. Kept in sync with the branches of make_provider.
    """
    model = model or config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)
```

```python
def make_provider(config: Config, model: str | None = None):
    """Create the appropriate LLM provider from config, for ``model`` (default:
    the configured agent default)."""
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.base import GenerationSettings
    from raven.providers.openai_codex_provider import OpenAICodexProvider

    model = model or config.agents.defaults.model
    check_provider_credentials(config, model)

    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)
```

Delete the now-duplicate `model = config.agents.defaults.model` line that followed `check_provider_credentials(config)`. Leave the rest of the body byte-identical.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_resolving_provider.py tests/test_lazy_provider.py tests/test_per_model_provider.py -v`
Expected: PASS (the two new tests, plus no regression in the two neighbours).

- [ ] **Step 5: Confirm no caller broke**

Run: `uv run pytest tests/ -k "provider or helpers or gateway" -q`
Expected: PASS. Both new params are keyword-optional, so `make_provider(config)` call sites in `gateway_commands.py:180`, `agent_commands.py:254`, and `_helpers.make_lazy_provider` are unaffected.

- [ ] **Step 6: Commit**

```bash
git add raven/cli/_helpers.py tests/test_resolving_provider.py
git commit -m "refactor(cli): let make_provider build an adapter for any model"
```

---

### Task 2: `ResolvingProvider`

**Files:**
- Create: `raven/providers/resolving_provider.py`
- Modify: `raven/cli/_helpers.py` (add `make_resolving_provider`)
- Test: `tests/test_resolving_provider.py` (extend Task 1's file)

**Interfaces:**
- Consumes: `make_provider(config, model)` from Task 1; `LazyProvider(factory, default_model, generation)` from `raven/providers/lazy.py:22`.
- Produces: `ResolvingProvider(config: Config)` with `_pick(model: str | None) -> LLMProvider` and the `LLMProvider` surface `AgentLoop` uses (`chat`, `chat_stream`, `chat_with_retry`, `get_default_model`). `make_resolving_provider(config: Config) -> ResolvingProvider`.

The delegation shape is copied from `raven/providers/per_model_provider.py`; the key is the *vendor name* rather than an explicit endpoint table, and sub-providers are `LazyProvider`-wrapped so each vendor's litellm build happens on first use.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_resolving_provider.py`:

```python
from raven.cli._helpers import make_resolving_provider
from raven.providers.resolving_provider import ResolvingProvider


def test_pick_routes_by_vendor():
    p = ResolvingProvider(_config())
    a = p._pick("anthropic/claude-opus-4-5")
    d = p._pick("deepseek/deepseek-v3")
    assert a is not d


def test_pick_memoizes_per_vendor():
    p = ResolvingProvider(_config())
    assert p._pick("anthropic/claude-opus-4-5") is p._pick("anthropic/claude-opus-4-5")


def test_pick_falls_back_to_default_vendor_for_unresolvable_model():
    p = ResolvingProvider(_config())
    assert p._pick("no-such-vendor/mystery") is p._pick(None)


def test_default_model_is_the_config_default():
    assert ResolvingProvider(_config()).get_default_model() == "deepseek/deepseek-v3"


@pytest.mark.asyncio
async def test_chat_with_retry_delegates_to_the_models_vendor():
    p = ResolvingProvider(_config())
    picked = p._pick("anthropic/claude-opus-4-5")
    calls: list[str | None] = []

    async def _spy(messages, tools=None, model=None, **kw):
        calls.append(model)
        return "ok"

    picked.chat_with_retry = _spy
    assert await p.chat_with_retry([], model="anthropic/claude-opus-4-5") == "ok"
    assert calls == ["anthropic/claude-opus-4-5"]


def test_make_resolving_provider_returns_one():
    assert isinstance(make_resolving_provider(_config()), ResolvingProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resolving_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'raven.providers.resolving_provider'`.

- [ ] **Step 3: Write the implementation**

Create `raven/providers/resolving_provider.py`:

```python
"""Provider that dispatches each call to the vendor adapter its model resolves to.

Per-session model selection means two concurrent turns can want two different
vendors, so a single baked-in adapter (whose api_key / api_base are fixed at
construction) is not enough. This resolves the vendor per call from the model
name via ``Config.get_provider_name``, so ``AgentLoop.provider`` never has to be
swapped -- which would tear an in-flight turn in another session.

Sub-providers are LazyProvider-wrapped: a vendor's litellm build is paid on its
first actual call, not up front for every configured vendor.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, StreamDelta
from raven.providers.lazy import LazyProvider

if TYPE_CHECKING:
    from raven.config.schema import Config


class ResolvingProvider(LLMProvider):
    """Route provider calls to a per-vendor adapter, keyed by the model's vendor."""

    def __init__(self, config: "Config"):
        super().__init__()
        self._config = config
        self._by_vendor: dict[str, LLMProvider] = {}
        defaults = config.agents.defaults
        self._default_model = defaults.model
        self.generation = GenerationSettings(
            temperature=defaults.temperature,
            max_tokens=defaults.max_tokens,
            reasoning_effort=defaults.reasoning_effort,
            timeout=defaults.llm_call_timeout,
        )

    def _pick(self, model: str | None) -> LLMProvider:
        effective = model or self._default_model
        vendor = self._config.get_provider_name(effective)
        if vendor is None:
            effective = self._default_model
            vendor = self._config.get_provider_name(effective) or "_default"
        cached = self._by_vendor.get(vendor)
        if cached is not None:
            return cached
        built = self._build(effective)
        self._by_vendor[vendor] = built
        return built

    def _build(self, model: str) -> LLMProvider:
        from raven.cli._helpers import make_provider

        sub = LazyProvider(
            factory=lambda: make_provider(self._config, model),
            default_model=model,
            generation=self.generation,
        )
        return sub

    def get_default_model(self) -> str:
        return self._default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self._pick(model).chat(messages, tools, model=model, **kwargs)

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self._pick(model).chat_with_retry(messages, tools, model=model, **kwargs)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamDelta]:
        async for delta in self._pick(model).chat_stream(messages, tools, model=model, **kwargs):
            yield delta
```

Note the vendor cache is keyed by **vendor**, not model — two Anthropic models share one adapter, because `LiteLLMProvider` takes the per-call `model` kwarg anyway.

Append to `raven/cli/_helpers.py`:

```python
def make_resolving_provider(config: Config):
    """Provider that resolves each call's vendor from its model name. Used by the
    gateway, where different sessions can be on different vendors at once."""
    from raven.providers.resolving_provider import ResolvingProvider

    check_provider_credentials(config)
    return ResolvingProvider(config)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_resolving_provider.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Commit**

```bash
git add raven/providers/resolving_provider.py raven/cli/_helpers.py tests/test_resolving_provider.py
git commit -m "feat(providers): resolve a call's vendor adapter from its model name"
```

---

### Task 3: Stop the module-global `litellm.api_base` write (R1)

With several vendor adapters alive at once, `litellm.api_base = api_base` at `raven/providers/litellm_provider.py:101` makes the last-constructed adapter's base URL win process-wide. Every call already passes `kwargs["api_base"] = self.api_base` explicitly (`:311`, `:390`), so the global is redundant as well as harmful.

**Files:**
- Modify: `raven/providers/litellm_provider.py:100-101`
- Test: `tests/test_litellm_provider_stream.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_litellm_provider_stream.py`:

```python
def test_construction_does_not_set_the_litellm_module_global(monkeypatch):
    """Two adapters must not fight over litellm.api_base (R1): a per-vendor
    api_base travels per call, never process-wide."""
    import litellm

    from raven.providers.litellm_provider import LiteLLMProvider

    monkeypatch.setattr(litellm, "api_base", None, raising=False)
    LiteLLMProvider(api_key="KA", api_base="http://a/v1", default_model="a/one")
    LiteLLMProvider(api_key="KB", api_base="http://b/v1", default_model="b/two")
    assert litellm.api_base is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_litellm_provider_stream.py::test_construction_does_not_set_the_litellm_module_global -v`
Expected: FAIL — `assert 'http://b/v1' is None`.

- [ ] **Step 3: Remove the global write**

In `raven/providers/litellm_provider.py`, delete these two lines:

```python
        if api_base:
            litellm.api_base = api_base
```

Leave `self.api_base` and the per-call `kwargs["api_base"]` paths untouched.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_litellm_provider_stream.py tests/test_litellm_provider_timeout.py tests/test_litellm_provider_attribution.py tests/test_provider_stream_fallback.py tests/test_provider_fallback_chain.py -v`
Expected: PASS. If any test relied on the global, it was asserting the bug — fix the test to assert the per-call kwarg instead.

- [ ] **Step 5: Commit**

```bash
git add raven/providers/litellm_provider.py tests/test_litellm_provider_stream.py
git commit -m "fix(providers): keep api_base per call instead of on the litellm module"
```

---

### Task 4: Point the gateway at `ResolvingProvider`

**Files:**
- Modify: `raven/cli/gateway_commands.py:180`
- Test: `tests/test_cli_gateway_commands.py` (extend)

**Interfaces:**
- Consumes: `make_resolving_provider(config)` from Task 2.

Only the gateway switches. The REPL (`agent_commands.py:254`) and TUI keep `make_provider` / `make_lazy_provider` — they are single-session and per-session model is a gateway concept (D2).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_gateway_commands.py` (define a local `_config()` identical to the one in `tests/test_resolving_provider.py` from Task 1 -- two small test files each owning their fixture beats a shared conftest helper used twice):

```python
def test_gateway_provider_resolves_vendors_per_call():
    """The gateway serves many sessions at once, so the provider it ends up
    holding must resolve a vendor per call rather than bake in the default
    model's vendor. Asserted on the actual composed object, not on source text."""
    from raven.cli._helpers import make_resolving_provider
    from raven.cli.gateway_commands import build_model_routing

    cfg = _config()
    router, provider = build_model_routing(cfg, make_resolving_provider(cfg))

    assert router is None  # routing.enabled defaults False
    anthropic = provider._pick("anthropic/claude-opus-4-5")
    deepseek = provider._pick("deepseek/deepseek-v3")
    assert anthropic is not deepseek
    assert anthropic.get_default_model() == "anthropic/claude-opus-4-5"
    assert deepseek.get_default_model() == "deepseek/deepseek-v3"
```

This asserts on the object `build_model_routing` actually returns, which is
exactly what `gateway_commands.py:180-196` hands to `AgentLoop`. The remaining
gap -- that line 180 calls `make_resolving_provider` rather than something else
-- is proven end-to-end by Task 10 step 5.3 (two sessions on two vendors, both
answer). Do NOT substitute an `inspect.getsource` grep for either.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_gateway_commands.py::test_gateway_provider_resolves_vendors_per_call -v`
Expected: PASS once Tasks 1-2 are in — this test pins the composition that Step 3
then wires into the gateway. If it FAILS with `AttributeError: '_pick'`, the
provider handed in was not a `ResolvingProvider`; re-check Task 2.

- [ ] **Step 3: Switch the call**

In `raven/cli/gateway_commands.py`, add `make_resolving_provider` to the existing `from raven.cli._helpers import (...)` block (alongside `make_provider` at `:23`) and change line 180:

```python
        provider = make_resolving_provider(config)
```

`build_model_routing(config, provider)` on the next line still composes: the knn backend wraps this in `PerModelProvider(routing.models, fallback=<ResolvingProvider>)`, so an explicit routing endpoint wins and anything else falls through to vendor resolution.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_gateway_commands.py tests/test_cli_gateway_spine.py tests/test_cli_gateway_health.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add raven/cli/gateway_commands.py tests/test_cli_gateway_commands.py
git commit -m "feat(cli): give the gateway a vendor-resolving provider"
```

---

### Task 5: `_process_message` prefers the session's model

**Files:**
- Modify: `raven/agent/loop/main.py:2133-2141`
- Test: `tests/test_agent_loop_model_selection.py` (new — no existing file covers model selection; matches the `test_agent_loop_<aspect>.py` convention)

**Interfaces:**
- Produces: the contract that `Session.metadata["model"]`, when set, is the model for that session's turns; absent, the router decides; absent both, `AgentLoop.model` (i.e. `agents.defaults.model`).

`session` is already in hand at `main.py:1970`, well before the model decision — no wire or signature change is needed anywhere.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_loop_model_selection.py`. Every assertion is at the provider boundary — the model a call is actually made with — because that is where being wrong costs money. Do NOT add tests that assert on `inspect.getsource` text: they pass while the behaviour is broken and fail on harmless rewording.

```python
"""Per-session model: Session.metadata["model"] outranks the router."""

from __future__ import annotations

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _ModelRecordingProvider:
    """Records the model each call was made with. Non-streaming path."""

    def __init__(self) -> None:
        self.seen: list[str | None] = []
        self.seen_fallbacks: list[list[str] | None] = []

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.seen.append(kwargs.get("model"))
        self.seen_fallbacks.append(kwargs.get("fallback_models"))
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "fake/default"


def _stub_edges(loop: AgentLoop) -> None:
    """No-op the sandbox/MCP bring-up so a text-only turn runs without a VM."""

    async def _noop() -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop


def _req(text: str, *, conversation: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="web", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
        conversation=conversation,
    )


async def test_session_model_reaches_the_llm_call(tmp_path):
    provider = _ModelRecordingProvider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    loop.sessions.get_or_create("web:abc").metadata["model"] = "anthropic/claude-opus-4-5"

    await loop._process_message(_req("hi", conversation="web:abc"))

    assert provider.seen == ["anthropic/claude-opus-4-5"]


async def test_a_session_without_a_model_uses_the_agent_default(tmp_path):
    provider = _ModelRecordingProvider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)

    await loop._process_message(_req("hi", conversation="web:plain"))

    assert provider.seen == ["fake/default"]


class _CountingRouter:
    """Records whether the router was consulted at all."""

    def __init__(self, model: str, fallbacks: list[str]) -> None:
        self._model = model
        self._fallbacks = list(fallbacks)
        self.calls = 0

    async def select_model_chain(self, prompt: str) -> tuple[str | None, list[str]]:
        self.calls += 1
        return self._model, list(self._fallbacks)


async def test_a_session_model_suppresses_the_router_entirely(tmp_path):
    """Without this, the if/elif could regress into two independent ifs and the
    other tests would not notice, because they run with no router configured."""
    provider = _ModelRecordingProvider()
    router = _CountingRouter("router/picked", ["router/fallback"])
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default", router=router)
    _stub_edges(loop)
    loop.sessions.get_or_create("web:abc").metadata["model"] = "anthropic/claude-opus-4-5"

    await loop._process_message(_req("hi", conversation="web:abc"))

    assert provider.seen == ["anthropic/claude-opus-4-5"]
    assert router.calls == 0
    assert provider.seen_fallbacks == [[]]


async def test_the_router_still_runs_when_the_session_has_no_model(tmp_path):
    """Guards the other direction: the elif branch must stay reachable."""
    provider = _ModelRecordingProvider()
    router = _CountingRouter("router/picked", [])
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default", router=router)
    _stub_edges(loop)

    await loop._process_message(_req("hi", conversation="web:plain"))

    assert router.calls == 1
    assert provider.seen == ["router/picked"]


async def test_two_sessions_keep_their_own_models(tmp_path):
    """The isolation guarantee: one loop, two sessions, two models."""
    provider = _ModelRecordingProvider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    loop.sessions.get_or_create("web:a").metadata["model"] = "anthropic/claude-opus-4-5"
    loop.sessions.get_or_create("web:b").metadata["model"] = "deepseek/deepseek-v3"

    await loop._process_message(_req("hi", conversation="web:a"))
    await loop._process_message(_req("hi", conversation="web:b"))

    assert provider.seen == ["anthropic/claude-opus-4-5", "deepseek/deepseek-v3"]
```

`AgentLoop.__init__` takes `model=` (`main.py:323`: `self.model = model or provider.get_default_model()`), so these pin both the override and the fallback without any monkeypatching of raven internals.

If `_process_message` needs more edges stubbed in this environment than `_start_executor` / `_connect_mcp`, copy whatever `tests/test_agent_loop_run_emit.py:163` currently stubs — do not weaken the assertions to compensate.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_loop_model_selection.py -v`
Expected: FAIL — `test_session_model_reaches_the_llm_call` and
`test_two_sessions_keep_their_own_models` both assert
`provider.seen == ["anthropic/claude-opus-4-5"]` but get `["fake/default"]`,
because nothing reads the session's model yet.
`test_a_session_without_a_model_uses_the_agent_default` should PASS already —
it pins the fallback you must not break.

- [ ] **Step 3: Rewrite the router block**

In `raven/agent/loop/main.py`, replace lines 2133-2141:

```python
        # ── Model routing (EcoClaw-style) ────────────────────────────────────
        routed_model: str | None = None
        fallback_models: list[str] = []
        if self.router is not None:
            routed_model, fallback_models = await self.router.select_model_chain(content)
            if routed_model and routed_model != self.model:
                logger.info("Router: {} → {}", self.model, routed_model)
            if fallback_models:
                logger.info("Router fallback chain: {}", fallback_models)
```

with:

```python
        # ── Model selection: the session's own pick, else the router ─────────
        # A per-session model is an explicit user choice, so it outranks the
        # router's heuristic and suppresses its fallback chain.
        routed_model: str | None = session.metadata.get("model")
        fallback_models: list[str] = []
        if routed_model is not None:
            logger.info("Session model: {} → {}", self.model, routed_model)
        elif self.router is not None:
            routed_model, fallback_models = await self.router.select_model_chain(content)
            if routed_model and routed_model != self.model:
                logger.info("Router: {} → {}", self.model, routed_model)
            if fallback_models:
                logger.info("Router fallback chain: {}", fallback_models)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_loop_model_selection.py -v`
Expected: PASS (5 tests: override, fallback, router-suppressed, router-still-runs,
two-sessions-two-models). `AgentLoop` takes `router=` as a keyword arg
(`raven/agent/loop/main.py:255`) and a router only needs
`async select_model_chain(prompt) -> (str | None, list[str])`.

- [ ] **Step 5: Check for regressions across the loop suite**

Run: `uv run pytest tests/ -k "agent_loop" -q`
Expected: PASS. Sessions without `metadata["model"]` keep the old behaviour exactly.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/loop/main.py tests/test_agent_loop_model_selection.py
git commit -m "feat(agent): run a turn on the session's own model when it has one"
```

---

### Task 6: `raven.session.model.{get,set}` RPC

**Files:**
- Modify: `raven/web_rpc/methods_config.py` (new `config` kwarg + two handlers + two registrations), `raven/cli/gateway_commands.py:439` (pass `config`)
- Test: `tests/test_web_rpc_config.py` (extend — Global Constraint 4)

**Interfaces:**
- Consumes: `agent.sessions` (the live `SessionManager`; same instance the loop uses — `gateway_commands.py:181` creates it, `:242` injects it, `:439` hands `agent` to `register_config_methods`).
- Produces:
  - `register_config_methods(dispatcher, *, agent=None, cron=None, config=None)` — one new keyword-only param.
  - `raven.session.model.get {session_key: str}` -> `{"model": str | None}`
  - `raven.session.model.set {session_key: str, model: str | None}` -> `{"ok": True, "model": str | None}`. Validates first: `model` non-null and unroutable -> `ValueError` (the dispatcher surfaces it; nothing is written). `model: null` deletes the key, restoring the global default.

The `config` param is **injected, not loaded**. Calling `load_config()` inside the handler would read the operator's real `~/.raven/config.json`, making both the tests environment-dependent and the validation disagree with the `Config` the running loop was built from. `gateway_commands.py` already has `config` in scope at the call site.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_rpc_config.py`:

```python
def _model_cfg():
    """Config with two vendors keyed, so routable/unroutable both have coverage."""
    from raven.config.schema import Config

    cfg = Config()
    cfg.agents.defaults.model = "deepseek/deepseek-v3"
    cfg.providers.deepseek.api_key = "KD"
    cfg.providers.anthropic.api_key = "KA"
    return cfg


class _FakeSessions:
    """Stands in for SessionManager: get_or_create + save, cache semantics included."""

    def __init__(self) -> None:
        from raven.session.manager import Session

        self._by_key: dict[str, Session] = {}
        self.saved: list[str] = []
        self._Session = Session

    def get_or_create(self, key: str):
        if key not in self._by_key:
            self._by_key[key] = self._Session(key=key)
        return self._by_key[key]

    def save(self, session) -> None:
        self.saved.append(session.key)


@pytest.mark.asyncio
async def test_session_model_set_then_get_round_trips(cfg_path):
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.set",
            "params": {"session_key": "web:abc", "model": "anthropic/claude-opus-4-5"},
        }
    )
    assert r["result"] == {"ok": True, "model": "anthropic/claude-opus-4-5"}
    assert agent.sessions.saved == ["web:abc"]

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "raven.session.model.get",
            "params": {"session_key": "web:abc"},
        }
    )
    assert r["result"] == {"model": "anthropic/claude-opus-4-5"}


@pytest.mark.asyncio
async def test_session_model_get_is_none_when_unset(cfg_path):
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.get",
            "params": {"session_key": "web:fresh"},
        }
    )
    assert r["result"] == {"model": None}


@pytest.mark.asyncio
async def test_session_model_set_null_clears_the_override(cfg_path):
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    agent.sessions.get_or_create("web:abc").metadata["model"] = "anthropic/claude-opus-4-5"
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.set",
            "params": {"session_key": "web:abc", "model": None},
        }
    )
    assert r["result"] == {"ok": True, "model": None}
    assert "model" not in agent.sessions.get_or_create("web:abc").metadata


@pytest.mark.asyncio
async def test_session_model_set_rejects_an_unroutable_model_without_writing(cfg_path):
    """Validate before write: a bad model must not land in metadata (fail-fast)."""
    agent = _FakeAgent()
    agent.sessions = _FakeSessions()
    d = Dispatcher()
    register_config_methods(d, agent=agent, config=_model_cfg())

    r = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "raven.session.model.set",
            "params": {"session_key": "web:abc", "model": "totally-unknown-vendor/x"},
        }
    )
    assert "error" in r
    assert agent.sessions.saved == []
    assert "model" not in agent.sessions.get_or_create("web:abc").metadata
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_rpc_config.py -k session_model -v`
Expected: FAIL — the dispatcher returns a method-not-found error for `raven.session.model.get`.

- [ ] **Step 3: Write the handlers**

In `raven/web_rpc/methods_config.py`, inside `register_config_methods`, after the `raven.subagents.*` registrations (before the channels block), add:

```python
    # Per-session model. MUST live here rather than in the web service: the
    # gateway's SessionManager caches Session objects and never re-reads the
    # file, so an out-of-process write is both invisible and liable to be
    # overwritten by the next save().
    def _sessions():
        sessions = getattr(agent, "sessions", None)
        if sessions is None:
            raise RuntimeError("raven.session.model.* requires a live agent loop")
        return sessions

    async def _session_model_get(params: dict) -> dict:
        session = _sessions().get_or_create(params.get("session_key", ""))
        return {"model": session.metadata.get("model")}

    async def _session_model_set(params: dict) -> dict:
        key = params.get("session_key", "")
        model = params.get("model")
        if isinstance(model, str) and not model.strip():
            # A blank string would take the session-model branch in
            # _process_message while effective_model's truthiness check ran the
            # default -- logging one model and using another. Reject it here.
            raise ValueError("model must be a non-empty string, or null to clear")
        if model is not None:
            if config is None:
                raise RuntimeError("raven.session.model.set requires a config to validate against")
            if config.get_provider_name(model) is None:
                raise ValueError(f"No configured provider can serve model {model!r}")
        session = _sessions().get_or_create(key)
        if model is None:
            session.metadata.pop("model", None)
        else:
            session.metadata["model"] = model
        _sessions().save(session)
        return {"ok": True, "model": model}

    dispatcher.register("raven.session.model.get", _session_model_get)
    dispatcher.register("raven.session.model.set", _session_model_set)
```

And widen the signature at the top of the function:

```python
def register_config_methods(
    dispatcher: "Dispatcher", *, agent: Any = None, cron: Any = None, config: Any = None
) -> None:
```

- [ ] **Step 3b: Pass the config at the gateway call site**

In `raven/cli/gateway_commands.py:439`:

```python
                    register_config_methods(web_dispatcher, agent=agent, cron=cron, config=config)
```

`config` is the `load_runtime_config(...)` result already in scope from line 144 — the same object `AgentLoop` was built from, so validation and execution agree by construction.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_web_rpc_config.py -v`
Expected: PASS (4 new tests + the pre-existing ones).

- [ ] **Step 5: Verify metadata survives a real save/load round trip**

Append to `tests/test_session_manager.py` (extend — Global Constraint 4):

```python
def test_metadata_model_survives_a_save_load_round_trip(tmp_path):
    """metadata["model"] is the per-session model's home; the append-only file's
    last metadata record must win on reload."""
    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("web:abc")
    s.metadata["model"] = "deepseek/deepseek-v3"
    mgr.save(s)

    s.metadata["model"] = "anthropic/claude-opus-4-5"
    mgr.save(s)

    mgr.invalidate("web:abc")
    assert mgr.get_or_create("web:abc").metadata["model"] == "anthropic/claude-opus-4-5"
```

Run: `uv run pytest tests/test_session_manager.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add raven/web_rpc/methods_config.py tests/test_web_rpc_config.py tests/test_session_manager.py
git commit -m "feat(web_rpc): read and write a session's model over the config rpc"
```

---

### Task 7: REST proxy for the session model

**Files:**
- Modify: `ui-webui/service/raven_config_routes.py`

**Interfaces:**
- Consumes: `raven.session.model.{get,set}` from Task 6.
- Produces:
  - `GET /raven/sessions/{session_key}/model` -> `{"model": str | null}`
  - `PUT /raven/sessions/{session_key}/model` body `{"model": str | null}` -> `{"ok": true, "model": str | null}`; 400 on an unroutable model or transport failure.

Mounted only when the gateway is on (this router already is — `service/main.py` guards it), matching its neighbours.

- [ ] **Step 1: Add the routes**

In `ui-webui/service/raven_config_routes.py`, after the `channels` routes and before `return router`:

```python
    @router.get("/sessions/{session_key}/model")
    async def get_session_model(session_key: str) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.session.model.get", {"session_key": session_key})

    @router.put("/sessions/{session_key}/model")
    async def set_session_model(session_key: str, body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.session.model.set",
                {"session_key": session_key, "model": body.get("model")},
            )
        except Exception as exc:  # unroutable model / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 2: Verify the service imports cleanly**

Run: `conda run -n ravenx python -c "import sys; sys.path.insert(0, 'ui-webui/service'); import raven_config_routes as m; r = m.build_raven_config_router(); print([x.path for x in r.routes])"`
Expected: the printed list contains `/raven/sessions/{session_key}/model`.

- [ ] **Step 3: Smoke-test against a live gateway**

With `../start_webapp.sh` running in gateway mode (`RAVEN_GATEWAY=1`):

```bash
curl -s -X PUT localhost:8000/raven/sessions/web:smoke/model \
  -H 'Content-Type: application/json' -d '{"model":"anthropic/claude-opus-4-5"}'
curl -s localhost:8000/raven/sessions/web:smoke/model
curl -s -X PUT localhost:8000/raven/sessions/web:smoke/model \
  -H 'Content-Type: application/json' -d '{"model":"bogus-vendor/x"}' -o /dev/null -w '%{http_code}\n'
```

Expected: `{"ok":true,...}`, then `{"model":"anthropic/claude-opus-4-5"}`, then `400`.

- [ ] **Step 4: Commit**

```bash
git add ui-webui/service/raven_config_routes.py
git commit -m "feat(ui-webui): proxy the per-session model over the config routes"
```

---

### Task 8: `get_model` shim + stop swallowing the model kwarg

AgentScope's chat path requires a resolvable `chat_model_config` or it 404s (`_chat.py:465-476`), and `get_model` resolves an AgentScope *credential* — a store the Credentials page no longer writes. In gateway mode that model object is never used, so replace the resolver (D6).

**Files:**
- Modify: `ui-webui/service/main.py`, `ui-webui/service/raven_gateway_agent.py:208-212`

- [ ] **Step 1: Install the shim**

In `ui-webui/service/main.py`, after the `_bridge_cls` selection block and before `create_app(...)`:

```python
# In gateway mode the AgentScope model object is never called -- the real model
# is the session's, resolved inside raven. Replace the resolver so a chat turn
# does not require a record in AgentScope's credential store, which the
# raven-native Credentials page no longer writes.
if _flag_enabled("RAVEN_GATEWAY"):
    from agentscope.app._service import _chat as _as_chat

    if not hasattr(_as_chat, "get_model"):
        raise RuntimeError(
            "agentscope.app._service._chat.get_model is gone; the gateway model "
            "shim needs updating before the service can start"
        )

    class _RavenModelStub:
        """Duck-types the attribute AgentScope reads off a chat model."""

        def __init__(self, name: str) -> None:
            self.model = name

    async def _resolve_raven_model(user_id, config, access):
        return _RavenModelStub(getattr(config, "model", "") or "")

    _as_chat.get_model = _resolve_raven_model
```

The guard is the point: an agentscope upgrade that renames or moves `get_model` must fail at boot, not at the first message.

- [ ] **Step 2: Keep the model name on the agent**

In `ui-webui/service/raven_gateway_agent.py`, change `RavenGatewayAgent.__init__`:

```python
    def __init__(self, *, name="Raven", state=None, model=None, **_ignore):
        self.name = name or "Raven"
        self.state = state or AgentState()
        # Display/telemetry only: the model that actually runs the turn is the
        # session's, resolved inside raven (see raven.session.model.*).
        self.model_name = getattr(model, "model", None)
```

- [ ] **Step 3: Verify the service starts and a turn still runs**

Run `../start_webapp.sh restart` with `RAVEN_GATEWAY=1`, then send one chat message from the UI.
Expected: the service starts with no traceback; the turn streams a reply. Then confirm the shim is load-bearing:

```bash
conda run -n ravenx python -c "
import sys; sys.path.insert(0,'ui-webui/service')
from agentscope.app._service import _chat
print('patched:', _chat.get_model.__name__)"
```

Expected: this prints `get_model` (unpatched) when run standalone — the patch is installed by `main.py`, so verify instead in the running service's startup log that no `RuntimeError` about `get_model` was raised.

- [ ] **Step 4: Verify the fail-fast guard actually fires**

Temporarily add `del _as_chat.get_model` immediately before the `hasattr` check, restart, confirm the service refuses to start with the `RuntimeError`, then remove the line.
Expected: startup fails with the message from Step 1.

- [ ] **Step 5: Commit**

```bash
git add ui-webui/service/main.py ui-webui/service/raven_gateway_agent.py
git commit -m "feat(ui-webui): resolve the chat model inside raven in gateway mode"
```

---

### Task 9: Frontend data layer — raven-sourced models

**Files:**
- Create: `ui-webui/frontend/src/api/ravenSessionModel.ts`, `ui-webui/frontend/src/hooks/useRavenModels.ts`
- Modify: `ui-webui/frontend/src/api/index.ts` (re-export)

**Interfaces:**
- Consumes: `ravenProvidersApi.list()` (`src/api/ravenProviders.ts`) -> `{providers: RavenProviderSummary[]}`; the routes from Task 7.
- Produces:
  - `ravenSessionModelApi.get(sessionKey: string): Promise<{model: string | null}>`
  - `ravenSessionModelApi.set(sessionKey: string, model: string | null): Promise<{ok: boolean; model: string | null}>`
  - `useRavenModels(): { groups: RavenModelGroup[]; loading: boolean; refetch: () => Promise<void> }` where `RavenModelGroup = { provider: string; displayName: string; models: string[] }`

- [ ] **Step 1: Write the API client**

Create `ui-webui/frontend/src/api/ravenSessionModel.ts`:

```typescript
import { client } from './client';

export const ravenSessionModelApi = {
	get: (sessionKey: string) =>
		client.get<{ model: string | null }>(
			`/raven/sessions/${encodeURIComponent(sessionKey)}/model`,
		),
	set: (sessionKey: string, model: string | null) =>
		client.put<{ ok: boolean; model: string | null }>(
			`/raven/sessions/${encodeURIComponent(sessionKey)}/model`,
			{ model },
		),
};
```

Add `export * from './ravenSessionModel';` to `ui-webui/frontend/src/api/index.ts` next to the other raven exports.

- [ ] **Step 2: Write the hook**

Create `ui-webui/frontend/src/hooks/useRavenModels.ts`:

```typescript
import { useCallback, useEffect, useState } from 'react';

import { ravenProvidersApi } from '@/api';

export interface RavenModelGroup {
	provider: string;
	displayName: string;
	models: string[];
}

/**
 * Picker options sourced from raven's own provider config (`~/.raven/config.json`),
 * which is what actually routes a turn. Only configured providers appear; one with
 * no curated `models` contributes its registry `defaultModel` so it is still
 * selectable.
 */
export function useRavenModels() {
	const [groups, setGroups] = useState<RavenModelGroup[]>([]);
	const [loading, setLoading] = useState(false);

	const refetch = useCallback(async () => {
		setLoading(true);
		try {
			const { providers } = await ravenProvidersApi.list();
			setGroups(
				providers
					.filter((p) => p.configured)
					.map((p) => ({
						provider: p.name,
						displayName: p.displayName,
						models: p.models.length > 0 ? p.models : p.defaultModel ? [p.defaultModel] : [],
					}))
					.filter((g) => g.models.length > 0),
			);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void refetch();
	}, [refetch]);

	return { groups, loading, refetch };
}
```

- [ ] **Step 3: Run the frontend gates**

Run: `pnpm -C ui-webui/frontend lint && pnpm -C ui-webui/frontend build`
Expected: 0 eslint errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add ui-webui/frontend/src/api/ravenSessionModel.ts ui-webui/frontend/src/api/index.ts ui-webui/frontend/src/hooks/useRavenModels.ts
git commit -m "feat(ui-webui): type the session-model route and raven model groups"
```

---

### Task 10: Wire the picker to the session's model

**Files:**
- Modify: `ui-webui/frontend/src/components/select/LlmSelect.tsx`, `ui-webui/frontend/src/pages/chat/ChatViewport.tsx:577-628`, `ui-webui/frontend/src/i18n/locales/{en,zh}.json`

**Interfaces:**
- Consumes: `useRavenModels()` and `ravenSessionModelApi` from Task 9.

`chat_model_config` keeps being written exactly as today (it satisfies AgentScope's non-null requirement and the Task 8 shim makes it inert). The new call is *additional*, and it is the one that actually takes effect.

- [ ] **Step 1: Accept raven groups in the picker**

In `LlmSelect.tsx`, replace the `useAvailableModels()` call with an optional prop so the component stops owning its data source:

```typescript
	/** Raven-sourced options. When omitted the picker renders its empty state. */
	ravenGroups?: RavenModelGroup[];
	ravenLoading?: boolean;
```

Import the group type (`import type { RavenModelGroup } from '@/hooks/useRavenModels';`), drop the `useAvailableModels()` call and the `groups`/`refetch` it provided, and replace the dropdown body:

```tsx
	const handleSelect = (provider: string, model: string) => {
		onChange?.({ type: provider, credential_id: 'raven', model, parameters: {} });
	};

	const hasOptions = (ravenGroups?.length ?? 0) > 0;
```

```tsx
			<DropdownMenuContent align="start" className="min-w-48 max-h-72 overflow-y-auto">
				{!ravenLoading && !hasOptions ? (
					<div className="px-2 py-3 text-center text-sm text-muted-foreground">
						{t('llm-select.empty.title')}
					</div>
				) : (
					(ravenGroups ?? []).map((group) => (
						<DropdownMenuSub key={group.provider}>
							<DropdownMenuSubTrigger>{group.displayName}</DropdownMenuSubTrigger>
							<DropdownMenuSubContent className="max-h-72 overflow-y-auto">
								{group.models.map((model) => (
									<DropdownMenuItem
										key={model}
										onSelect={() => handleSelect(group.provider, model)}
									>
										{model}
									</DropdownMenuItem>
								))}
							</DropdownMenuSubContent>
						</DropdownMenuSub>
					))
				)}
				{allowClear ? (
					<>
						<DropdownMenuSeparator />
						<DropdownMenuItem onSelect={() => onChange?.(null)}>
							{clearLabel ?? t('llm-select.clear')}
						</DropdownMenuItem>
					</>
				) : null}
			</DropdownMenuContent>
```

`credential_id: 'raven'` is a sentinel — nothing resolves it (Task 8). Point the "add credential" item at the Credentials page instead of `CreateCredentialDialog`, or drop it: `onAddCredential` becomes dead once the AgentScope credential flow is gone, so remove the prop and its call site rather than leaving a button that opens the wrong dialog.

Check the other consumer: `src/pages/schedule/create-schedule-dialog.tsx:300` also renders `LlmSelect`. Pass it `useRavenModels()` too, so the prop is never undefined in practice.

- [ ] **Step 2: Wire ChatViewport**

Replace `useAvailableModels()` with `useRavenModels()`, pass `ravenGroups`/`ravenLoading` to `LlmSelect`, and extend the two model paths.

In `handleLlmChange` (currently at `:620-625`), add the call that actually takes effect:

```typescript
	const handleLlmChange = async (config: ChatModelConfig | null) => {
		if (!config || !sessionId || !agentId) return;
		setSelectedModel(config);
		await ravenSessionModelApi.set(`web:${sessionId}`, config.model);
		await sessionApi.update(sessionId, agentId, { chat_model_config: config });
		await refetchSessions();
	};
```

Order matters: if the raven write fails the UI must not look switched, so it goes first and its rejection propagates.

In the session-switch effect (`:577-603`), restore from raven rather than from AgentScope:

```typescript
	useEffect(() => {
		if (!view || !sessionId) return;
		let cancelled = false;
		ravenSessionModelApi
			.get(`web:${sessionId}`)
			.then(({ model }) => {
				if (cancelled) return;
				setSelectedModel(
					model ? { type: '', credential_id: 'raven', model, parameters: {} } : null,
				);
			})
			.catch(() => {});
		return () => {
			cancelled = true;
		};
	}, [sessionId, view]);
```

The `cancelled` flag matters: switching sessions fast would otherwise let a slow response for the previous session overwrite the new one's model. Leave the existing effect's `fallback`/`tts`/`knowledge`/`work_dir` restores alone; only the `chat_model_config` branch moves out.

**Critical — keep `chat_model_config` non-null.** The shim from Task 8 replaces `get_model`, but AgentScope rejects a null config *before* ever calling it:

```python
        model_cfg = session_record.config.chat_model_config
        if not model_cfg:
            raise HTTPException(status_code=404, ...)
        model = await get_model(user_id, model_cfg, self._access)
```

The old auto-pick block you are deleting was what kept it non-null. Replace that role — still in the same effect, and independent of the raven picker:

```typescript
		if (!view.session.config.chat_model_config && sessionId && agentId) {
			sessionApi
				.update(sessionId, agentId, { chat_model_config: AGENTSCOPE_MODEL_PLACEHOLDER })
				.then(() => refetchSessions())
				.catch(() => {});
		}
```

Define the placeholder once, at module scope in `ChatViewport.tsx`, so the
sentinel is named rather than inlined:

```typescript
/**
 * AgentScope rejects a turn whose session has no `chat_model_config`, and that
 * check runs before the gateway-mode `get_model` shim can intervene. This
 * satisfies it; nothing ever resolves it -- the model that runs the turn is the
 * session's, held by raven.
 */
const AGENTSCOPE_MODEL_PLACEHOLDER: ChatModelConfig = {
	type: '',
	credential_id: 'raven',
	model: 'raven',
	parameters: {},
};
```

Also delete the now-unused `getFirstAvailableModel` helper (`:510-530`) and the `groups` entry in this effect's dependency array. Verify with step 5.7 below that a brand-new session can send its first message — that is the regression this guards.

- [ ] **Step 3: Add the i18n strings**

Targeted edits only (Global Constraint 8). In `en.json` under `"llm-select"`, change `"empty"` to point at the right page and add a default marker:

```json
			"description": "Configure a provider on the Credentials page to get started."
```

Mirror the same key in `zh.json`.

- [ ] **Step 4: Run the frontend gates**

Run: `pnpm -C ui-webui/frontend lint && pnpm -C ui-webui/frontend build`
Expected: 0 eslint errors; build succeeds.

- [ ] **Step 5: End-to-end verification**

With `../start_webapp.sh` in gateway mode:

1. Open the chat page. The dropdown lists providers configured on the Credentials page (NOT AgentScope credentials).
2. Pick a model. Confirm the gateway log for the next turn prints `Session model: <default> → <picked>`.
3. Create a second session, pick a different model, send a message in each. Confirm each turn logs its own model.
4. Switch back to the first session. Confirm the picker shows its model, not the other one's.
5. Restart the gateway, reopen session one. Confirm the model persisted.
6. Confirm `~/.raven/config.json` `agents.defaults.model` is **unchanged** throughout (D7).
7. **Brand-new session, no model picked, send a message immediately.** It must answer on `agents.defaults.model` — NOT 404. A 404 here means the `chat_model_config` placeholder from Step 2 is missing or landed too late; check the service log for "No model configuration found for agent".

- [ ] **Step 6: Commit**

```bash
git add ui-webui/frontend/src/components/select/LlmSelect.tsx ui-webui/frontend/src/pages/chat/ChatViewport.tsx ui-webui/frontend/src/i18n/locales/en.json ui-webui/frontend/src/i18n/locales/zh.json
git commit -m "feat(ui-webui): drive the chat model picker from the session's raven model"
```

---

### Task 11: Domain terms and migration notes

AGENTS.md §6 requires a new domain term to be defined in the matching `CONTEXT.md` in the same change, verifiable against the code.

**Files:**
- Modify: `CONTEXT.md` (Provider and Session entries), `ui-webui/docs/MIGRATION.md`

- [ ] **Step 1: Define `ResolvingProvider`**

In `CONTEXT.md`, directly after the existing **Provider** entry (whose `_Avoid_` note already separates provider from model):

```markdown
**ResolvingProvider**:
The Provider the gateway runs on (`providers/resolving_provider.py`): it holds no
endpoint of its own and dispatches each call to the vendor adapter that
`Config.get_provider_name(model)` resolves to, memoized per vendor. Lets two
sessions on two vendors run concurrently without swapping a shared adapter.
_Avoid_: confusing it with ModelRouter / KNNModelRouter, which select a *model*;
this selects the *vendor* for an already-chosen model.
```

- [ ] **Step 2: Document the session-model key**

In `CONTEXT.md`, extend the **Session** entry with:

```markdown
A session's `metadata["model"]`, when present, is the model its turns run on; it
outranks the router and falls back to `agents.defaults.model` when absent.
```

- [ ] **Step 3: Update MIGRATION.md**

MIGRATION.md:62 currently notes that the config pages configure AgentScope's
model/credential and are "not equal to Raven's config". Append this paragraph
immediately after that note (match the file's existing prose language):

```markdown
Update (2026-07-28): the chat model picker is now Raven-native and per-session.
Options come from `/raven/providers` (`~/.raven/config.json`), and the choice is
stored in raven's own session store as `Session.metadata["model"]` via
`raven.session.model.set`. AgentScope's `chat_model_config` survives only as an
inert placeholder that satisfies the non-null check in `_chat.py`; in gateway
mode `get_model` is replaced by a stub, so no AgentScope credential record is
required for a chat turn. `agents.defaults.model` stays the fallback for
sessions with no pick, and for cron / IM / Sentinel.
```

- [ ] **Step 4: Verify the claims against the code**

Run: `uv run pytest tests/test_resolving_provider.py tests/test_agent_loop_model_selection.py -q`
Expected: PASS — the two behaviours the new glossary entries assert.

- [ ] **Step 5: Commit**

```bash
git add CONTEXT.md ui-webui/docs/MIGRATION.md
git commit -m "docs(*): define ResolvingProvider and the per-session model key"
```

---

## Not in this plan

Carried from the spec's non-goals — do not let scope creep add them:

- `ModelParametersPopover` (temperature, max_tokens, fallback model, TTS) stays cosmetic.
- Writing `agents.defaults.model` from the web UI (D7).
- Bridge mode (D2).
- Authentication for the web service (R4). `PUT /raven/sessions/{session_key}/model` lands on an unauthenticated, wildcard-CORS router family, the same shape a security review already flagged on `/raven/subagents/instances`. This plan neither worsens it (validated write, no secrets returned) nor fixes it. Do NOT bolt a per-route ownership check on: there is no user model in this service to check against. Track separately.
- Marking which picker entry is the global default (spec Open items) — needs `GET /raven/agent-defaults`, deliberately deferred.
