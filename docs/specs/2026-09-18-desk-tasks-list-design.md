# The desk's tasks tab reads one `tasks.list` - design

Date: 2026-09-18
Status: designed
Base: `c418e0bb` on `refactor/ui_web_architecture`. Line anchors below were taken on that commit.

## Goal

The desk's tasks tab (`ui-web/src/features/tasks/`, #488) draws every unit of delegated
work a conversation started -- a `spawn` call or a `run_subagent_dag` run, a playbook run
being the latter -- but its live source answers the empty list: there is no wire method
that returns tasks as tasks. Today a run is N node rows on `subagent.list`, the run-level
title and edges live behind `dag.get` (which needs the live tool), templates and usage
behind `dag.node` / `subagent.context`. This design adds one read, `tasks.list`, that
returns a run-level row per task with its nodes inline, built from the records the
runtime already writes, and reuses every other surface the tab needs.

## Non-goals

- Resuming an interrupted run or re-running one failed node. No runtime primitive exists
  (a new graph may only reference an earlier run's *completed* nodes); the mock hedges both
  actions as illustrative.
- Token-level streaming of a sub-agent's answer. The live transcript is republished per
  tool result.
- Cron jobs. They are a session per job, in the rail and in Settings, not a task.

## What is already there

Three stores hold a task, and the design names them because the reader that gets one of
them wrong reads a stopped run as still running:

| Store | Path | Writer | Holds |
|---|---|---|---|
| run dir | `<session_dir>/subagents/mas_dag/<run_id>/graph.json`, `manifest.json` | `DagRunStore.init`, `_finalize -> write_manifest` | the graph (edges, templates, `skills`, `mcps`, `inputs`, `instance`, `confirm`, top-level `replan`); per-node terminal state once finalized |
| session node registry | `<session_dir>/subagents/nodes.json` (`dag_store.REGISTRY_FILENAME`) | `record_nodes`, `ensure_node_claimed`, `record_outcome` | `nodes{<id>: {kind: dag\|spawn, run_id?, status, started_at_ms, ended_at_ms?, has_output?}}`; the only place a hard-stopped run's `cancelled` / `skipped` land |
| instance registry | `~/.raven/subagent_instances.json` (`raven.agent.subagent.instances.get_registry`) | `upsert_dag_node`, `upsert_spawn` | rows `kind in {cli, acp, dag-node}` with `runId`, `nodeId`, `status`, `createdAtMs`, `updatedAtMs` |

Node files sit in the flat namespace `<session_dir>/subagents/nodes/<node_id>.{prompt,out,
error}.md`, `.meta.json`, `.transcript.jsonl` (older records: `mas_dag/<run>/<node>.*`; the
manifest carries absolute paths either way).

Two facts about ids decide the wire shape. A spawn has a **task id** (the manager's
`uuid4[:8]`, `manager.py:829`) that `subagent.status` and `subagent.interrupt` use, and a
**record id** (the model-chosen `node_id`, required by the spawn tool,
`spawn_tool.py:470-474`) that `subagent.list` reports as `id`. `handle` is
`instance or task_id`, so an unnamed spawn's handle is its task id.

A hard stop (`subagent.interrupt` on a run id -> `manager.cancel_by_id` -> `task.cancel()`)
enters `run_dag`'s `except CancelledError` (`dag_runner.py:671-694`): `_finalize` never
runs and `manifest.json` is never written; `_mark_stopped` + `_record_outcome` write
`cancelled` / `skipped` into `nodes.json` only, and the instance registry's rows stay
`running`. Today's readers (`subagent.list`, `dag.get`) consult manifest and instance
registry and so report such a run as `interrupted`.

## Design

### 1. `tasks.list(session_key, kind?, id?)` -> `{tasks: TaskRow[]}`

One read per conversation, nodes inline, no bodies (messages, output, rendered prompt
stay on `dag.node` / `subagent.context`). Read from the three stores and the node files;
no dependency on the live `run_subagent_dag` tool. Liveness comes from
`dag_live.live_run_ids(loop)` and `manager.live_handles(session_key)`; with no loop,
nothing is live. `kind` + `id` narrow to one task; ids are unique per kind, not across.
Sorted by `started_at` descending; a dag row whose nodes have not started sorts by the
UTC prefix of its `run_id`.

`TaskRow`: `id`, `kind` (`spawn` | `dag`), `task_summary` (null on a run that predates the
field), `status` (`running` | `completed` | `failed` | `interrupted` | `cancelled`),
`replan?` (`{run_id, from_node, reason, started, error?}` from `graph.json`), `playbook`
(derived: the node-id prefix `<sanitized name>-<6 hex>-` matched against the library's
names; null otherwise), `started_at` / `ended_at` (epoch ms), `agent` (spawn only),
`handle` (spawn only; what `subagent.cancel_instance` takes), `counts` (`total`,
`pending`, `running`, `completed`, `failed`, `skipped`, `cancelled`, `interrupted`,
`exception`), `nodes[]`.

`TaskNode`: `node_id`, `node_summary`, `agent`, `instance`, `status`
(`DagSnapshotNodeStatus`), `depends_on` (may name a node outside this task: the id space
is the session's), `started_at` / `ended_at`, `error` (capped at 500 chars -- the manifest
stores it uncapped; a spawn's is the head of `.error.md`, or of `.out.md` when `aborted`),
`tokens_in` / `tokens_out` (null = the lane cannot report; never zero), `tool_call_count`
(null = zero or unreported, an acknowledged ambiguity of `as_meta`), `tool_failure_count`,
`has_output`, `prompt_template`, `inputs`, `skills`, `mcps`, `files[]`
(`{path, op: write|edit, add, del, size}`). While a node runs, the usage, the tool counts and
the files are read off the activity being collected for it in this process (the same
in-memory account `subagent.context` and `dag.node` serve a transcript from), since the record
on disk carries them only once the run finishes; what the lane has not reported yet stays null,
and a node that is not running takes nothing from that index (its key is a record id unique per
conversation only). A dag node that has finished while its run has not keeps the account the
runner set aside for it at its end (`activity.record_settled`) until the manifest is written,
so its usage does not vanish between the two.

### 2. Status derivation

Node status, first source that has a value: `manifest.json[<id>].status`; else
`nodes.json` `nodes[<id>].status` (the sentinel `unrecorded` reads as no value); else the
instance registry's `dag-node` row; else `pending`. Then, when the run is not live and
the status is `pending` / `running` / `exception`, `interrupted` -- the three-state rule
`read_run_reconciled` applies (`dag_resume.py:71`); `_dag_rows` omits `exception` and is
corrected in the same change.

A spawn node maps meta `status` (`running` / `completed` / `failed` / `aborted` ->
`failed` / `cancelled`); with no status, `.error.md` present -> `failed`, `.out.md` present
-> `completed`, else `running`; `running` with `(agent, handle)` not in
`manager.live_handles` -> `interrupted`.

Task status, first rule that matches: (0) `graph.json` carries `replan`: `started` ->
`cancelled` with `replan`, else `failed` with `replan.error` as the reason; (1) any node
`failed` -> `failed`; (2) any `interrupted` -> `interrupted`; (3) any `pending` /
`running` / `exception` -> `running`; (4) any `cancelled` or `skipped` -> `cancelled`;
(5) `completed`. Rule 4 says *any*: `_mark_stopped` leaves completed nodes alone, so a
run stopped after its first node finishes ends as `{completed, cancelled, skipped}`.

### 3. Stop: reused

DAG: `subagent.interrupt(subagent_id=<run_id>)` -- every run is adopted into the manager's
task index (`adopt_background_run`, `manager.py:731`). Spawn:
`subagent.cancel_instance(session_key, agent, handle)` with `TaskRow.handle`. Both reach
only runs started by this gateway process. After a hard stop the row reads `cancelled`
because `tasks.list` consults `nodes.json` (section 2); a pending spawn cancelled before
dispatch leaves no record and is withdrawn by the `subagent.status{cancelled}` frame.

### 4. Node context: reused

`dag.node(run_id, node, session_key)` and `subagent.context(id, session_id)` return
`messages[]` in the `session.resume` shape; the tab draws them with the transcript's own
renderer, including the synthetic `role=console` row an in-flight `cli` lane emits. The
record is re-read on every status transition and, for a dag node, on each `dag.node_updated`
frame; a spawn gets no per-step frame, so its record is re-read on a one-second beat while it
runs (a beat is skipped while a read is still out), the cadence the transcript's spawn card
already reads on. Each such read of a running node also re-reads its row through
`tasks.list(kind, id)`, which is how the panel's token total moves during the run.

### 5. Live updates

`dag.run_started` builds a row (`status=running`; `nodes[].subagent` becomes `agent`; no
timestamps, so `started_at` starts from the run id's prefix); `dag.node_updated` moves a
node (timestamps only on `running` / `completed` / `failed` / `exception`);
`dag.run_completed` ends a row (a hard stop sends `{stopped: true}` and no `files`);
`dag.run_replanned` marks the old row `cancelled` with `replan`; `subagent.status` builds
a spawn row at `pending` with `handle = instance ?? task_id`, and `call_id` from `running`
is the row's `id`. Terminal transitions are not replayed by `session.resume`, but
`turn.subscribe` replays the in-flight turn's whole buffer (`subscriptions.py:80-116`),
so the store is idempotent on `(kind, id, status)` and `tasks.list` is the source of
truth after a reconnect or a terminal event.

### 6. Two backend changes beside the read

- **Files per node.** No sub-agent has `deliver_files`, and neither manifest nor meta
  records what a node wrote. `RunActivity` gains `files`, filled where the in-process lane
  already sees the tool result (`raven_loop.py:485`, `result.file_change` / `result.diff`
  from the filesystem tools), and `as_meta()` carries it into the manifest entry and the
  spawn meta. The acp and cli lanes record none in this change.
- **The spawn receipt names its record.** `_announce_result`'s delegated mark gains
  `node_id` (`manager.py:1881`; the dag mark already carries `run_id`), so the transcript's
  receipt row can open the task.

### 7. The page

`features/tasks/` derives its types from `generated.ts`, reads through `tasks.list`, and
draws the mock: the list row, the strip (three chips and an overflow), the running count on
the tab, the pane (status bar with `interrupted` and `cancelled`, a stop action, the why
banner naming the failed node, file and diff chips, the board with tool counts and a lane
per shared `(agent, instance)`), the node panel (header with the agent, its status word, the
duration and the token total when the lane reported one, context and order tabs, a chat dock
for a stateful agent's instance). The desk's
deliverables and diff tabs gain a task-derived group beside the session-level rows; a
task file's diff is built on click from the node's messages with the page's existing hunk
builders. The offline page answers `tasks.list` from `src/rpc/fixtures/tasks.ts`.

## Wire conventions

`rpc-schema/openrpc.json` is the source; `raven/rpc/models.py` mirrors it
(`tests/test_rpc_schema_match.py`); both `ui-web/src/rpc/generated.ts` (`npm run gen`,
`gen:check`) and `ui-tui/src/rpc/generated.ts` (`npm run gen:rpc`, `lint:rpc`) are
regenerated. Fields with a meaningful null are declared `["<type>", "null"]` and left
out of `required` (the schema-match test strips nullable fields from the pydantic side
before comparing, so a nullable field listed as required fails); arrays and objects are
non-null on the schema side. `replan` is the one optional object. Timestamps are epoch milliseconds, as on
`DagSnapshotNode` and the `dag.*` events. Node status vocabulary is
`DagSnapshotNodeStatus`. The agent field is `agent`, as on `SubagentCall` and
`InstanceRow`.

## Domain terms

**Task** is added to `CONTEXT.md` beside Task summary and Node summary, and to
`ui-web/CONTEXT.md`.

## Testing

- `tests/test_rpc_tasks.py`: rows for a spawn and a dag built with the runtime's own
  writers; the status ladder (hard stop reads `cancelled` from `nodes.json`; a run nothing
  is executing reads `interrupted`, `exception` included; a replan that did not start reads
  `failed`); counts from `graph.json` including `skipped`; the null semantics; `files`.
- `tests/test_rpc_schema_match.py`, `npm run gen:check`, `npm run lint:rpc`.
- `ui-web`: the tasks domain's tests against the new shape, the fixture gates, the boot
  golden regenerated, and the desk tests for the tab order and the running-count badge.
