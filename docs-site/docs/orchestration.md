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
      "subagent": "Raven-General",
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
| `routes` | Route name to condition. Makes this node a decision: the verdict call picks the route from its answer |
| `select` | `"one"` (default) or `"many"`: how many routes one answer may select |
| `when` | Guard. The node runs only if the condition holds once its dependencies settle |

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
re-running them; the saved output must exist. Earlier failed, skipped,
canceled, interrupted, not-executed, or still-running nodes are not valid
inputs. Ids are case-folded for uniqueness and remain claimed after failure.

File references are confined to the working directory and the current
conversation's sub-agent history. They do not expose the whole Agent home,
memory store, skill tree, or another conversation. `@nodes/` names the current
conversation's flat node-artifact directory.

## Conditional branches

A graph can decide where it goes next. A node that declares **`routes`** is a
**decision node**: each route name maps to the condition, in plain words, under
which the node's answer selects that route. **`select`** says how many routes one
answer may select: `"one"`, the default where routes are given, or `"many"`, any
number including none. Any node, a decision node included, may carry **`when`**,
a guard over what the decision nodes before it chose.

The verdict call that checks a decision node's answer also picks its route. The
conditions are the graph author's criteria: they go to the judge and are not added
to the node's prompt, and the node's own text never selects a route.

| Field | Meaning |
| --- | --- |
| `routes` | Route name to condition. At least two routes for `select: "one"`, at least one for `"many"`; names use the node-id charset (letters, digits, `_`, `-`), no two may differ only in case, and `done` is reserved in any letter case |
| `select` | `"one"`: exactly one route, so give the decision a catch-all route for everything else. `"many"`: any number, none included. Only on a node with `routes` |
| `when` | Guard, read once every dependency has settled |

A guard's atoms are `<node>.<route>`, true when that decision node selected that
route, and `<node>.done`, true once it settled, whatever it chose. An atom names a
decision node of this graph that appears in the guarded node's own `depends_on`.
Atoms combine with `not`, `and`, `or`, and parentheses; `not` binds tightest, then
`and`, then `or`. Every decision node in a node's `depends_on` must appear in its
`when`; write `<node>.done` where the route does not matter. A node whose
dependencies in the graph were all left out is left out with them, so a branch
node needs no guard beyond naming any decision node it depends on. A node of
the branch that also depends on something outside it, the decision node included,
needs the branch's route in its own `when`, as `<node>.<route>` rather than
`<node>.done`, which holds whichever route is selected. A merge node that should
run on every route keeps `<node>.done`.

A node ends `not_executed` when its guard is false (`when_false`), when the guard
is unknown because a decision it names was never made (`when_unknown`), or when
every dependency it has in the graph was itself not executed
(`upstream_not_executed`). It is never dispatched and writes no output, and unlike
a skipped node it does not skip its dependents. A node of the same graph that
reads one receives this sentence where the output would have been:

```text
<This task is not executed and the output is missing, please go on without this output.>
```

The stand-in is for the same graph only: a later graph that names a node an
earlier run did not execute is refused, as it is for a failed node (see
[Node contract and data flow](#node-contract-and-data-flow)).

A node may not run when it has a `when` of its own, `<node>.done` included, or
when it depends on nodes of the graph that all may not run. When a template reads
such a node, the rendered prompt also carries a note naming those inputs. Raven
adds no titles of its own, so give each output that may be missing its own titled
block in the template. Read such a node with `{{ <id>.output }}`: a `ref:@nodes/`
path or `{"file": ...}` input aimed at its artifacts is refused at submission,
because a node that is not executed writes no file.
The check matches node ids without regard to case on every host, as the session's
node namespace does, so changing a filename's case cannot evade it.

A decision node whose route cannot be established fails, with the reason recorded
on the node; it does not fall back to a default route, and its dependents are
skipped. A host with no model available to judge refuses a graph that declares
routes before anything is dispatched.

## Scheduling and shared limits

Before dispatch, Raven checks required fields, ids, dependencies, cycles,
input contracts, routes and guards, path confinement, and backend
capabilities. Independent ready nodes run concurrently, sharing the host's
semaphore with `spawn` and other DAGs. `agents.defaults.maxConcurrentSubagents`
defaults to 8; `agents.defaults.maxSubagentSpawnsPerHour` defaults to 30 per
session and also bounds repeated DAG submissions.

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
| `not_executed` | A branch left the node out: its guard was false or unknown, or none of its dependencies in the graph was executed. Never dispatched, no output |
| `cancelled` | The active node was canceled |
| `interrupted` | A history reader found unfinished recorded work without a live run |

A model-based **verdict** checks whether the returned output actually
accomplished the node's prompt, using output, transcript tail, and available
transport stop reason. A normal process return alone is not enough. However,
verdicts are a quality check, not a safety gate: for an ordinary node, a judge
failure/timeout falls back to accepting a normal backend return. A decision node
has no route to fall back to, so it fails instead (see
[Conditional branches](#conditional-branches)).

For an exception, `resolve_dag_node` can continue with corrective instructions,
abandon the node, or replan. Replanning finalizes the old run and starts a new
one; reuse completed outputs by reference and assign fresh ids to replacement
nodes. Failed prerequisites eventually skip their dependents; independent
branches need not be discarded.

The `subagentDag` settings default to a 180-second judge timeout, a 600-second
background adjudication window, and two continuations per node. These bound
additional model calls and waiting; they do not prove an external action is safe
to retry. Check for already-created files, messages, or remote jobs before
continuing a node with side effects.

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
