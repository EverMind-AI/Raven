# A2A protocol support: the host speaks it, sub-agents do not

Status: design, approved 2026-09-14.

## The gap

Raven reaches other agents today, but never on a protocol it does not control both ends
of.

`raven/acp/` serves ACP over stdio, so an editor or a parent raven can drive this process.
`raven/acp_client/` and `raven/agent/subagent/backends/` reach the other way, to a `kind:
cli` subprocess, a `kind: acp` child, or a `kind: openai` endpoint. The first two are
processes raven starts and holds. The third is genuinely remote, but `openai` is an LLM
API shape, not an agent-interoperability protocol: there is no agent identity to fetch, no
declared skills, no task that outlives a request, and no vocabulary for "I need more input
before I can continue".

A2A supplies exactly those. It is how raven talks to an agent that someone else built,
runs, and secures -- one that publishes what it can do, holds its own task state, and sits
in a different trust domain. This design adds both faces of it.

For a reader coming from the registry design of 2026-08-14: its deferred `openai` ->
`http` + `protocol` rename names A2A as a trigger, but that trigger is a second HTTP
protocol *among the agents raven launches*. A2A adds none, so the rename stays deferred on
its original terms and `raven/config/schema.py` is untouched.

## The boundary

**A2A is a face of the host agent. Sub-agents are reached over ACP and speak no A2A in
either direction.**

Serving A2A is the host answering as the agent its Card describes. Calling A2A is one tool
the host agent holds. Neither is a configured agent entity, and no sub-agent machinery is
involved on either side.

This is a boundary, not a permission setting. It forecloses a design that otherwise looks
attractive -- publishing each product in `agents/` as its own A2A endpoint -- and it is
what keeps the two protocols orthogonal instead of competing:

| | ACP | A2A |
|---|---|---|
| Direction | raven orchestrates downward | raven interoperates outward |
| Transport | stdio, process-level | HTTP, cross-host |
| Trust | host owns the child's lifetime | separate trust domain, authenticated |
| Who holds the face | host **and** sub-agent | host only |

Without it, "how do I call raven-code" would have two answers and every later feature would
have to pick one. With it, that question has one answer.

```
external A2A client --A2A--> host raven --A2A--> external A2A agent
                                 |   (the only process holding either A2A face)
                                 +--ACP--> agents/raven-code
                                 +--ACP--> agents/raven-ppt
                                 +--ACP--> agents/raven-design
                                 +--ACP--> agents/raven-oncall
                                 +--ACP--> agents/raven-research
```

The five products keep `kind: "acp"`. No A2A route to them is added.

## Protocol version: 1.0 only

A2A 1.0.0 restructures 0.3 rather than extending it, and the differences are on the wire:

| | 0.3 | 1.0.0 |
|---|---|---|
| Send | `message/send` | `SendMessage` |
| Get | `tasks/get` | `GetTask` |
| Task state | `working` | `TASK_STATE_WORKING` |
| Task envelope | on `result` | on `result.task` |
| Version header | absent | `A2A-Version` required |

The Agent Card path is **not** one of the differences: 0.3 and 1.0 both publish at
`/.well-known/agent-card.json`. The older `/.well-known/agent.json` belongs to the 2025
drafts before 0.3, and appears here only because the rejected `pya2a` still serves it.

1.0 layers the spec as a canonical data model (normatively a `.proto`), abstract
operations, and three bindings -- JSON-RPC, gRPC, HTTP/REST -- which must be functionally
identical. We implement **the JSON-RPC binding only**.

The spec defines an absent `A2A-Version` header to mean 0.3, so a 1.0-only server has to
answer header-less requests deliberately rather than by accident: we return
`VersionNotSupportedError`. No 0.3 compatibility layer is built.

## Dependency: `a2a-sdk`

**`pya2a` is rejected.** It is a 2025-04-15 package at 0.1.1, two releases total, and it
fails on four independent counts:

- it serves the pre-0.3 draft -- `tasks/send`, `tasks/sendSubscribe`,
  `tasks/pushNotification/set`, and the old `/.well-known/agent.json` -- with no
  `message/send` anywhere, so it is two protocol generations behind the target;
- it carries no `A2A-Version`, no `TASK_STATE_*`, and no `supportedInterfaces`;
- it pins `starlette>=0.27,<1.0` and `cryptography<42.0.0`. This repo floors starlette at
  1.3.1 (a `[tool.uv]` constraint, since nothing here imports starlette directly) and
  resolves cryptography at 48.0.1, so `pya2a[server]` is unsatisfiable here, by resolver
  output rather than by inspection;
- it installs into the top-level package name `a2a`, the same name the official SDK uses,
  overlapping on five paths including `a2a/__init__.py` and `a2a/client/client.py`.
  Installing both silently clobbers one with no installer error. Its own default install
  cannot be imported at all: `a2a/__init__.py` unconditionally imports `a2a.server`, which
  imports `starlette`, which the package declares only under an optional extra.

**`a2a-sdk` 1.1.2 is adopted**, with its cost stated rather than hidden. Its object model
is protobuf -- `AgentCard`, `Task` and `Message` are `a2a_pb2` messages, not Pydantic
models -- which is foreign to a repo whose config and contracts are Pydantic throughout, so
conversion happens at our boundary. Installing it adds **thirteen** entries to the lock,
not the five its own metadata lists: the direct five (`protobuf`, `google-api-core`,
`googleapis-common-protos`, `json-rpc`, `culsans`) plus `a2a-sdk` itself and the transitive
`google-auth`, `opentelemetry-api`, `proto-plus`, `pyasn1`, `pyasn1-modules`, `wrapt`,
`aiologic`. Nothing is removed or downgraded. Counting the declared dependencies alone
understates that footprint, and `opentelemetry-api` and `google-auth` in particular are not
visible in the SDK's own metadata at all.

`opentelemetry-api` is the one with a cost beyond its size: `tests/test_no_otel_tracing.py`
pins that Raven has no OpenTelemetry, and that pin fires here. It arrives through
`google-api-core`, which has required it at base since 2.36, so it is unavoidable while
depending on `a2a-sdk`. What it does not bring is the sdk or an exporter, and without an sdk
the api is inert -- `get_tracer` returns a `ProxyTracer` whose spans report
`is_recording() == False`. So the decision recorded is that the pin guards the sdk, the
exporter, and any import under `raven/`, and stops asserting which transitive packages a
vendor declares. Reversing it means dropping `a2a-sdk`.

What it buys is the part worth buying. `RequestHandler` is eleven protocol methods, already
implemented by `DefaultRequestHandler` over a task store, an event queue and a streaming
aggregator. What we implement against it is `AgentExecutor`, which is two:

```
execute(context: RequestContext, event_queue: EventQueue) -> None
cancel(context: RequestContext, event_queue: EventQueue) -> None
```

That reduces inbound A2A from "implement a protocol" to "translate an A2A task into one
raven turn".

## Transport binding: our own aiohttp routes, deliberately temporary

The SDK's server half is written for ASGI and this gateway is aiohttp, but the coupling is
optional rather than structural. `a2a/server/routes/` and `a2a/compat/v0_3/` import
starlette and fastapi directly; `a2a/utils/error_handlers.py` and `a2a/utils/proto_utils.py`
reference starlette types under `TYPE_CHECKING` with a runtime `try/except ImportError`
that falls back to `Any`. Nothing else mentions either. The four core packages --
`request_handlers`, `agent_execution`, `tasks`, `events` -- import cleanly in a venv with
no starlette, no fastapi and no grpc, which is how this was established rather than by
reading the imports.

So the binding layer is ours and the logic is not:

```
aiohttp routes (ours)        ->  RequestHandler, 11 methods (SDK)
  JSON-RPC envelope              |
  A2A-Version header             v
  /.well-known/agent-card.json   AgentExecutor, 2 methods (ours)
                                 |
                                 v
                                 one raven turn
```

`routes_aiohttp.py` is written to be deleted. The SDK ships
`add_a2a_routes_to_fastapi()`, `create_jsonrpc_routes()` and `create_agent_card_routes()`;
if the gateway ever moves to starlette/fastapi, the A2A migration is dropping that file and
calling those. No A2A logic moves.

That migration is not in this change: a protocol feature should not sit behind a whole-repo
web-stack rewrite. It is nonetheless cheaper than it looks, which is the reason to isolate
the binding rather than spread it. The server-side aiohttp surface is six files and roughly
eighty call sites, concentrated in `raven/rpc/transports/ws.py` (539 lines, 46 of them),
and that file uses only conventional API -- `web.Request`, `web.Response`,
`web.WebSocketResponse`, `web.json_response`, `web.FileResponse`, and a set of HTTP
exception classes -- each with a direct starlette equivalent. The whole ASGI stack is
already resolved in the lock (`fastapi` 0.137.1 by way of `everos`, `starlette` 1.3.1,
`sse-starlette` 3.4.1, `uvicorn` 0.46.0) while no file under `raven/` or `bridge/` imports
any of it.

Three things make that migration non-trivial when it comes, recorded so whoever does it
does not rediscover them: `ws.py` carries the browser auth bootstrap (one-time nonce,
HttpOnly SameSite=Strict cookie, Origin check, `X-Raven-Token`) and is security code, not
mechanical translation; `raven web` depends on an `AppRunner`/`TCPSite` process shape that
becomes a `uvicorn.Server`; and five other files use aiohttp as an HTTP *client*, so the
dependency stays either way.

## Module layout

Named for symmetry with the ACP pair that already exists.

```
raven/a2a/               inbound -- the server face
  card.py                Agent Card construction
  executor.py            AgentExecutor: an A2A task becomes one raven turn
  lifecycle.py           raven turn states -> TaskState; questions -> INPUT_REQUIRED
  auth.py                authenticating the caller
  asking.py              the question broker; a parked task is a turn still running
  routes_aiohttp.py      JSON-RPC dispatch + A2A-Version; deleted on migration
  runtime.py             executor + DefaultRequestHandler assembly, and the standalone site
  gate.py                sub-agent refusal, and the one module the rpc surface may import
raven/a2a_client/        outbound -- the client face
  client.py              JSON-RPC over the SDK client
  peers.py               origin -> credential lookup; the model never holds one
  tool.py                the single host-agent tool; withheld from sub-agents
```

Seating the pair in the layer machinery takes four entries, not one, and a contract can
only break on an edge it names -- so anything left out is simply unwatched. `raven.a2a`
joins the served surfaces in "the served surfaces do not import the launcher", and joins
the forbidden set of "inner layers know no surface" so no inner package may reach it.
`raven.a2a_client` joins that same contract's inner seats, beside `acp_client`. And the ws
gateway's mount is one surface hosting another, which this repo answers with a facade
rather than an allowlist: `raven/rpc/` may name `raven.a2a.gate` and nothing else, pinned
by a roster guard in `tests/test_l4_entrances.py`, exactly as `raven/acp/` reaches rpc only
through `raven.rpc.bootstrap`.

## Inbound

`DefaultRequestHandler` from the SDK, driven by our `AgentExecutor`.

### The Agent Card

Served at `/.well-known/agent-card.json`, declaring one entry in `supportedInterfaces` --
the JSON-RPC binding at `protocolVersion: "1.0"` -- plus `capabilities`, `skills` and the
`securitySchemes` the next section enforces.

`capabilities` is four fields (`streaming`, `push_notifications`, `extensions`,
`extended_agent_card`) and each is answered by what this build actually does, never
optimistically: `push_notifications` is false because it is out of scope below, and
`streaming` is true per the Streaming section.

Skills are derived from what the host can do rather than hand-written, following the rule
the registry design set for capability fields: derived in one place, because a hand-filled
field is wrong on some agent by construction.

### Authenticating the caller

An A2A endpoint is a network face for other people's agents, so it authenticates them.
The Card advertises the scheme under `securitySchemes`; the protobuf admits five
(`api_key`, `http_auth`, `oauth2`, `open_id_connect`, `mtls`), and this build implements
one -- a bearer token in the `Authorization` header, declared as `http_auth`.

The token is configured, never minted per caller: there is no enrolment flow here, and
inventing one would be a larger design than the protocol face itself. An unauthenticated
request is refused before it reaches `AgentExecutor`, so a turn is never started by an
unknown caller.

This deliberately does not reuse the `/rpc` cookie. That cookie authenticates *this user's
browser* to their own gateway, and its bootstrap mints a nonce for a human to click; an
A2A caller is a program in another trust domain with no browser and no user. Sharing one
credential between them would make either face's compromise the other's.

### Task lifecycle, and a turn that needs the user

One A2A task maps to one raven turn. `TaskState` moves `TASK_STATE_SUBMITTED` ->
`TASK_STATE_WORKING` -> `TASK_STATE_COMPLETED`, with `TASK_STATE_FAILED` on a raised turn
and `TASK_STATE_CANCELED` from `cancel()`.

The interesting state is `TASK_STATE_INPUT_REQUIRED`, and it is why this section exists.
A raven turn can ask -- that is `AskUserTool`, and over ACP it goes out as
`session/request_permission` on the caller's own wire. A2A models the same thing natively:
the task reports `INPUT_REQUIRED` and the caller sends another message against the same
task id.

The mechanism underneath is worth stating exactly, because the protocol's wording invites
the wrong one. **The turn never stops.** `AskUserTool` sets `blocking_interaction = True`
and awaits `QuestionResponder.await_question`, so the turn's coroutine stays alive holding
a future. Nothing is suspended, serialised or replayed. What the second message does is
resolve that future -- so "resuming a task", on this side, is answering a turn that was
running the whole time, and the A2A face keeps the coroutine in the background while the
HTTP request returns.

That costs nothing structural, because `QuestionResponder` is a **structural Protocol** and
every transport already brings its own broker: the TUI has one, the gateway has one, and
A2A adds a third. `AskUserTool` is untouched. The one asymmetry: when A2A is mounted on a
running gateway, that shared `AgentLoop`'s `ask_user` already carries the gateway's own
broker, and the A2A face leaves it in place rather than installing a second one, so a
question asked mid-turn there still reaches the gateway's own user, not the A2A caller.

A task in a terminal state is not restartable; a message sent against one is an error, per
the spec.

### Streaming

`SendStreamingMessage` and `SubscribeToTask` are implemented, and `capabilities.streaming`
is therefore true. The cost is small -- `DefaultRequestHandler` already aggregates the
event queue, and the aiohttp side is an SSE response -- and the alternative is that every
caller polls `GetTask` through turns that routinely run for minutes.

### Mounting

Two hostings:

- the gateway mounts it when `a2a.server.enabled` is set, sharing that port and lifecycle;
- `raven a2a serve` runs it standalone for a headless deployment, where running the command
  is itself the opt-in and the config flag is not consulted.

Onboarding switches the gateway face on, minting `a2a.server.token` in the same write. The
switch stays a real one -- a config nobody onboarded, and any `A2aConfig()` assembled
in-process, is inert -- but a finished install serves A2A rather than shipping a capability
whose first use is an edit to a JSON file.

What that opens is narrower than "a network face". The gateway binds loopback and the bind
is not configurable, so this is a local face, not an internet-facing one; every request is
checked against a freshly minted 256-bit bearer token. An empty token refuses everyone,
which is why enabling and minting are one write rather than two: an enabled face with no
credential would advertise a capability that answers nobody.

The token makes `config.json` secret-bearing in a way the switch alone would not. Nothing
narrows that file on its own and it is created under the process umask, so the minting
write fixes it to owner-only from the temp file's first byte rather than chmod-ing after
the bytes are down. It already held provider API keys; the obligation predates A2A and is
merely discharged here.

The cost lands at mount, once, not per request: a gateway that serves A2A imports
`a2a-sdk`, measured at roughly 570 ms and 391 modules on top of the gateway's own stack,
sqlalchemy and the OpenTelemetry api among them. Building the handler behind the enabled
check still keeps that off the module import path, so an operator who switches the face off
pays none of it.

## Outbound

One tool on the host agent, taking an Agent Card URL and a message. It is registered only
once a peer is configured, because its schema is not free: measured at 158 tokens, reserved
on every turn of every conversation, against a tool surface already within about fifty
tokens of the bound `tests/test_agent_loop_token_budget.py` holds. A host with no peers has
nowhere to send a message, so it was paying for a tool it could not use. Nothing becomes
unreachable -- a peer that needs no credential is still listed by origin with `credential`
empty, which sends no header -- so reaching a public agent costs one config line instead of
a per-turn tax on every install.

Configuration is a section of its own, `a2a`, holding the inbound switch and its token, and
a list of trusted peers keyed by origin. A peer entry carries an origin and its credentials
-- raven neither starts an A2A peer nor holds its lifetime, so there is nothing else to
declare.

**The model never sees a credential.** It passes a URL; the client layer matches the origin
against the trusted list and attaches what that peer's Card declares. An origin absent from
the list is called unauthenticated or refused, per that section's policy -- never an
invitation for the model to supply a secret itself.

A peer's capabilities are read from its fetched Card rather than declared in config, for
the same derive-once reason.

One limit worth stating plainly: the model learns a peer exists from the trusted-peer list
or from the user naming it in the turn. Nothing advertises a peer's skills into the turn
unprompted. Whether that should change is a question for after there are real peers to
route between.

## Errors

The SDK's error types are the vocabulary, and the mapping is fixed here so two
implementers do not each invent one:

| Condition | A2A error |
|---|---|
| Missing or unsupported `A2A-Version` | `VersionNotSupportedError` |
| Unknown task id on `GetTask` / `CancelTask` | `TaskNotFoundError` |
| Message against a terminal task | `TaskNotCancelableError` / `InvalidRequestError` per operation |
| A part type this build cannot read | `ContentTypeNotSupportedError` |
| Push-notification methods (out of scope) | `PushNotificationNotSupportedError` |
| A raven turn that raised | `InternalError`, with the cause logged and **not** returned |

The last row is the one with a rule behind it: a turn's traceback can carry file paths,
prompt fragments and tool output, and the caller is in another trust domain. The task's
status message says the turn failed; the detail stays in this host's logs.

## The gate

The boundary has two halves and they need different enforcement, because only one passes
through the tool registry.

**Outbound: the tool joins `WITHHELD_FROM_SUBAGENT`.** It is a route to another agent that
does not pass through `spawn` -- exactly the shape `load_playbook` has, which `role.py`
records as a third route to a graph beside `spawn` and `run_subagent_dag`, withheld by name
for that reason. So the A2A tool is withheld by name too,
and is not registered at all in a sub-agent process rather than hidden from the schema,
because hiding leaves a tool reachable through `tool_call`.

The cost is one frozenset entry, guarded the moment it is added:
`tests/test_agent_loop_subagent_role.py` holds that set in both directions, failing on a
withheld name nothing registers as loudly as on a withheld name the gates let through.

**Inbound: both hostings refuse to start** when `is_subagent_process()` is true, and say
why. Serving a port does not pass through the registry, so this gate is new code. The
signal is the existing `RAVEN_SUBAGENT` variable: the host merges `subagent_role_env()`
into every `kind: acp` child it launches, all five products in `agents/` are `kind: acp`,
and none overrides the variable in `config.json`, `subagent.json` or `.env` -- so the seam
already covers them, and covers a future product for free.

## Tests

Sub-agent rule, joining the two directions `tests/test_agent_loop_subagent_role.py`
already enforces:

- the A2A tool is registered on the host and absent in a sub-agent process;
- both server hostings refuse to start under `RAVEN_SUBAGENT=1`.

Protocol conformance, driven by the SDK's own client -- the closest thing to a second
implementation available: card fetch, `SendMessage`, `GetTask`, `CancelTask`, a streaming
subscription, and a header-less request answering `VersionNotSupportedError`.

Two that guard the decisions most likely to erode:

- an unauthenticated request is refused **before** `AgentExecutor` runs, asserted by the
  executor not having been entered rather than by the status code alone;
- a turn that raises returns `InternalError` whose payload carries no traceback text.

Lifecycle: a turn that asks a question parks the task in `TASK_STATE_INPUT_REQUIRED` and a
second message against that task id resumes the same turn.

## Deliberately out of scope

- the gRPC and HTTP/REST bindings -- JSON-RPC only;
- push notification configuration (four of the eleven `RequestHandler` methods);
- a 0.3 compatibility layer;
- per-caller credentials or any enrolment flow: one configured bearer token;
- migrating `raven serve` to starlette/fastapi;
- deferring the `a2a-sdk` import to the first inbound request. It is paid at mount today,
  so every onboarded gateway pays it at boot whether or not a peer ever calls; moving it
  behind the first request would return that to the operator who never uses the face;
- advertising peer skills into the turn, so the model can pick a peer unprompted;
- bounding the task store. `InMemoryTaskStore` has no TTL, no count cap and no reclaim
  path, and the caller is the one who decides how many tasks exist, so an authenticated
  peer can grow it for the process lifetime. Deferred rather than solved: the token is
  configured per deployment and there is no enrolment flow, so every caller is one the
  operator admitted by hand, and a restart clears it. The same applies to concurrency --
  an inbound turn does not pass through `Scheduler.submit` (recorded in that guard's own
  roster), so nothing on this path bounds how many turns a peer holds open at once, and a
  parked `INPUT_REQUIRED` task holds one until its ask times out.

## Evidence

Measured 2026-09-13 and 2026-09-14 against `a2a-sdk` 1.1.2 and `pya2a` 0.1.1 installed in
isolated environments, and against this repo at `origin/main`.

| Claim | How it was checked |
|---|---|
| 1.0 method names, bindings, version header | the published specification and its method-mapping table |
| `TASK_STATE_*` has nine values | enumerated from the SDK's protobuf enum |
| task rides `result.task` in 1.0 | `SendMessageResponse` descriptor fields are `task`, `message` |
| card path is `agent-card.json` for both 0.3 and 1.0 | the SDK serves both dialects and contains no other well-known path |
| `capabilities` / `securityScheme` field sets | protobuf descriptors for `AgentCapabilities` and `SecurityScheme` |
| SDK core needs no ASGI | `request_handlers`, `agent_execution`, `tasks`, `events` imported in a venv with no starlette/fastapi/grpc |
| the two `utils` files degrade rather than require | `TYPE_CHECKING` plus `try/except ImportError` falling back to `Any` |
| SDK types are protobuf | `type(AgentCard)` is `google._upb._message.MessageMeta` |
| `pya2a` name collision | five overlapping paths between the two `RECORD` manifests |
| `pya2a` unsatisfiable here | uv resolver output against `starlette>=1.3.1` |
| ASGI stack already locked | `fastapi` 0.137.1, `starlette` 1.3.1, `sse-starlette` 3.4.1, `uvicorn` 0.46.0 present; zero importers under `raven/` and `bridge/` |
| aiohttp server surface | six files, ~84 `web.` sites, 46 in `ws.py` |
| a served raven already has a question path | ACP sends `session/request_permission` on the caller's wire |
| a turn does not suspend to ask | `AskUserTool` sets `blocking_interaction = True` and awaits `broker.await_question`, holding a future |
| every transport brings its own broker | `QuestionResponder` at `raven/contracts/asking.py:58` is a structural `Protocol` |
| all five products inherit the gate | every `agents/*/subagent.json` is `kind: acp`; no `RAVEN_SUBAGENT` override in any product's config, roster row or env file |
