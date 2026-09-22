# Agent protocols: ACP and A2A

For setup of Raven specialists, Claude Code, or another existing product, start
with [Agent Integrations](agent-integrations.md). For implementing a transport
or backend, use [Protocol and Backend Integration](protocol-backends.md).

Raven can serve another host and call other agents. Choose the interface by who
owns the process and what needs to cross the boundary, not by the word "agent"
alone. This page describes the implementation shipped in this repository.

## Choose an interface

| Need | Interface | Boundary |
| --- | --- | --- |
| An editor or local host launches Raven | Agent Client Protocol (ACP) server | Child process, stdio JSON-RPC, sessions |
| Raven launches a local agent | ACP client or CLI backend | A configured sub-agent process |
| An independently operated agent calls Raven | Agent2Agent (A2A) server | HTTP JSON-RPC, tasks, optional SSE |
| Raven asks a remote peer for work | `a2a_send` | Agent Card discovery and an HTTP request |
| A browser or terminal UI controls Raven | Raven RPC | The product contract in `rpc-schema/openrpc.json` |
| An agent needs an external tool or resource | MCP | Tool/resource access, not an agent task protocol |

ACP is not an HTTP endpoint. A2A is not a replacement for the local sub-agent
roster. [DAG orchestration](orchestration.md) coordinates tasks after a request
arrives; it is separate from the protocol that carried the request.

## Serve Raven over ACP

Configure a model provider first, as in [Quick Start](quick-start.md). An ACP
host then launches the installed `raven acp` command. From a source checkout:

```bash
uv run raven acp
uv run raven acp --config /absolute/path/to/config.json
```

These are alternative launches, not two steps. The command waits for
newline-delimited JSON-RPC on stdin; stdout is reserved for protocol frames.
Logs go to `<config dir>/logs/acp.log`, rotating at 10 MB with three retained
files. Warnings and errors also reach stderr. Keep wrapper banners off stdout.

### Session lifecycle and capabilities

1. Send `initialize` with `protocolVersion: 1` and client capabilities.
2. Send `session/new` with an absolute `cwd` and `mcpServers` (use `[]` when
   there are none). Use the returned `sessionId` for subsequent requests.
3. Send `session/prompt`; consume `session/update` events while it runs and
   handle any server-to-client permission or question requests.
4. Wait for the prompt response, or send `session/cancel` to stop the turn.
   Only then start the next prompt on that session.

An example creation request, sent **after** initialize:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "session/new",
  "params": {"cwd": "/absolute/path/to/project", "mcpServers": []}
}
```

| Capability | Current behaviour |
| --- | --- |
| Prompt content | Text, images, embedded text context; audio is not advertised |
| `session/load` | Replay stored history as updates before returning |
| `session/resume` | Reopen without replaying history |
| Session management | List, close, and delete are supported; closing and deleting are different operations |
| Model selection | Read `configOptions`; use `session/set_config_option` for the next turn |
| Modes | Read the returned modes; `session/set_mode` applies a declared profile to the next turn |
| Per-session MCP | Stdio servers scoped to that session; HTTP/SSE are not advertised |
| Steering | Raven extension `_raven/session/steer`, only when advertised; not a standard ACP method |

Load and resume also require `cwd`. A deployment with no modes declared cannot
answer `session/set_mode`. Unusable MCP attachments are dropped with a log
warning; creating a session does not prove that every requested tool connected.
Raven uses its own file and command tools rather than the client's unsaved
editor buffers or `terminal/*` services.

### Approvals, questions, and replay

As an ACP **server**, Raven sends tool approval requests through
`session/request_permission`. General questions use `elicitation/create` when
the client advertises form support, with a permission-shaped fallback for
clients that do not. Keep reading both requests and notifications while a
prompt is pending; waiting only for its final response can deadlock interaction.

As an ACP **client**, Raven runs delegated agents unattended. It automatically
selects an offered permission option, preferring `allow_always`, then
`allow_once`. This is a significant trust boundary: it is **not** per-operation
human approval. Configure the child agent's policy and isolation before enabling
it. See [Permissions and security](permissions.md).

Transcript replay is bounded to the newest 500 messages and 16 KiB per message
text. Multimodal history can replay as text rather than reconstructing the
original attachments. Recognised credentials in outbound ACP payloads are
redacted, but redaction is not a complete secret scanner.

## Serve Raven over A2A

The gateway-mounted A2A face is off by default. Enable it explicitly:

```bash
raven a2a enable
```

This writes `a2a.server.enabled` and mints `a2a.server.token` if missing.
Restart the existing `raven gateway` or `raven web` process to mount the routes.
Do not start a second gateway on the same port. To close the face, run
`raven a2a disable` and restart; the token is retained.

Alternatively, run a standalone listener:

```bash
raven a2a serve --host 127.0.0.1 --port 8710
```

Standalone serving is itself an opt-in and does not require `enabled: true`,
but it still needs a non-empty configured token. Running `a2a enable` first is
one way to provision it; remember that this also enables the gateway face on
its next restart. Both hostings refuse to start inside a sub-agent process.

### Agent Cards and authentication

| Route or method | Access | What it provides |
| --- | --- | --- |
| `GET /.well-known/agent-card.json` | Public | Protocol version, interface URL, general skill, authentication requirement |
| `POST /a2a` (default path) | Bearer token | A2A JSON-RPC methods |
| `GetExtendedAgentCard` | Authenticated and only when advertised | Orchestration capability and a Raven extension describing the local roster |

The public card intentionally hides the installed sub-agents. The extended
card is available only when the hosting supplies a roster; standalone serving
does not currently supply one. Its roster is reference data, not a list of
directly callable remote sub-agents: ask the host for an outcome.

The JSON-RPC binding requires `A2A-Version: 1.0` and
`Authorization: Bearer <configured token>`. An empty token refuses all RPC
callers; a successful public card fetch proves neither authentication nor
model-provider readiness. The default path can be changed with `a2a.server.path`.

For a standalone listener, this read-only check fetches the public card:

```bash
curl --fail http://127.0.0.1:8710/.well-known/agent-card.json
```

Keep listeners on a private interface or behind authenticated TLS termination.
A bearer token is shared operator access, not per-user authorization or a
sandbox. Restrict the network and child-agent privileges accordingly.

### Tasks, streaming, and limits

The supported RPC methods are `SendMessage`, `SendStreamingMessage`, `GetTask`,
`ListTasks`, `CancelTask`, `SubscribeToTask`, and `GetExtendedAgentCard`.
Older slash-style method names such as `message/send` are not this binding.
Messages require a `messageId`, a role such as `ROLE_USER`, and `parts`.

An inbound task runs one Raven turn. It starts submitted, becomes working, and
normally ends completed or failed. If the turn asks a question, it enters
input-required; a `SendMessage` with the same `taskId` answers the live waiting
turn, rather than starting another. This is an in-process wait, not a durable
checkpoint. Rich form questions are not reproduced as a full remote form UI.
This A2A question broker is wired in standalone hosting. Gateway-mounted A2A
retains the gateway's existing question broker instead; do not assume a remote
peer can complete the same question round-trip there.

Streaming methods return SSE `StreamResponse` envelopes. Non-streaming
`SendMessage` returns a `SendMessageResponse` containing a task or message.
The current Raven executor puts the final text in the completed task's status
message; it does not promise token-by-token output or generated-file artifact
delivery. A client should also understand text in message and artifact events
when talking to other peers.

Task storage is **in memory** and is lost on restart. Push notifications are
not supported; default advertised input/output is `text/plain`. `CancelTask`
is exposed, but a canceled task state is not proof that every external side
effect was undone or that every child process stopped. A turn failure returns
a fixed safe message; detailed exceptions stay in the server log.

## Call a remote A2A peer

Add credentials to the host config, not to the model prompt. Merge this fragment
into the existing configuration; do not replace provider settings:

```json
{
  "a2a": {
    "peers": [
      {
        "origin": "https://agent.example.test",
        "authScheme": "bearer",
        "credential": "REPLACE_WITH_PEER_TOKEN"
      }
    ]
  }
}
```

`a2a_send` takes `card_url` and `message`. It discovers the card, sends a text
task, and gathers text from the response. It does not expose a multi-turn task
management API or an automatic answer loop for remote input-required tasks.

Credentials match the card URL's **origin** (scheme, host, port). An unlisted
origin receives no credential, but is not automatically network-blocked: the
peer list is a credential map, not an egress firewall. Use only peers the user
named or already trusts. Redirects are not followed, and a JSON-RPC interface
on another origin is refused before sending the task. For intentional separate
origins, use a card and peer configuration on the intended interface's origin.

## Troubleshooting and implementation map

| Symptom | First check |
| --- | --- |
| ACP produces invalid JSON | Wrapper stdout; keep diagnostics on stderr |
| ACP session creation fails | Initialize first; supply a valid absolute `cwd` |
| ACP appears stuck after a tool call | Read and answer permission/question requests |
| An attached MCP tool is absent | Handshake capabilities and ACP connection warnings |
| A2A card works but RPC returns 401 | Non-empty bearer token; the public card is unauthenticated |
| A2A reports version mismatch | Send `A2A-Version: 1.0` |
| A2A reply appears empty | Read status messages as well as message/artifact parts |
| A2A setting has no effect | Restart the serving process after enable/disable |

Implementation entry points: `raven/acp/capabilities.py`,
`raven/acp/methods.py`, `raven/acp_client/permissions.py`, `raven/a2a/card.py`,
`raven/a2a/routes_aiohttp.py`, `raven/a2a/runtime.py`, and
`raven/a2a_client/client.py`. CLI help is useful for invocation, but some ACP
help prose predates session modes and per-session MCP; negotiated capabilities
and the method implementation determine support.
