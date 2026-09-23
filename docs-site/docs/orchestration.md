# DAG orchestration

Use a directed acyclic graph (DAG) when a task has parallel workstreams and
explicit dependencies. Raven validates the graph, dispatches its nodes to the
available agents, and records their results. This is orchestration inside a
Raven host, not a durable distributed workflow service.

## Choose the delegation shape

| Shape | Use it for | What is shared |
| --- | --- | --- |
| Direct turn | Work that needs no delegation | The current conversation |
| `spawn` | One focused sub-agent task | An explicit task and optional instance |
| `run_subagent_dag` | Several tasks with dependencies | Node outputs, references, run status |
| [Playbook](playbooks.md) | A reviewed workflow used repeatedly | A stored procedure, parameters, and any carried configuration |

ACP, CLI, built-in, and OpenAI-compatible backends can appear in the local
roster. A2A peers are separate: `a2a_send` calls a remote host, not a DAG node
backend named `a2a`. See [Agent protocols](agent-protocols.md).

## Dynamic workers and task charters

The normal roster lists registered agents. Opt-in generation can instead
prepare a **Worker Table** for a turn: each worker has a label, an underlying
agent, and a task-specific brief. Two workers may use the same agent with
different responsibilities; they are not two newly installed agents.

Merge this fragment into the host configuration and reload/restart the host
so the next turn uses it:

```json
{
  "playbooks": {
    "agentHarness": "generate"
  }
}
```

The default is `"default"`, with no generated table. Generation adds model
work before dispatch; a generation failure logs a warning and falls back to
the unconfigured turn. It neither selects a task graph by itself nor removes
the main agent's own tools. The model still decides what to delegate.

| Layer | Lifetime | Meaning |
| --- | --- | --- |
| Roster agent | Registered configuration | Which backend can execute work |
| Worker Table row | One host turn | Label and responsibility for this task |
| Charter | One dispatched worker turn | Narrowed instructions, tools, checks, and optional deadline |
| Stored Playbook | Reusable file | Authored graph/guidance and parameters |

Use the current tool's worker labels in generated-mode `spawn`, DAG, and
replan arguments. A stored Playbook's private DAG uses underlying registry
names instead, so a coincidentally identical label cannot rewrite the saved
procedure.

A **Charter** travels to supported Raven ACP workers through
`_meta["raven.playbook"]`, or is bound locally for the built-in worker.
It intersects the worker's existing permissions: it may narrow a tool set or
deadline, not restore a disabled tool or extend a configured deadline.
Arbitrary external ACP agents do not necessarily interpret this metadata.

Declarative checks and optional generated judge code are separate mechanisms.
The code is admitted through an AST allowlist; rejected code is dropped with
a warning, and a judge exception does not become an automatic denial.
Charters are not an OS sandbox or a fail-closed security boundary. Keep
[permissions](permissions.md) and backend isolation independently configured.

For hands-on instance follow-up and steering, use
[Working with sub-agents](agent-collaboration.md). Implementation lives in
`raven/agent/subagent/delegate.py`, `charter.py`, `charter_code.py`, and
`raven/playbook/agent_generator.py`.

## A first graph

First configure the needed agents and choose the conversation's working
directory. Ask Raven to inspect two independent aspects of a project, then
synthesize the findings without editing files. The model can submit the
following **tool arguments**, not a shell command or standalone config file:

```json
{
  "task_summary": "Review project readiness",
  "background": false,
  "nodes": [
    {
      "id": "api_audit",
      "subagent": "Raven-Code",
      "node_summary": "Inspect API contracts",
      "prompt_template": "Read the public API and report compatibility risks with file references. Do not edit files."
    },
    {
      "id": "test_audit",
      "subagent": "Raven-Code",
      "node_summary": "Inspect test coverage",
      "prompt_template": "Read the tests and report missing coverage with file references. Do not edit files."
    },
    {
      "id": "readiness_summary",
      "subagent": "Raven",
      "node_summary": "Synthesize readiness findings",
      "depends_on": ["api_audit", "test_audit"],
      "prompt_template": "Combine these findings into a prioritized review. Do not edit files. API: {{ api_audit.output }} Tests: {{ test_audit.output }}"
    }
  ]
}
```

The first two nodes can run concurrently; the last waits for both. Substitute
names from the current tool's advertised roster. When the host generates a
worker table, use its worker labels rather than the underlying agent names.
Choose fresh node ids when submitting another graph in the same conversation.

For writing tasks, divide file ownership or use separate working copies:
parallel nodes do not imply separate worktrees or exclusive file locks.

## Node contract and data flow

| Field | Meaning |
| --- | --- |
| `id` | Task address, unique across the conversation, including earlier DAGs and `spawn` calls |
| `subagent` | Agent or worker label advertised by the current tool |
| `node_summary` | Short user-visible step title |
| `prompt_template` | Task instructions and input placeholders |
| `depends_on` | Prerequisite node ids |
| `inputs` | Literal strings, `{"file": "path"}`, or `{"node": "id"}` |
| `skills` | Omitted: the agent's menu; `[]`: no skills; list: narrowed menu where supported |
| `mcps` | Omitted: the row's defaults; `[]`: no attached MCPs; list: replace the selection |
| `instance` | Reuse a sub-agent session, not a node output address |

The graph requires `task_summary` and `nodes`. `background` belongs to the
tool call; `confirm` is a graph-wide gate, defaulting to false. The model-facing
schema uses snake_case; stored playbooks also accept camelCase node fields.
Unknown node fields are rejected rather than silently becoming instructions.

Useful prompt forms are `{{ inputs.brief }}`, `{{ api_audit.output }}`, and
`{{ ref:brief.md }}` for contents; `{{ inputs.brief.path }}`,
`{{ api_audit.output_path }}`, and `{{ ref_path:brief.md }}` pass paths to a
backend that can read local files. A path on this host is not a file transfer to
a remote service. Prefer content forms when the backend has no local-file access.

References to another node in the same graph must declare their dependency.
Earlier completed nodes in the same conversation can be referenced without
re-running them; the saved output must exist. Failed, skipped, canceled,
interrupted, or still-running nodes are not valid inputs. Ids are case-folded
for uniqueness and remain claimed after failure.

File references are confined to the working directory and the current
conversation's sub-agent history. They do not expose the whole Agent home,
memory store, skill tree, or another conversation. `@nodes/` names the current
conversation's flat node-artifact directory.

## Scheduling and shared limits

Before dispatch, Raven checks required fields, ids, dependencies, cycles,
input contracts, path confinement, and backend capabilities. Independent ready
nodes run concurrently, sharing the host's semaphore with `spawn` and other
DAGs. `agents.defaults.maxConcurrentSubagents` defaults to 8;
`agents.defaults.maxSubagentSpawnsPerHour` defaults to 30 per session and also
bounds repeated DAG submissions.

Nodes sharing an `instance` are serialized. Add dependencies when their order
matters; a shared instance alone is not a deterministic ordering rule. Backends
without session continuation cannot provide state merely because an instance
was supplied. Unsupported optional skill/MCP narrowing is reported as a
downgrade; inspect those notices before relying on the restriction.

## Foreground, background, and approval

`background` defaults to true: the tool returns a run id, and later results are
announced to the originating conversation. Use false when the main agent needs
the outputs before continuing. A foreground call returns either the final
result or a node exception report; answering with `resolve_dag_node` continues
the wait.

A foreground run is **bound** while its originating turn is active. When that
turn ends, it is **released** and later reports follow the background path.
Bound exception waits have no adjudication deadline; released/background waits
are bounded. "Background" does not mean the run survives process termination.

Set `confirm: true` when the user should approve the whole graph before any
node runs, especially for sending, publishing, or spending. **With no ask channel
wired, the current implementation logs a notice and runs unconfirmed.** With an
ask channel, refusal or a delivery error stops dispatch. This flag is not an
unattended security gate. Approval does not bypass individual
tool policy, and delegating through ACP may auto-approve the child's own
protocol requests. Read [Permissions and security](permissions.md) before
running a graph with external effects.

## Failure handling and replanning

| State | Interpretation |
| --- | --- |
| `pending` | Not yet dispatched; dependencies or execution capacity may be outstanding |
| `running` | The node is active |
| `completed` | Accepted by the configured completion path |
| `exception` | Unsuccessful work awaiting a continue, abandon, or replan decision |
| `failed` | Failure is final for this node |
| `skipped` | A prerequisite failed or execution stopped before dispatch |
| `cancelled` | The active node was canceled |
| `interrupted` | A history reader found unfinished recorded work without a live run |

By default, a model-based **verdict** checks whether the returned output
actually accomplished the node's prompt, using output, transcript tail, and
available transport stop reason. A normal process return alone is not enough.
However, verdicts are a quality check, not a safety gate: disabling the judge,
or a judge failure/timeout, falls back to accepting a normal backend return.

For an exception, `resolve_dag_node` can continue with corrective instructions,
abandon the node, or replan. Replanning finalizes the old run and starts a new
one; reuse completed outputs by reference and assign fresh ids to replacement
nodes. Failed prerequisites eventually skip their dependents; independent
branches need not be discarded.

The `subagentDag` settings default to enabled verdicts, a 180-second judge
timeout, a 600-second background adjudication window, and two continuations
per node. These bound additional model calls and waiting; they do not prove an
external action is safe to retry. Check for already-created files, messages,
or remote jobs before continuing a node with side effects.

## Inspect and recover

Ask Raven to use `dag_status` to inspect a run and `cancel_dag` to stop one.
These are model tools, not `raven dag` shell commands. The TUI and WebUI consume
live progress and can read the saved graph; RPC exposes `dag.get` and `dag.node`.

History belongs to the session manager, separately from user deliverables:

```text
<session directory>/subagents/
  mas_dag/<run_id>/graph.json
  mas_dag/<run_id>/manifest.json
  nodes/<node_id>.prompt.md
  nodes/<node_id>.out.md
  nodes/<node_id>.error.md
  nodes/<node_id>.meta.json
  nodes/<node_id>.memory.json
```

Not every file exists for every outcome; an unfinished run may have no final
manifest. Reloading a UI reconstructs recorded state rather than replaying all
live events. After a host restart, unfinished nodes may read as interrupted:
history recovery is **not automatic execution resumption**. Inspect completed
outputs and side effects, then submit a fresh graph for remaining work.

Use `raven tracing` for runtime spans and `raven trajectory --help` for saved
trajectory workflows. Do not publish raw prompts, credentials, or private files
with an incident report. Core implementation lives in
`raven/agent/subagent/dag_graph.py`, `dag_runner.py`, `dag_tool.py`,
`dag_control_tools.py`, and `dag_resume.py` in the same directory.
