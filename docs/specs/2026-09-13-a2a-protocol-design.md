# A2A protocol support: the host speaks it, sub-agents do not

Status: design, approved 2026-09-13.

## The gap

Raven has two protocol faces today and both point inward.

`raven/acp/` serves ACP over stdio, so an editor or a parent raven can drive this
process. `raven/acp_client/` and `raven/agent/subagent/backends/` reach outward, but
only to things raven itself launches: a `kind: cli` subprocess, a `kind: acp` child, a
`kind: openai` Chat Completions endpoint. Every one of those is a process or an endpoint
raven owns the shape of.

What is missing is interoperability with an agent that raven did not build and does not
launch -- one that speaks a public protocol, lives at a URL, holds its own task state,
and belongs to someone else's trust domain. A2A is that protocol.

The registry design of 2026-08-14 already named it. Its deferred-work table lists
`openai` -> `http` + `protocol` with the trigger written as "when a second HTTP protocol
lands (A2A, ACP over HTTP)". This is that landing.

## Owner ruling (2026-09-13)

**A2A is a host-only protocol face. Sub-agents are reached over ACP and speak no A2A in
either direction.**

That is a boundary, not a permission setting. It forecloses a design that otherwise
looks attractive -- exposing each product in `agents/` as its own A2A endpoint -- and it
is what keeps A2A and ACP orthogonal rather than competing:

| | ACP | A2A |
|---|---|---|
| Direction | raven orchestrates downward | raven interoperates outward |
| Transport | stdio, process-level | HTTP, cross-host |
| Trust | host owns the child's lifecycle | separate trust domain, authenticated |
| Who holds the face | host **and** sub-agent | host only |

Without the ruling, "how do I call raven-code" would have two answers, and every later
feature would have to pick one. With it, that question has one answer forever.

```
external A2A client --A2A--> host raven <--A2A--> external A2A agent
                                 |   (the only process holding either A2A face)
                                 +--ACP--> agents/raven-code
                                 +--ACP--> agents/raven-ppt
                                 +--ACP--> agents/raven-design
                                 +--ACP--> agents/raven-oncall
                                 +--ACP--> agents/raven-research
```

The five products keep `kind: "acp"`. No A2A route to them is added.

## Protocol version: 1.0 only

A2A 1.0.0 is a restructure of 0.3, not a point release, and the differences are on the
wire:

| | 0.3 | 1.0.0 |
|---|---|---|
| Agent Card path | `/.well-known/agent.json` | `/.well-known/agent-card.json` |
| Send | `message/send` | `SendMessage` |
| Get | `tasks/get` | `GetTask` |
| Task state | `working` | `TASK_STATE_WORKING` |
| Task envelope | on `result` | on `result.task` |
| Version header | absent | `A2A-Version` required |

1.0 layers the spec as a canonical data model (normatively a `.proto`), abstract
operations, and three bindings -- JSON-RPC, gRPC, HTTP/REST -- which must be
functionally identical. We implement **the JSON-RPC binding only**.

The spec defines an absent `A2A-Version` header to mean 0.3. A 1.0-only server therefore
has to answer header-less requests deliberately: we return `VersionNotSupportedError`
rather than guessing a dialect. No 0.3 compatibility layer is built.

## Dependency: `a2a-sdk`

Decided after measuring three candidates against this repo.

**`pya2a` is rejected.** It is a 2025-04-15 package at 0.1.1 with two releases total, and
it fails on four independent counts:

- it implements the original April-2025 draft (`tasks/send`, `tasks/sendSubscribe`,
  `/.well-known/agent.json`), predating even 0.2's `message/send` -- two major versions
  behind the target;
- it carries no `A2A-Version`, no `TASK_STATE_*`, and no `supportedInterfaces`;
- it pins `starlette>=0.27,<1.0` against this repo's `starlette>=1.3.1`, which resolves
  as unsatisfiable, and `cryptography<42.0.0` against a lock holding 48.0.1;
- it installs into the top-level package name `a2a` -- the same name the official SDK
  uses -- overlapping on five core paths including `a2a/__init__.py` and
  `a2a/client/client.py`, so installing both silently clobbers one with no installer
  error. Its own default install cannot be imported at all: `a2a/__init__.py`
  unconditionally imports `a2a.server`, which imports `starlette`, which the package
  declares only under an optional extra.

**`a2a-sdk` 1.1.2 is adopted**, with its cost stated rather than hidden. Its object model
is protobuf (`AgentCard`, `Task` and `Message` are `a2a_pb2` messages, not Pydantic
models), which is foreign to a repo whose config and contracts are Pydantic throughout;
conversion therefore happens at our boundary, not inside it. The base install adds five
packages to the lock: `protobuf`, `google-api-core`, `googleapis-common-protos`,
`json-rpc`, `culsans`.

What it buys is the part worth buying. `RequestHandler` is eleven protocol methods,
already implemented by `DefaultRequestHandler` over a task store, an event queue and a
streaming aggregator. What we implement against it is `AgentExecutor`, which is two
methods:

```
execute(context: RequestContext, event_queue: EventQueue) -> None
cancel(context: RequestContext, event_queue: EventQueue) -> None
```

That reduces inbound A2A from "implement a protocol" to "translate an A2A task into one
raven turn".

## Transport binding: our own aiohttp routes, deliberately temporary

The SDK's server half is written for ASGI, and this gateway is aiohttp. The coupling is
separable: `starlette` and `fastapi` appear only under `a2a/server/routes/` and
`a2a/compat/v0_3/`, while the four core packages -- `request_handlers`,
`agent_execution`, `tasks`, `events` -- import cleanly in an environment with no
starlette, no fastapi and no grpc installed.

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

This file is written to be deleted. The SDK already ships
`add_a2a_routes_to_fastapi()`, `create_jsonrpc_routes()` and
`create_agent_card_routes()`; if the gateway is ever moved to starlette/fastapi, the
migration for A2A is dropping `routes_aiohttp.py` and calling those instead. No A2A
logic moves.

That migration is not in this change, and the reason is sequencing: a protocol feature
should not sit behind a whole-repo web-stack rewrite. It is nonetheless cheaper than it
looks, which is why the binding is isolated rather than spread. The server-side aiohttp
surface is six files and roughly eighty call sites, concentrated in
`raven/rpc/transports/ws.py` (539 lines, 46 of them), and that file uses only
conventional API -- `web.Request`, `web.Response`, `web.WebSocketResponse`,
`web.json_response`, `web.FileResponse` and a set of HTTP exception classes -- each with
a direct starlette equivalent. The whole ASGI stack is already resolved in the lock
(`fastapi` 0.137.1 by way of `everos`, `starlette` 1.3.1, `sse-starlette` 3.4.1,
`uvicorn` 0.46.0) and no file under `raven/` or `bridge/` imports any of it.

Three things make that migration non-trivial when it comes, and they are recorded here so
whoever does it does not rediscover them: `ws.py` carries the browser auth bootstrap
(one-time nonce, HttpOnly SameSite=Strict cookie, Origin check, `X-Raven-Token`) and is
security code, not mechanical translation; `raven web` depends on an `AppRunner`/`TCPSite`
process shape that becomes a `uvicorn.Server`; and the remaining five aiohttp files use
it as an HTTP *client*, so the dependency stays either way.

## Module layout

Named for symmetry with the ACP pair that already exists.

```
raven/a2a/               inbound -- the server face
  card.py                Agent Card construction
  executor.py            AgentExecutor: an A2A task becomes one raven turn
  routes_aiohttp.py      JSON-RPC dispatch + A2A-Version; deleted on migration
  gate.py                sub-agent refusal
raven/a2a_client/        outbound -- the client face
```

Import-linter gains one contract line: `raven.a2a` must not import `raven.cli`, matching
the existing "the served surfaces do not import the launcher" rule that already binds
`raven.rpc` and `raven.acp`.

## Inbound

`DefaultRequestHandler` from the SDK, driven by our `AgentExecutor`.

The Agent Card is served at `/.well-known/agent-card.json` and declares a single entry in
`supportedInterfaces` -- the JSON-RPC binding, `protocolVersion: "1.0"`. Skills are
derived from what the host can actually do, never hand-written, following the rule the
registry design already set for capabilities: derived in one place, because hand-filled
capability fields are wrong on some agent by construction.

Mounting is opt-in and off by default, in two hostings:

- a config flag mounts it on the running gateway, sharing that port and lifecycle;
- `raven a2a serve` runs it standalone for a headless deployment.

Default-off is a security position, not caution. The `/rpc` WebSocket is a local,
token-guarded surface for this user's own page; an A2A endpoint is a network face for
other people's agents. Running `raven web` must not silently open the second one.

## Outbound

A remote A2A agent is a roster row of a new `kind: "a2a"`, added to the discriminated
union in `raven/config/schema.py` beside `cli`, `openai`, `acp` and `builtin`. The row
holds the Agent Card URL and its auth material.

Capabilities are read from the fetched Card rather than declared in config, for the
reason the registry design already gives: under one protocol, different agents disagree
(`can_fork` is false on codex and true on hermes), so a hand-filled field is guaranteed
wrong somewhere, and three consumers read it.

## The gate

The ruling has two halves and they need different enforcement, because only one of them
passes through the tool registry.

**Outbound is already closed, and the design keeps it that way.** The only model-facing
routes to any sub-agent are `spawn`, `run_subagent_dag`, and the three graph controls
(`cancel_dag`, `resolve_dag_node`, `dag_status`); direct chat is a host UI path, not a
model tool. All five are in `WITHHELD_FROM_SUBAGENT` and are not registered at all in a
sub-agent process -- not hidden from the schema, because hiding leaves a tool reachable
through `tool_call`. A `kind: "a2a"` row is therefore unreachable from a sub-agent
without any new code, and the correct way to keep it that way is a test, not a gate.

This is also why no ad-hoc "call this A2A URL" tool is added. Such a tool would be a
fourth route to another agent, bypassing `spawn`, and would have to be withheld
explicitly -- the same trap `load_playbook` already fell into by dispatching from inside
the call. Roster rows only.

**Inbound needs a new gate**, because serving a port does not go through the registry.
Both hostings refuse to start when `is_subagent_process()` is true, and say why. The
signal is the existing `RAVEN_SUBAGENT` environment variable: the host merges
`subagent_role_env()` into every `kind: acp` child it launches, all five products in
`agents/` are `kind: acp`, and none of them overrides the variable in `config.json`,
`subagent.json` or `.env` -- so the seam already covers them, and covers any future
product for free.

## Tests

`tests/test_agent_loop_subagent_role.py` already holds the sub-agent rule in both
directions: it fails on a withheld name that nothing registers, and on a withheld name
the gates let through. Two assertions join it:

- a `kind: "a2a"` row is unreachable in a sub-agent process;
- both A2A server hostings refuse to start under `RAVEN_SUBAGENT=1`.

Protocol conformance is tested against the SDK's own client, which is the closest thing
to a second implementation available: card fetch, `SendMessage`, `GetTask`, `CancelTask`,
and a header-less request answering `VersionNotSupportedError`.

## Deliberately out of scope

- the gRPC and HTTP/REST bindings -- JSON-RPC only;
- push notification configuration (four of the eleven `RequestHandler` methods);
- a 0.3 compatibility layer;
- migrating `raven serve` to starlette/fastapi;
- the deferred `openai` -> `http` + `protocol` rename. It is now triggered, but it is a
  migration of an existing kind and does not belong in the change that adds a new one.

## Evidence

Measured 2026-09-13 unless stated.

| Claim | How it was checked |
|---|---|
| A2A 1.0.0 wire shape | the published specification and its method-mapping table |
| `TASK_STATE_*` has nine values | enumerated from the installed SDK's protobuf enum |
| SDK core needs no ASGI | `request_handlers`, `agent_execution`, `tasks`, `events` imported in a venv with no starlette/fastapi/grpc |
| SDK types are protobuf | `type(AgentCard)` is `google._upb._message.MessageMeta` |
| `pya2a` name collision | five overlapping paths between the two `RECORD` manifests |
| `pya2a` unsatisfiable here | resolver output against `starlette>=1.3.1` |
| ASGI stack already locked | `fastapi` 0.137.1, `starlette` 1.3.1, `sse-starlette` 3.4.1, `uvicorn` 0.46.0 present; zero importers under `raven/` and `bridge/` |
| aiohttp server surface | six files, ~84 `web.` sites, 46 in `ws.py` |
| all five products inherit the gate | every `agents/*/subagent.json` is `kind: acp`; no override of `RAVEN_SUBAGENT` in any product's config, roster row or env file |
