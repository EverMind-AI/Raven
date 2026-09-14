# A2A protocol support: the host speaks it, sub-agents do not

Status: design, approved 2026-09-13; outbound shape revised 2026-09-14.

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

The registry design of 2026-08-14 named it in advance. Its deferred-work table lists
`openai` -> `http` + `protocol` with the trigger written as "when a second HTTP protocol
lands (A2A, ACP over HTTP)". **That trigger does not fire here**, because A2A does not
enter the sub-agent roster at all -- see the ruling below. The rename stays deferred and
the discriminated union is untouched.

## Owner rulings

**First (2026-09-13): A2A is a host-only protocol face. Sub-agents are reached over ACP
and speak no A2A in either direction.**

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

**Second (2026-09-14): A2A is a capability of the host agent, not an entry in the
sub-agent roster.** An earlier draft of this design gave a remote A2A agent a roster row
of a new `kind: "a2a"`. That was wrong, and the repo already carries the evidence.

The roster answers "who may I delegate to", and its rows are shaped by raven owning the
worker: the `acp` row declares `command`, `cwd`, `env`, `ready_timeout_ms`, `mcps`,
`allow_mcp_secrets`, `session_mcp` and `preset`. An external A2A peer satisfies none of
those -- raven does not start it, does not hold its lifetime, and cannot grant it an MCP.
The strain is already visible on the one existing kind that is also external: `kind:
"openai"` carries two runtime warnings whose whole content is that roster fields do not
apply to it (`mcps`/`allowMcpSecrets`, and `readsLocalFiles`). A `kind: "a2a"` row would
have been a third instance of the same mismatch.

The confusion underneath was between **subordination** (the roster) and **reachability**
(a protocol). A2A peers are reachable, not subordinate. So the outbound half is a tool on
the host agent, and the sub-agent union is not touched.

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
  client.py              JSON-RPC over the SDK client
  peers.py               origin -> credential lookup; the model never holds one
  tool.py                the single host-agent tool; withheld from sub-agents
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

One tool on the host agent, taking an Agent Card URL and a message. No new agent entity,
no roster row, and no change to `raven/config/schema.py`'s discriminated union.

Configuration is a section of its own, `a2a`, holding the inbound switch and a list of
trusted peers keyed by origin. A peer entry carries credentials and nothing else the
roster would have demanded.

**The model never sees a credential.** It passes a URL; the client layer matches the
origin against the trusted list and attaches whatever that peer's Agent Card declares
under `securitySchemes` (API key, HTTP auth, OAuth2, OIDC, mTLS). An origin absent from
the list is called unauthenticated or refused, per that section's policy -- it is never
an invitation for the model to supply a secret itself.

This is the one place the roster shape would genuinely have helped, and it is worth being
explicit about what is given up. A roster row carries `description` and `owns`, which are
the text the classifier routes on; a tool taking a URL carries no such advertisement, so
the model learns a peer exists only from the trusted-peer list or from the user naming it
in the turn. That is the accepted cost of not modelling reachability as subordination.
Capabilities are still read from the fetched Card rather than declared, for the reason the
registry design gives: under one protocol different agents disagree, so a hand-filled
capability field is guaranteed wrong somewhere.

## The gate

The ruling has two halves and they need different enforcement, because only one of them
passes through the tool registry.

**Outbound: the tool joins `WITHHELD_FROM_SUBAGENT`.** It is a route to another agent
that does not pass through `spawn`, which is exactly the shape `load_playbook` already
has -- a third route to a graph, withheld by name for that reason. So the A2A tool is
withheld by name too, and is not registered at all in a sub-agent process rather than
hidden from the schema, because hiding leaves a tool reachable through `tool_call`.

This is a real gate rather than a free one, and that is the price of the second ruling.
The earlier roster-row draft inherited the gate without writing it, since the only
model-facing routes to a roster entry -- `spawn`, `run_subagent_dag`, and the three graph
controls -- are already withheld. Paying one frozenset entry is the cheaper half of the
trade: `tests/test_agent_loop_subagent_role.py` already holds that set in both
directions, failing on a withheld name nothing registers and on a withheld name the gates
let through, so the new entry is guarded the moment it is added.

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

- the A2A tool is in the withheld set, so both existing directions of that file's check
  now cover it: it must be registered on the host and absent in a sub-agent process;
- both A2A server hostings refuse to start under `RAVEN_SUBAGENT=1`.

One more, because the second ruling is the kind a later change undoes by accident: the
sub-agent discriminated union admits exactly `cli`, `openai`, `acp` and `builtin`. A test
pinning that set turns "someone added `kind: a2a` back" into a red test rather than a
design regression nobody notices.

Protocol conformance is tested against the SDK's own client, which is the closest thing
to a second implementation available: card fetch, `SendMessage`, `GetTask`, `CancelTask`,
and a header-less request answering `VersionNotSupportedError`.

## Deliberately out of scope

- the gRPC and HTTP/REST bindings -- JSON-RPC only;
- push notification configuration (four of the eleven `RequestHandler` methods);
- a 0.3 compatibility layer;
- migrating `raven serve` to starlette/fastapi;
- the deferred `openai` -> `http` + `protocol` rename. Its stated trigger is a second
  HTTP protocol *in the roster*, and A2A does not enter the roster, so it stays deferred
  on its original terms rather than being deferred again by this change;
- routing text for peers. A roster row would have carried `description` and `owns`; the
  tool carries neither, so the model is not advertised a peer it could choose on its own.
  If that turns out to matter, it is a separate change with its own evidence.

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
| the roster assumes raven owns the worker | the `acp` row declares `command`, `cwd`, `env`, `ready_timeout_ms`, `mcps`, `allow_mcp_secrets`, `session_mcp`, `preset` |
| an external kind already strains it | `kind: "openai"` carries two "not supported for kind 'openai'" warnings, for `mcps`/`allowMcpSecrets` and for `readsLocalFiles` |
