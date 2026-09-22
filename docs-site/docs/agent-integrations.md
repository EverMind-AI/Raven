# Agent Integrations

Use this guide to connect and operate existing agents. If you are building a
new Raven agent, see [Building an Agent](building-agent.md); if you are
implementing an ACP, CLI, HTTP, or other backend, see [Protocol and Backend
Integration](protocol-backends.md). For wire messages and lifecycle details,
see [Agent Protocols](agent-protocols.md).

## Agent origin and runtime { #agent-origin-and-runtime }

“Raven agent” and “external agent” describe who supplies the agent. `kind`
describes the runtime backend Raven uses. These are independent axes: a
Raven-shipped agent can use ACP, and an external agent can use ACP, CLI, or
HTTP.

| Integration | Runtime configuration | Execution boundary |
| --- | --- | --- |
| In-process backend | `kind: builtin` | A Raven loop in the host process |
| Raven-shipped agent | Discovered folder, `kind: acp` | Launcher starts Raven with the agent's own configuration |
| External local agent | `kind: acp` or `kind: cli` in `subagents.agents` | Another product's process and native policy |
| OpenAI-compatible HTTP agent | `kind: openai` in `subagents.agents` | Remote endpoint; no local tool loop in this backend |
| A2A peer | `a2a.peers` plus `a2a_send` | An independent host, outside the sub-agent roster |

`spawn` and DAG nodes select from one roster. A node must use a name or worker
label actually advertised by the current tool; a familiar product name alone
does not install, enable, or authenticate an agent.

## Raven-native agents { #raven-native-agents }

| Agent | Best for | Important boundary |
| --- | --- | --- |
| Raven-Code | Coding, debugging, and verification | Edits real files; partition parallel writers' files, and treat todo completion as a claim rather than test evidence. |
| Raven-Design | Visual design, review, and presentation work | Requires design/media readiness; routes deck requests to hidden Raven-PPT. |
| Raven-Oncall | Running and watching multi-round or remote work | Needs target-machine access, budget, and stop conditions; the first response does not mean the job finished. |
| Raven-PPT | Producing `.pptx` decks | Normally enters through Raven-Design; needs deck engine, templates, rendering, and media requirements. |
| Raven-Research | Live-web research and sourced reports | Needs the research profile and credentials; it is not a general coding or chat delegate. |

These five definitions are not necessarily five visible roster entries.
The shipped Raven-PPT manifest is hidden behind Raven-Design's routing; an
operator's local manifest may differ. Routing also considers declared
requirements, attachments, and tier, so a task does not always run on the same
implementation as the visible entry.

Definitions are discovered from agent folders, including the user-owned
`$RAVEN_HOME/agents/` tree. Readiness depends on the runtime and any declared
engine wheel being installed in the interpreter that launches the agent.
Restart the resident host after changing a definition to ensure its live roster
is rebuilt. Do not assume that changing a display name creates a new identity.
Set them up through `raven onboard` or the WebUI agent settings.

From a TUI, WebUI, or configured channel, name the desired roster entry in the
request and state the workspace, allowed side effects, and evidence you expect.
For a DAG node, set `subagent` to the currently advertised roster name; a
generated worker label is valid only for the current `run_subagent_dag` call.
Stored Playbooks must use a registered roster name, not a turn-local label. A
display name that is not in the current roster does not select an agent.

### Calling the five native agents { #calling-the-five-native-agents }

| Calling surface | Support | Notes |
| --- | --- | --- |
| Host delegation through `spawn` | All five, when enabled and ready | Ask from the TUI, WebUI, or a configured channel; use the advertised roster name. Raven-PPT normally enters through Raven-Design's route. |
| Stateful instance chat | ACP agents that advertise stateful sessions | Continue an instance from the WebUI instance panel or the corresponding RPC; check the live capability snapshot before relying on resume. |
| `run_subagent_dag` node | Advertised roster agents | Put the roster name in `subagent`; generated Worker Table labels are turn-local and cannot be used in stored Playbooks. |
| Playbook | Registered roster agents | A Playbook ultimately dispatches through the same DAG registry; use Raven-Design for the normal Raven-PPT route. |
| Direct ACP over stdio | All five | Start the product launcher and speak newline-delimited JSON-RPC. Raven-Research starts ACP without an `--acp` flag. |
| One-shot CLI hosting | Raven-Code only | Use its launcher with `--task` or `--prompt-file`; `--session` continues the CLI conversation. |
| Messaging channels | All host-delegated agents | The channel reaches Raven first; delegation then uses the same roster and permissions. |
| Direct A2A call to one native agent | Not supported | A2A exposes a Raven host, not five independent native-agent endpoints. |

The first four surfaces are host-level ways to delegate work; only direct ACP
and Raven-Code's CLI hosting start a native product launcher explicitly. A
successful readiness probe or delegation receipt is not evidence that the task
completed; inspect the result, files, and tests or report artifacts.

### Minimal ACP invocation { #minimal-acp-invocation }

Each shipped entry is started by its launcher. Use the corresponding command:

| Agent | Launcher command | Note |
| --- | --- | --- |
| Raven-Code | `python agents/raven-code/run.py --acp` | Also supports one-shot CLI hosting. |
| Raven-Design | `python agents/raven-design/run.py --acp` | Requires the design engine. |
| Raven-Oncall | `python agents/raven-oncall/run.py --acp` | Requires registered target-machine access for operational work. |
| Raven-PPT | `python agents/raven-ppt/run.py --acp` | Normally reached through Raven-Design; requires the PPT engine. |
| Raven-Research | `python agents/raven-research/run.py` | ACP is the default hosting; this launcher has no `--acp` flag. |

Use the interpreter that has Raven and the agent's declared engine wheel
installed. Request a deck through Raven-Design in normal use rather than
selecting the hidden Raven-PPT route directly.

For example, start Raven-Code:

```bash
python /absolute/path/to/Raven/agents/raven-code/run.py --acp
```

The launcher renders that agent's config and starts `raven acp` over stdin and
stdout. Send one JSON-RPC object per line; keep logs and wrapper output off
stdout. After the process starts, the smallest useful request sequence is:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/absolute/path/to/project","mcpServers":[]}}
{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"<session-id-from-session-new>","prompt":[{"type":"text","text":"Reply with one sentence; do not edit files."}]}}
```

| Field or argument | Meaning |
| --- | --- |
| `--acp` | Serve the agent over stdio ACP; it is not an HTTP listener. |
| `python` | Use the interpreter that has Raven and the agent's declared engine wheel installed. |
| `protocolVersion` | ACP protocol version requested during the handshake; Raven currently uses `1`. |
| `clientCapabilities` | Features the caller can handle; `{}` is the minimal declaration. |
| `cwd` | Absolute task workspace for the session; it is not the agent's ACP home. |
| `mcpServers` | Per-session stdio MCP attachments; use `[]` when none are needed. |
| `sessionId` | The identifier returned by `session/new`; reuse it for later prompts. |
| `prompt` | An array of content blocks; the minimal block has `type: "text"` and `text`. |

The server may emit `session/update` notifications between these requests. Read
them, and handle permission or question requests before waiting for the final
prompt response. The full lifecycle and negotiated capabilities are in [Agent
Protocols](agent-protocols.md#serve-raven-over-acp).

### Task prompt templates { #task-prompt-templates }

Use the advertised roster name and state the scope, allowed side effects, and
evidence you expect. These one-line templates are starting points, not proof
that a task can run or has completed:

| Agent | Example request |
| --- | --- |
| Raven-Code | `Raven-Code: inspect the parser in src/parser.py; edit only the parser files; run the matching tests; report the command output. Do not commit or push.` |
| Raven-Design | `Raven-Design: review this dashboard for hierarchy and accessibility; propose concrete visual changes and save the feedback to the project workspace.` |
| Raven-Oncall | `Raven-Oncall: run this parameter sweep on the registered lab machine; use a two-hour budget; stop failed workers; compare each round and report the final artifacts.` |
| Raven-PPT | `Raven-PPT: create a .pptx product-review deck from these notes; use Raven-Design when possible; render every slide and return unresolved issues.` |
| Raven-Research | `Raven-Research: compare these approaches using sources from the last year; cite material claims with URLs and write the report to the requested workspace file.` |

For every agent, verify readiness before a real task and inspect the final
answer, generated files, and task evidence afterward. ACP readiness or a listed
roster row proves that the process can be started; it does not prove that the
task completed successfully.

<span id="working-safely-with-raven-code"></span>

### Raven-Code: workspace and verification boundaries { #raven-code-workspace-and-verification }

Raven-Code's `code-flow` adds product behavior beyond an identity prompt. Give
each task a bound project directory, explicit file ownership, allowed side
effects, and a verification command; inspect the files, command output, and
Harness Manifest afterward.

| Area | Boundary |
| --- | --- |
| Project instructions | `AGENTS.md`, `CLAUDE.md`, and `CONTEXT.md` are read from the working directory under configured selection, path confinement, and prompt budget; they are not Agent-home bootstrap files. |
| File and workspace state | File tools track versions read by the session; with read-before-edit, unread or externally changed versions are refused. Shared checkouts expose the same HEAD and changes; notices and the read ledger are not file locks or worktree isolation, and full writes are not universally guarded. |
| Task state | `todo` persists per session; completion is a model claim, not test evidence. Existing sessions restore their checklist, while a new task needs a genuinely new session; in-place `/new` retains the session key and may not reset this product checklist. |
| Git evidence | The Harness Manifest reports changes, commits, blockers, and shared-workspace attribution in ACP response metadata; do not infer Git facts from “ready”. |
| Product configuration | The Raven-Code launcher enables its product configuration. Installing `code-flow` elsewhere does not enable every capability; notices/reports and the replacement tool face have separate gates. |

Request example: “Inspect project instructions, fix this parser bug in the
owned files only, run matching tests, and report remaining changes or blockers.
Do not commit or push.”

For follow-ups read [Working with sub-agents](agent-collaboration.md); for
background jobs read [Long-running work (Oncall)](oncall.md). See the
[implementation case study](building-agent.md#case-study-raven-code) for the
launcher and harness details.

## Connect external agents { #connect-external-agents }

The following presets are declared in the current source. This is a list of
supported configuration paths, **not** a claim that every vendor release or
your local installation has passed a live task.

| Preset key | Transport and launcher | Check before use |
| --- | --- | --- |
| `claude_code` | ACP adapter via `npx` | Adapter download/runtime and the child's authentication |
| `codex` | ACP adapter via `npx` | Preset sets `INITIAL_AGENT_MODE=agent-full-access`; assess host and network access |
| `opencode` | `opencode acp` | Current preset withholds session MCP delivery via `sessionMcp: false` |
| `hermes` | `hermes acp --accept-hooks` | Native authentication; the flag accepts previously unseen shell hooks |
| `openclaw` | `openclaw acp` | Its gateway and authentication; startup has a longer handshake allowance |
| `mirothinker` | OpenAI-compatible HTTP | Endpoint/model/key; preset opts out of stateful replay |

GitHub Copilot, Qwen Code, CodeBuddy, Qoder, Grok Build, Kimi Code, and Pi also
have preset entries. Adapter versions and exact commands are maintained in
`raven/agent/subagent/presets.py` and `raven/agent/subagent/acp_registry_presets.py`.
Prefer the installed version's preset instead of reconstructing its flags.

Distinguish three claims when assessing compatibility:

- **Preset declared:** Raven has a configuration template.
- **Handshake/probe successful:** this installation starts and reports capabilities.
- **Task verified:** a real task exercised authentication, tools, and output.

Hand-written integrations need their own checks at all three applicable levels.

## Connect and verify

1. Install and authenticate the external agent according to its own version.
   Ensure its executable is on the resident Raven process's `PATH`.
2. Configure it from the WebUI's agent setup, or add a row to
   `subagents.agents` in the host config. Preserve existing rows and settings.
3. Inspect the configured row and readiness result. ACP probing may launch a
   process and a shim may download code; a full task test can spend provider quota.
4. Run a harmless task such as “Reply with one sentence; do not edit files.”
   Then verify any required file, tool, or resume capability separately.
5. Enable broader delegation only after checking permissions and isolation.

A minimal custom ACP entry using an already installed Hermes executable:

```json
{
  "subagents": {
    "agents": [
      {
        "name": "Hermes Agent",
        "preset": "hermes",
        "kind": "acp",
        "enabled": true,
        "command": "hermes acp --accept-hooks",
        "cwd": "/absolute/path/to/project",
        "readyTimeoutMs": 30000,
        "timeout": 600
      }
    ]
  }
}
```

This is a fragment to merge, not a replacement for your config. The hook flag
has the security consequence stated above. ACP `command` launches a server:
do not put `{prompt}`, `{prompt_file}`, or `{agent_id}` into it.
`readyTimeoutMs` limits the handshake; `timeout` limits a task. The latter
defaults to no automatic deadline when omitted.

For a CLI backend, `command` is an argv template. `{prompt}` is substituted as
one argument, `{prompt_file}` names a prompt file, and no placeholder means
stdin delivery. A `resumeCommand` must use `{agent_id}`; id provisioning and
transcript parsing must agree with the CLI. Do not copy those fields into ACP.

For an HTTP backend, configure `baseUrl`, `model`, and credentials instead of
a command. Host-side replay may provide conversation history, but it is not a
remote native session. This backend cannot read local paths or accept MCP
delivery. An OpenAI-compatible agent endpoint is distinct from changing Raven's
main model provider.

## Files, state, and credentials

| Location or handle | Responsibility |
| --- | --- |
| User working directory | Files the task is authorized to read or change |
| Host Agent home | Host identity, sessions, skills, and memory |
| Agent state root | Launcher-rendered config and agent working state |
| Agent ACP home | The child engine's identity, transcripts, and skill pool |
| `instance` | Continue a child session where that backend supports it |
| Node id | Address a recorded task/output in the host conversation |

A CLI's `readsLocalFiles` is a declaration about its environment. ACP
capabilities are negotiated and interpreted by its adapter. The HTTP backend
has no local file access: send bounded content, not a host path. Configure
memory identities only when that agent actually supports the intended memory
path; adding metadata does not make an external product share Raven's memory.

Keep credentials out of prompts and shared manifests. A2A credentials are
matched by origin, not by agent display name; its peer list is not an egress
firewall. Follow the [A2A guide](agent-protocols.md#call-a-remote-a2a-peer).

## Security and troubleshooting

Outbound ACP automatically prefers an offered allow option. The parent's
permission mode does not provide per-tool human review inside every external
agent. Review native permissions, account privileges, writable mounts, hooks,
and network access before using unattended delegation.

| Symptom | Check |
| --- | --- |
| Agent absent from the selectable roster | Discovery, enabled/hidden status, routing, readiness |
| Engine unavailable | Required wheel in the child interpreter, not merely another virtualenv |
| Handshake passes, task fails | Provider credentials, tool prerequisites, child stderr |
| CLI output missing or garbled | Exit status, transcript format, output parser |
| Instance does not continue | Resume contract and negotiated session support |
| MCP selection not delivered | Backend support and `sessionMcp` isolation policy |
| Remote agent cannot open a file | Content versus host-path transfer |

For graph failures see [DAG Orchestration](orchestration.md). For permissions
and isolation see [Permissions and Security](permissions.md).
