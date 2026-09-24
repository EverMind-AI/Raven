# Protocol and Backend Integration

Start here when implementing an adapter, backend, or protocol client. For
operator configuration use [Agent Integrations](agent-integrations.md) or
[Channels and Messaging](channels.md). Protocol methods and deployment limits
are documented in [Agent Protocols](agent-protocols.md).

## Choose the extension point

| Requirement | Extend | Do not confuse it with |
| --- | --- | --- |
| Another local agent speaks ACP already | An ACP roster row/preset | A new transport implementation |
| A CLI has a nonstandard transcript | CLI configuration and, when needed, a parser | An ACP server |
| A remote service accepts chat completions | OpenAI-compatible agent configuration | A local tool-capable backend |
| A remote agent publishes an Agent Card | A2A client/server integration | A roster entry with `kind: a2a` |
| A messaging platform needs a bot | A channel adapter | A sub-agent backend |
| A service provides tools/resources | MCP | An agent-to-agent task protocol |
| A product needs custom turn behaviour | Plugin/participant | A fork of the shared Agent Loop |

Prefer an existing backend kind where it expresses the behaviour. Backend kinds
are a closed, validated set; adding a custom string to a plugin manifest does
not register a new backend.

## Respect runtime contracts

The Assembly Root is `raven/core/runtime.py:build_runtime`. Runtime consumers
depend on contracts; entrances and product adapters assemble or translate work.
Keep the Kernel independent of CLI, RPC, ACP, and product code.

| Contract or surface | Source |
| --- | --- |
| Sub-agent execution | `raven/contracts/subagent_backend.py` |
| Tool execution | `raven/contracts/tool.py` |
| Channels and login | `raven/contracts/channel.py`, re-exported by `raven/channels/contract.py` |
| Turn input, events, scheduling | `raven/spine/` |
| Product UI wire schema | `rpc-schema/openrpc.json` |
| Backend row validation | `raven/config/schema.py` |
| Backend construction | `raven/agent/subagent/backends/__init__.py` |

Use `CONTEXT-MAP.md` and `CONTEXT.md` for canonical names. A backend session,
host conversation, A2A task, and DAG node id have different lifetimes; mapping
them explicitly prevents results or credentials crossing conversations.

## Implement a sub-agent backend

`SubagentBackend.run` receives task text and keyword-only context including
`task_id`, `workspace`, `executor`, `session_key`, `instance`, `provider`,
`model`, `mcps`, `mcp_grant`, `mode`, `authored_task`, and `on_delta`.
Use the actual Protocol signature rather than a copied subset.

The contract returns final text and raises on failure. The manager owns
announcements, the shared semaphore, and rate limits. Do not create a second
dispatch budget inside the backend or return a success-looking sentence when
no final answer was produced.

Implement and test:

- Start/readiness versus per-task timeout as separate operations.
- Native session id binding and resumption, if supported.
- Local-file access as a real property, not inferred from the agent name.
- Explicit MCP selection and credential scope; no implicit secret sharing.
- `streams`/`on_delta` only when actual transport events provide deltas.
- Cancellation, process cleanup, output truncation, and nonzero exit handling.

Adding a backend kind requires coordinated schema, construction, capability,
configuration-write, and regression-test changes. It is not currently a
generic third-party plugin registration point. A parser-only change can often
stay in `raven/agent/subagent/backends/transcript.py` with fixtures.

## Integrate ACP safely

For an ACP client, negotiate before assuming optional features. Drive
requests and notifications concurrently while a prompt is pending; a client
that stops reading cannot answer permission or elicitation requests.
Keep stdout protocol-only and continuously drain child stderr.

Test initialize, session creation, prompt updates, final stop reason,
load/resume, cancellation, unknown capabilities, and malformed frames.
Do not advertise methods that only return “not implemented.” Model/mode
changes apply to the appropriate session and next turn, not every session
sharing a connection.

Per-session MCP is especially sensitive when connections are pooled. A
capability declaration does not by itself prove isolation between sessions;
Raven has a separate `sessionMcp` policy for that reason. Verify simultaneous
sessions with different server selections. Also test unanswered permission
requests and client disconnects.

The packaged schema fixtures and tests in `tests/test_acp_schema.py` help check
frame shapes. The current outbound client auto-approves offered permission
options, except that a request whose `toolCall.rawInput.command` the host's
deny rules refuse is answered with a reject option; integrations must disclose
that trust model, not promise interactive human review.

## Integrate A2A safely

Use the protocol SDK's request and response shapes. Raven's current binding is
A2A 1.0 JSON-RPC, not the older slash-named method form. Streaming events have
`StreamResponse` envelopes; non-streaming send has `SendMessageResponse`.
Read task status text as well as message and artifact text.

Test the public card separately from authenticated RPC and optional extended
cards. Match credentials to the intended origin; test off-origin interfaces
and redirects without sending credentials elsewhere. Never expose internal
tracebacks to peers.

Test a real local client/server round-trip with a stubbed turn, not only a
hand-written JSON fixture on each side. This catches envelope disagreements.
The task store is currently in memory; do not claim durable resumption or push
notifications. Question routing differs between standalone and gateway hosting,
as described in the protocol guide.

## Add a messaging adapter

An adapter lives under `raven/channels/adapters/<name>/`. Its `spec.py` exports
a `ChannelSpec` with display name, lazy factory, capabilities, and a config
schema. Discovery scans these packages; do not import an optional platform SDK
merely to enumerate channels.

The host owns channel socket fields (`enabled`, `allow_from`, `workspace`).
The spec declares adapter-specific fields and secret/required markers.
`dispense_channel_config` provides the admitted view to the factory; CLI and UI
configuration should use that same declaration rather than a second field list.

Implement `start`, `stop`, and `send` and route inbound messages through
`Intake`. Apply sender/group checks before downloading untrusted attachments
or performing reactions. Preserve routing metadata and conversation identity;
the gateway wires submission and outbound delivery.

Declare only capabilities you implement. Interactive login requires
`SupportsLogin`; streaming requires the corresponding protocol, and the
channel capability proof checks those claims. File attachment delivery needs
real sends and error handling, not a flag alone. The current gateway channel
outlet remains non-streaming.

Test allowed/denied senders, group policy, media handling, missing credentials,
reconnect/shutdown, and outbound failures without connecting a real account.
Then perform an explicitly authorized live-account acceptance test separately.

## Verification and handoff

Representative repository checks:

```bash
uv run pytest tests/test_subagent_third_party.py tests/test_acp_schema.py tests/test_a2a_interop.py -q
uv run pytest tests/test_channels_contract.py tests/test_channels_registry.py tests/test_channels_intake.py tests/test_channels_outlet.py -q
```

Run the specific adapter/backend tests too, plus the repo's lint and import
contracts for code changes. Report exactly which vendor versions, operations,
and hosting surfaces were exercised. A mocked test is not a live authentication
test. Do not include credentials, QR data, or raw private transcripts in fixtures.
