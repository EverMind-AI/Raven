# Agent Integrations

Use this guide to connect an existing agent. To create a new Raven agent, see
[Building an Agent](building-agent.md). For wire messages and lifecycle details,
see [Agent Protocols](agent-protocols.md).

## Distinguish origin from transport

“Raven agent” and “external agent” describe who supplies the agent. `kind`
describes how Raven runs it. They are different axes: Raven's own specialized
agents use ACP too.

| Integration | Configuration | Execution boundary |
| --- | --- | --- |
| In-process backend | `kind: builtin` | A Raven loop in the host process |
| Shipped specialized agent | Discovered folder, `kind: acp` | Launcher starts Raven with the agent's own configuration |
| External local agent | `kind: acp` or `kind: cli` in `subagents.agents` | Another product's process and native policy |
| OpenAI-compatible HTTP agent | `kind: openai` in `subagents.agents` | Remote endpoint; no local tool loop in this backend |
| A2A peer | `a2a.peers` plus `a2a_send` | An independent host, outside the sub-agent roster |

`spawn` and DAG nodes select from one roster. A node must use a name or worker
label actually advertised by the current tool; a familiar product name alone
does not install, enable, or authenticate an agent.

## Shipped Raven agents

| Agent | Purpose | Configuration concern |
| --- | --- | --- |
| Raven-Code | Coding, debugging, and verification | Real file edits; partition parallel writers' files |
| Raven-Design | Visual design and presentation work | Design engine and media-service readiness |
| Raven-Oncall | Execute and watch operational work | Target-machine access and long-running jobs |
| Raven-PPT | PowerPoint production | Deck engine, templates, and rendering dependencies |
| Raven-Research | Research and evidence gathering | Research profile, tools, and model credentials |

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

## Working safely with Raven-Code

Raven-Code adds product-specific behavior through `code-flow`, not only a
different identity prompt. Give it a bound project directory and an explicit
verification task, then inspect its evidence:

1. Project instructions such as `AGENTS.md`, `CLAUDE.md`, and `CONTEXT.md`
   are read from the working directory, subject to configured selection,
   path confinement, and prompt budget. They are not Agent home bootstrap files.
2. File tools track versions read by the current session. With read-before-edit
   enforcement enabled, an unread or externally changed version is refused.
   Re-read and reconcile instead of blindly retrying.
3. The `todo` checklist persists per session. A completed item is the model's
   claim, not a test result; ask for the actual command/output.
4. The Harness Manifest reports Git facts in ACP response metadata: changes,
   commits, blockers, and shared-workspace attribution. It is not inferred
   from the model saying “ready.”

Request example: “Inspect project instructions, fix this parser bug in the
owned files only, run matching tests, and report remaining changes or blockers.
Do not commit or push.”

Two sessions sharing a directory observe the same HEAD and changes.
Concurrency notices and the read ledger are not file locks or automatic
worktree isolation. A shared manifest cannot be attributed to one session,
and full writes are not universally guarded by read-before-edit.

An existing session restores its checklist. A new task should use a genuinely
new session; the host's in-place `/new` retains its key and does not guarantee
this separate product checklist is reset.

The Raven-Code launcher enables its product configuration. Installing
`code-flow` elsewhere does not enable everything: notices/reports and the
replacement tool face have separate gates. See the
[implementation case study](building-agent.md#case-study-raven-code).
For follow-ups read [Working with sub-agents](agent-collaboration.md);
for background jobs read [Long-running work (Oncall)](oncall.md).

## External presets

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
