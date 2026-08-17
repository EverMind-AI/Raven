# Session-scoped model selection — design

Date: 2026-07-28
Status: draft (pending spec review)

## Goal

Make the chat page's model selector a real control: list models from the user's
**raven** provider config, record the choice **per session** in raven's own
session store, and have the gateway's turn actually run on that model. Switching
sessions restores that session's model; a session without one falls back to
`agents.defaults.model`.

## Context: what is broken today

The selector is decorative. Verified chain:

1. Frontend persists the choice to AgentScope's session config —
   `sessionApi.update(sessionId, agentId, { chat_model_config })`
   ([ChatViewport.tsx:624](../../frontend/src/pages/chat/ChatViewport.tsx#L624)).
2. `RavenGatewayAgent.__init__(self, *, name, state=None, **_ignore)` swallows
   every model argument
   ([raven_gateway_agent.py:208](../../service/raven_gateway_agent.py#L208)).
3. `GatewayClient.send_turn` sends only `{session_key, content}` over
   `turn.send` ([raven_gateway_agent.py:166](../../service/raven_gateway_agent.py#L166));
   `TurnSendParams` has no model field.
4. The gateway's `AgentLoop` is built once with
   `model=config.agents.defaults.model`
   ([gateway_commands.py:228](../../../raven/cli/gateway_commands.py#L228)).

Separately, the dropdown's **candidate list** comes from AgentScope's credential
store (`useAvailableModels` -> `/credential/`), which is a different store from
the Credentials page (`/raven/providers` -> `~/.raven/config.json`). So even the
names offered are not guaranteed to be routable by raven.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Model list comes from `/raven/providers` | Single source of truth; `ProviderConfig.models` is documented as "User-curated model names for the picker" ([schema.py:318](../../../raven/config/schema.py#L318)) |
| D2 | Gateway mode only | Bridge mode is no longer maintained; `RavenBridgeAgent` is untouched |
| D3 | Per-session model lives in raven's `Session.metadata["model"]` | Free-form persisted dict ([manager.py:60](../../../raven/session/manager.py#L60)); last metadata record wins on load ([manager.py:307](../../../raven/session/manager.py#L307)). No schema change |
| D4 | No sticky default | A session keeps its own model across restarts. New sessions start on `agents.defaults.model`. No "last selected" record |
| D5 | Cross-vendor switching supported via a resolving provider | Concurrent sessions on different vendors is the whole point of per-session; also fixes a pre-existing gap (see R1) |
| D6 | Service-layer shim replaces AgentScope's `get_model` | Removes the hidden dependency on a stray AgentScope credential record existing forever |
| D7 | The global `agents.defaults.model` is never written by the web UI | Keeps cron / IM / Sentinel on a stable model; this is what makes D3 coherent |

### Rejected alternatives

- **Write `agents.defaults.model` on switch.** The gateway loads config once
  ([gateway_commands.py:144](../../../raven/cli/gateway_commands.py#L144)) with no
  watcher, so a file write alone changes nothing until restart — the same
  situation `raven.channels.set` honestly reports as
  `{"restart_required": True}` ([methods_config.py:153](../../../raven/web_rpc/methods_config.py#L153)).
  Adding hot-apply would work, but `agents.defaults.model` is global: switching a
  chat session's model would also change what Feishu/Telegram turns and cron jobs
  run on. Contradicts D7.
- **Thread the model through `turn.send` / `TurnRequest`.** Unnecessary:
  `_process_message` already holds the `Session` object
  ([main.py:1970](../../../raven/agent/loop/main.py#L1970)) before the model
  decision ([main.py:2133](../../../raven/agent/loop/main.py#L2133)), so the
  model can be read straight off it. Avoids touching the wire protocol.
- **Have the web service write the session JSONL directly.** Actively harmful,
  see R2.
- **Mirror raven providers into AgentScope credentials.** Copies API keys into a
  second store and introduces drift.

## Architecture

Four components. The raven-side surface is deliberately ~20 lines plus one new
provider class; everything else is web-side.

```
Credentials page data  ──►  GET /raven/providers            (exists)
                                      │
frontend picker  ─── useRavenModels ──┘
       │
       └─ on select / on session switch
              PUT /raven/sessions/{session_key}/model        (new, web-side proxy)
                                      │
                            raven.session.model.set          (new RPC, raven-side)
                                      │
                            agent.sessions -> Session.metadata["model"] + save()
                                      │
turn time:  _process_message reads session.metadata["model"] (raven-side, ~4 lines)
                                      │
                            effective_model  ──►  ResolvingProvider picks the vendor
```

### C1 — raven: read the session's model at turn time

`raven/agent/loop/main.py`, replacing the router block at
[main.py:2133-2141](../../../raven/agent/loop/main.py#L2133-L2141):

```python
session_model = session.metadata.get("model")
routed_model, fallback_models = session_model, []
if routed_model is None and self.router is not None:
    routed_model, fallback_models = await self.router.select_model_chain(content)
```

An explicit per-session choice beats the router's heuristic; log when it does.
Sessions with no `metadata["model"]` (IM, cron) fall through to
`effective_model = model or self.model`
([main.py:1497](../../../raven/agent/loop/main.py#L1497)), i.e.
`agents.defaults.model`. That is D7, for free.

`effective_model` is read **once** per turn, so mutating a session's model
mid-turn cannot tear that turn in half.

### C2 — raven: an in-process write entry point

Two handlers in `raven/web_rpc/methods_config.py`. `register_config_methods`
already receives `agent`
([gateway_commands.py:439](../../../raven/cli/gateway_commands.py#L439)), and
`agent.sessions` is the same `SessionManager` instance the loop uses
([gateway_commands.py:181,242](../../../raven/cli/gateway_commands.py#L181)) —
same object, same cache.

- `raven.session.model.get {session_key}` -> `{model: str | None}`
- `raven.session.model.set {session_key, model}` -> `{ok: true}`; validates
  `config.get_provider_name(model)` is not `None` **before** writing, sets
  `session.metadata["model"]`, calls `sessions.save(session)`. `model: null`
  clears the override (back to the global default).

Mirrors the existing `raven.subagents.set` idiom: validate, write, apply to the
live loop.

### C3 — web service

- `raven_config_routes.py`: `GET`/`PUT /raven/sessions/{session_key}/model`
  proxying the C2 RPCs. Gateway-only, consistent with its neighbours.
- `main.py`: when `RAVEN_GATEWAY` is on, replace
  `agentscope.app._service._chat.get_model` with a stub exposing `.model`
  (`from ._model import get_model` binds it into that module's namespace,
  [_chat.py:38](../../../../RavenX_demo/src/agentscope/app/_service/_chat.py#L38),
  so one patch covers both the primary and fallback call sites). **Assert the
  attribute exists at import time and refuse to start otherwise** — an
  agentscope upgrade must break at boot, not at the first message.
- `raven_gateway_agent.py`: stop swallowing the model argument; keep
  `getattr(model, "model", None)` for display/telemetry only. The model no
  longer travels with the turn, so `send_turn` is unchanged.

### C4 — frontend

- New `useRavenModels.ts`: `ravenProvidersApi.list()`, grouped by provider,
  `configured` providers only; a provider with an empty `models[]` contributes
  its `defaultModel`.
- `LlmSelect` takes the grouped raven data. The "add credential" item links to
  the Credentials page instead of opening AgentScope's `CreateCredentialDialog`.
- `ChatViewport`: on select and on session switch, read/write
  `/raven/sessions/web:{sessionId}/model`. The raven session key for a web
  session is `web:<agentscope session id>`
  ([raven_gateway_agent.py:231](../../service/raven_gateway_agent.py#L231)).
- `chat_model_config` keeps being written as today (sentinel
  `credential_id: 'raven'`) purely to satisfy AgentScope's non-null requirement;
  the shim makes it inert.

### C5 — raven: `ResolvingProvider`

New `raven/providers/resolving_provider.py`, modelled on the existing
[PerModelProvider](../../../raven/providers/per_model_provider.py) delegation
shape:

```
_pick(model) -> config.get_provider_name(model) -> LazyProvider cache
```

Reuses `config.get_provider_name` ([schema.py:796](../../../raven/config/schema.py#L796),
backed by `_match_provider` at [schema.py:740](../../../raven/config/schema.py#L740))
for vendor resolution and [LazyProvider](../../../raven/providers/lazy.py#L19) so
each vendor adapter is built on first use. The provider surface `AgentLoop`
actually touches is small: `chat_stream`, `chat_with_retry`, `classify_error`,
`get_default_model`.

`make_provider(config)` ([_helpers.py:92](../../../raven/cli/_helpers.py#L92))
splits: the current body becomes `build_vendor_provider(config, model)`;
`make_provider` returns the `ResolvingProvider`. Existing call sites are
unchanged. Composition with knn routing still holds:
`PerModelProvider(routing.models, fallback=ResolvingProvider(config))`.

`AgentLoop.provider` is therefore **never mutated** — vendor selection happens
per call, from the model string.

## Risks

**R1 — `litellm.api_base` is a module global.**
[litellm_provider.py:101](../../../raven/providers/litellm_provider.py#L101)
does `litellm.api_base = api_base` at construction. With several vendor adapters
coexisting they clobber each other. Every call already passes
`kwargs["api_base"]` explicitly
([litellm_provider.py:311](../../../raven/providers/litellm_provider.py#L311)), so
the global assignment is removable. `_setup_env` writes
`os.environ[spec.env_key]`, which is per-vendor and does not collide, but needs a
test pinning that.

Same root cause as a **pre-existing** gap: the router's `routed_model` and
`chat_with_retry`'s `fallback_models` already jump to other vendors' models while
carrying the startup vendor's key. C5 fixes both.

**R2 — cross-process session writes are destructive (why C2 exists).**
`get_or_create` returns the cached `Session` and never re-reads the file
([manager.py:271-273](../../../raven/session/manager.py#L271-L273)). If the web
service wrote the JSONL directly: (a) the gateway's cached object keeps the old
metadata, so the write does not take effect; (b) the gateway's next `save()`
appends a metadata record built from its **stale** in-memory dict, and last
record wins on load — silently overwriting the web service's write.

**R3 — concurrency.** `_active_turns` is keyed by session
([turn.py:172](../../../raven/rpc/methods/turn.py#L172)), so turns in
different sessions run concurrently. C5 is what makes this safe: no shared
mutable provider. Setting a session's model while that same session has a turn
in flight is benign — `effective_model` was already captured (C1).

**R4 — the new route inherits an existing exposure.**
`PUT /raven/sessions/{session_key}/model` takes `session_key` as a free
parameter on a router family with **no authentication** (no `X-User-ID`, no
`Depends`) behind `allow_origins=["*"]` / `allow_methods=["*"]`
([main.py:175-180](../../service/main.py#L175-L180)) — the same shape an
automated review flagged on `/raven/subagents/instances`. This design does not
worsen it (write is validated, no secrets returned) and does not fix it:
authenticating the whole service is a separate decision, tracked separately. Do
not paper over it with a per-route check — there is no user/ownership model in
this service to check against.

## Non-goals

- `ModelParametersPopover` (temperature, max_tokens, fallback model, TTS) stays
  cosmetic. This design does not pretend to wire it.
- Writing `agents.defaults.model` from the web UI (D7).
- Bridge mode (D2).
- The AgentScope `/credential/` store and any remaining UI for it.
- Authentication for the web service (R4).

## Testing

| What | Where | Model on |
|---|---|---|
| `ResolvingProvider._pick` dispatch + lazy build + cache | `tests/test_resolving_provider.py` | `tests/test_per_model_provider.py` |
| Coexisting adapters do not clobber `litellm.api_base` / env | same file | `tests/test_litellm_provider_*.py` |
| `raven.session.model.set` validates before writing; `null` clears; rejects unroutable | extend `tests/test_web_rpc_config.py` (AGENTS.md §5.4 — do not add a new file) | its `_FakeAgent` + `cfg_path` fixtures |
| Session metadata round-trips through save/load (last-record-wins) | `tests/test_session_manager.py` | existing |
| `metadata["model"]` beats the router; absent -> `agents.defaults.model` | `tests/test_agent_loop_model_selection.py` | new |
| Frontend gates | `pnpm -C frontend lint` and `build` | no JS unit runner |

Per AGENTS.md §6, document the `Session.metadata["model"]` key under the
**Session** entry in `CONTEXT.md`, and define `ResolvingProvider` under the
**Provider** entry (whose `_Avoid_` note already separates provider from model).

## Open items

- `ResolvingProvider` naming. `ProviderRouter` would collide with
  `routing/`'s `ModelRouter` / `KNNModelRouter`, which select *models*, not
  vendors.
- Whether the picker should visually mark which entry is
  `agents.defaults.model`, so a user can tell "this session overrides the
  default" from "this session is on the default". Resolving this as "yes" adds
  one read-only web-side route (`GET /raven/agent-defaults` -> `{model,
  provider}`, no secrets, zero raven change since that router already imports
  raven). Deliberately **not** in scope until the question is answered — with
  cross-vendor support (D5) nothing else needs it.
