# Working with sub-agents

Delegate a bounded task, inspect what the specialist actually did, and continue
the right conversation when you need changes. Raven exposes both delegated
tasks and stateful agent instances; they are different things.

Set up agents first with [Agent Integrations](agent-integrations.md). This page
covers using them, not installing their transports.

## Choose a collaboration shape

| Need | Use | Completion evidence |
| --- | --- | --- |
| One specialist task | Ask the host to `spawn` it | Returned result and relevant files/tests |
| Parallel or dependent work | [DAG orchestration](orchestration.md) | Node outputs, dependencies, and verdicts |
| Repeat a reviewed procedure | [Playbook](playbooks.md) | The actual graph result, not merely a dispatch receipt |
| Continue with a particular specialist | Direct chat with its stateful instance | That instance's response/history |
| Run and keep watching a long job | [Oncall](oncall.md) | Campaign status and final report |

`spawn` and `run_subagent_dag` are model tools, not shell commands. Delegation
uses additional model calls and can start subprocesses or external work.

## A first read-only collaboration

1. Open a conversation in the project's working directory. Check the chosen
   agent is enabled and ready.
2. Ask: “Have Raven-Code review the public API in this checkout. Do not edit
   files. Return compatibility risks with file references, then summarize
   the findings for me.”
3. Inspect the delegated task and its result in the WebUI sub-agent panel or
   task view. A stream of activity is progress, not proof of success.
4. Open its resumable instance, if one exists, and ask: “Expand the highest
   priority risk. Do not change the implementation yet.”
5. Return to the main conversation and ask it to incorporate the follow-up
   into the final summary.

The generic `raven` agent is another option when no specialized setup is ready.
Choose a stateful backend if follow-up conversation matters; a one-shot
stateless task is not automatically a resumable chat.

## Sessions, instances, and task ids

The host session is the conversation you opened. An instance is addressed by
an agent and handle within that session. A DAG node id identifies a task and its
outputs; it does not identify a conversation to chat with.

Reusing an instance can preserve that child's context. A new instance starts
separate state. The WebUI can create an instance for an enabled, stateful agent
even before the host delegates anything to it; creation records an idle
instance and does not itself run a task.

Do not assume all agents see the host's entire transcript. Send the specific
requirements and attachments they need. File paths refer to the execution
environment that receives them, not automatic transfers to every remote agent.

## Direct chat and handoff

The sub-agent panel shows instance history and, when supported, a composer
addressed to that instance. Direct turns use their own execution lane. They
can run alongside the host or other instances, but a busy instance cannot
necessarily accept another normal turn.

The direct conversation is kept separately from the main transcript. On your
next turn to the main agent, the runtime supplies a **Handoff Block**: instance
identity, times, and paths to recorded prompts/results. It does not paste the
whole exchange or produce an automatic semantic summary. Ask the host to read
the relevant records when the decision depends on their contents.

The host is also told about user-created instances and still-running direct
turns. This is evidence of activity, not evidence that a specialist finished.
Forgetting an instance removes its addressability; it does not erase the
record directories used for audit.

## Steer an active turn

Steering adds a correction to a turn already running, instead of starting a
new task. For example: “Limit this review to public interfaces; leave internal
helpers out.” The correction is read before a later model call; it cannot undo
an action already performed.

The server exposes `subagents.instance.steer`. Client UI support varies; the
current WebUI instance composer uses normal direct turns, so do not assume
typing into it invokes steer. A client implementing the RPC sends:

```json
{
  "session_key": "HOST_SESSION_KEY",
  "agent": "Raven-Code",
  "handle": "INSTANCE_HANDLE",
  "text": "Limit the review to public interfaces; do not edit files."
}
```

These are RPC parameters, not a CLI command. Resolve session/agent/handle from
the current instance list rather than inventing an address.

| Response | Meaning |
| --- | --- |
| `injected` | Text accepted for the running turn; not a promise it already acted on it |
| `no_turn` | No active turn; send a normal follow-up if still wanted |
| `unsupported` | This run's transport cannot accept steering |

CLI-agent runs and ACP agents without Raven's negotiated steering extension
cannot be assumed to support it. See [Agent Protocols](agent-protocols.md).

## Models, modes, and shared files

Where an instance advertises modes or models, its controls use that instance's
own menu. A mode override can take precedence over the host session's tier;
clearing it restores inheritance. Clearing a model override returns to the
agent's own choice. Neither setting grants new filesystem or tool permissions.

Parallel instances do not imply independent working copies. For concurrent
edits, give each agent explicit file ownership or separate worktrees. Raven-Code
reports shared-workspace conditions, but that is not a filesystem lock.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Agent missing | Enabled roster, installation readiness, and current session |
| Cannot create or resume an instance | Backend statefulness and whether the agent is enabled |
| Direct send refuses while busy | Wait for this instance; use a capable steering client for in-turn corrections |
| Host does not use the follow-up | Send a new host turn and ask it to inspect handoff records |
| Visible history but no live activity after restart | Recorded history is not automatic execution recovery |
| Successful return but wrong result | Verify files, evidence, and DAG verdicts before retrying |

Stop or retry only after checking side effects. For scheduled work, canceling a
conversation is not proof that the remote process or campaign stopped. Use
the [Oncall lifecycle](oncall.md) for that case.
