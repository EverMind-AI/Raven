# Using Raven

Choose how to talk to Raven, give the conversation the right working directory,
and add other agents or services only when needed. Installed commands below can
be prefixed with `uv run` in a source checkout.

## Choose a surface

| Goal | Entry point | Next guide |
| --- | --- | --- |
| Terminal conversation | `raven tui` | [Command Reference](commands.md) |
| Browser conversation | `raven web` | [Launch WebUI](webui.md) |
| One task in a checkout | `raven agent -m "Summarize this project without editing files"` | [Permissions and security](permissions.md) |
| Chat from a messaging platform | A configured channel and resident gateway | [Channels and Messaging](channels.md) |
| Delegate to a specialist | A configured agent in the roster | [Agent Integrations](agent-integrations.md) |
| Follow up with a specialist | Its stateful instance | [Working with sub-agents](agent-collaboration.md) |
| Operate a shared browser | Browser tools and the WebUI panel | [Browser collaboration](browser-collaboration.md) |
| Retrieve document passages | Knowledge library or RPC client | [Knowledge bases](knowledge.md) |
| Run and watch a long job | Raven-Oncall with a declared campaign | [Long-running work (Oncall)](oncall.md) |
| Integrate an editor or remote peer | ACP or A2A | [Agent Protocols](agent-protocols.md) |

`raven agent` requires a message; it is not an interactive REPL. Use the TUI or
WebUI for a conversation. Start with [Quick Start](quick-start.md) if no model
provider is configured.

## Work in the right directory

The working directory holds the files the task acts on. Agent home holds
identity, memory, skills, and transcripts; it is not an interchangeable name for
the project checkout. For a one-shot task:

```bash
raven agent --workspace /absolute/path/to/project -m "Read the README and summarize the setup steps"
```

This executes a model turn and can incur provider charges. For changes, state
which files may be edited and how to verify the result. Parallel coding agents
do not automatically receive separate worktrees: divide file ownership or give
them separate working copies.

## Continue a conversation

Each one-shot CLI invocation starts a fresh session by default. Use one of
`--continue`, `--resume <id-or-prefix>`, or `--session <full-key>` when continuity
is intentional; these options are mutually exclusive.

```bash
raven sessions --help
raven agent --continue -m "Expand the previous summary; do not modify files"
```

Use the UI's conversation picker for interactive sessions. A sub-agent
`instance` continues that child's conversation; it is not the host session id
or a DAG node id. See [DAG Orchestration](orchestration.md).

## Add capability deliberately

Use a skill for reusable instructions, MCP for external tools, and a plugin for
runtime contributions. Use a specialized agent for a separate identity and
execution profile; use a DAG or playbook to coordinate dependent work. A2A is
for an independently operated peer, not another name for a local plugin.

[Skills, Memory and Extensions](skills-and-extensions.md) explains these choices.
For scheduled or proactive work, read [Proactive Reminders and Follow-ups](proactivity.md) and
keep the appropriate resident host running.

## Check results and diagnose problems

Start with `raven status` and `raven doctor`. A configured provider, a listed
agent, or a successful handshake does not establish that a real task succeeded.
Check the final answer, generated files, and relevant tests or task evidence.

For a stalled task, check permission requests, child-agent readiness, provider
errors, and the run's progress before retrying. A retry can repeat external side
effects. Use `raven tracing` and `raven trajectory --help` for diagnostics, and
redact private material before sharing it. [Trajectory debugging and replay](trajectory-debugging.md)
walks through capturing evidence and reproducing harness behavior without
repeating recorded external actions.
