# DAG node adjudication: a third decision, `replan`

Status: design, approved 2026-09-04.

## The gap

A DAG node that finishes is judged (`dag_verdict.py`). A node the verdict fails is
suspended as `exception`, its report is announced to the main agent, and the agent
answers through `resolve_dag_node` with one of two decisions:

- `continue` -- send the node a correcting message and let it try again;
- `abandon` -- fail the node and skip its dependents.

Both act on **one node**. Neither can act on the **plan**. When the report says the
graph itself was wrong -- a step that cannot work as wired, a missing step nothing in
the graph performs, work assigned to the wrong sub-agent -- the agent's only options
are to keep retrying a node that will keep failing, or to abandon it and lose every
branch waiting on it. The judgement it actually reached ("this plan needs to change")
has no expression.

## The decision

A third value, `replan`. The agent hands `resolve_dag_node` a new node list; the run
it is answering winds down, and a **new run** is validated and started from that list.
The new run may reference the wound-down run's completed nodes by id, and may add
nodes of its own.

`replan` rather than `reorchestrate`: the existing values are single lowercase verbs,
and "orchestrate" already names the wider act in this codebase (the main agent
orchestrates DAGs; the guide skill is `subagent-dag-orchestration`). A decision value
spelled `reorchestrate` would collide with that broader sense.

## Chained runs, not an in-place splice

The new graph is a **separate run with its own run id and run dir**. The wound-down
run records the link.

The alternative -- splicing the new nodes into the live run -- was rejected. Node ids
are unique per conversation and are claimed at `store.init`, so a splice has to claim
new ids mid-run, overwrite `graph.json` to a cumulative graph (every read-back path
takes structure from that file), make `by_id` append-only so a dropped node does not
vanish from the record, and teach `_finalize` to iterate the effective node set rather
than `spec.nodes`. Chaining needs none of that, and it lands the feature on machinery
that already exists for exactly this shape: `SessionNodes`, `is_readable`,
`_check_earlier_dep` and `{{ ref:@runs/<run_id>/... }}` are all there so that a later
graph can name an earlier run's completed node.

### Ordering is load-bearing

The old run must reach `_finalize` before the new run is **submitted**.
`read_session_nodes` derives a node's state from the run's index entry, and a run whose
entry carries no per-node `status` reads back as `RUNNING` for every node -- neither
reusable nor readable. `_finalize` is what writes those statuses (`_record_outcome`).
So:

1. the decision is validated, the new run's id is minted, and the decision lands on the
   desk;
2. the run winds down and finalizes, its completed nodes becoming `is_readable`;
3. the new run is submitted under that id, validating against the now-settled index;
4. it starts, and the link is recorded on the old run.

The id is minted in step 1, before the hand-off, because the wind-down in step 2 names
it: a node reason reading "superseded by a replan" with no destination leaves the reader
of that run with nowhere to go. Minting early is how this code already works --
`_execute` mints its own run id, and `run_dag` takes one explicitly rather than
generating it, precisely so a caller can key other state to the same id.

Recording the link is step 4 and not step 1, though, and the two facts have different
writers for that reason. The runner knows the id and writes the node reasons; only the
tool knows whether the new run actually started, so the tool writes the link.

Step 1 is therefore validating against an index that does not yet describe the old run,
and cannot use `is_readable` to decide whether a reference resolves. It reads the old
run's per-node state from the live reconciled read instead and **overlays** it on the
session index before the structural checks run. The overlay is the more current of the
two by construction: it is what the finalize in step 2 is about to write. Reference
checks that would otherwise refuse a completed-but-not-yet-recorded node ("that run left
it with no output") then answer correctly.

Two validation passes over the same graph is not redundancy. The first exists to refuse
a bad graph inside the caller's own turn, where the model can fix it and nothing has been
dispatched. The second is the authority, running under `index_guard` at the moment the
ids are claimed -- the same split `_execute` and `run_dag` already have.

### What "reuse an existing node" means

Reuse is by **reference**, never by re-declaration. A completed node of the wound-down
run is named in the new graph's `depends_on` and read through `{{ <id>.output }}`; the
dependency is satisfied on sight, since that run is over.

Re-declaring any id the wound-down run claimed is refused, and this is not a
restriction the design chose -- `SessionNodes` states the reason:

> *is this id taken?* -- `owner`. Every id a run ever claimed, whatever the outcome. A
> failed node still owns its id: its run directory exists and its prompt is on disk, so
> handing the name to something else would make the history ambiguous.

That includes the adjudicated node. **A replan gives up on that node**; redoing its
work means a new node under a new id, which may reference whatever the old one left.

Two consequences follow and are accepted:

- **Attempt budgets do not carry across a replan.** Each node of the new run starts at
  attempt 1 with a full `max_continuations`. There is no per-node counter that spans
  runs, and inventing one would mean tracking identity across ids that are required to
  differ.
- **With no replan cap, the hourly dispatch quota is the only mechanical bound.** A
  replan charges `charge_dag_run` (below), so a runaway replan loop terminates against
  `max_subagent_spawns_per_hour` (default 30 per session, rolling hour) with the
  existing refusal text, which already says the task may be looping.

## Winding the old run down

A replan cancels in-flight nodes immediately rather than letting them finish.

This needs a signal the current scheduling round can see. `_await_adjudications` is
reached only when the ready set is empty **and** the round's group tasks have all been
awaited, so a decision that merely sits on the desk is not applied until every running
node has finished on its own -- which is the opposite of cancelling them. So
`AdjudicationDesk` gains a `replanned: asyncio.Event`, set by `resolve` when the decision
is `replan`, and `_run_ready_groups` races the round against it as it already races
`cancel`. In-flight node tasks are then cancelled the instant it fires, releasing their
semaphore slots.

The event belongs on the desk rather than in a fourth per-run index on the tool: the desk
is already the per-run rendezvous that the tool writes to and the runner holds, so
nothing new has to be threaded from one to the other.

`cancel` itself is deliberately **not** reused for this. It carries two meanings the
replan path needs to differ on: `_mark_stopped` maps `exception` to `cancelled`, where
a replanned run's adjudicated node must read `failed` with a reason naming the new run;
and `cancel.is_set()` is what suppresses the run's result announcement on the grounds
that the user stopped it, which is a different fact from "the agent replanned it".

Statuses on wind-down:

| Node state when the replan lands | Becomes | Reason recorded |
|---|---|---|
| `completed` | unchanged | -- |
| `running` | `cancelled` | cancelled by the replan |
| `exception` (the adjudicated node) | `failed` | `Superseded by replan into run <new>: <reason>` |
| `exception` (any other still open) | `failed` | same |
| `pending` | `skipped` | same |

The reason string names the new run id in every case. Without it, someone reading the
old run afterwards sees a batch of failures and skips with no way to learn that the work
continued elsewhere.

A cancelled in-flight node is not `completed`, so the new graph cannot reference it by
id. If its attempt output file happened to land, that file is reachable by path through
`{{ ref:@runs/<old_run_id>/... }}` -- an existing capability, not something this design
adds.

## The tool surface

`resolve_dag_node` gains one parameter and widens two.

- `decision` accepts `replan`.
- `nodes` -- required when the decision is `replan`. Its schema is the graph tool's own
  node schema, reached through a public accessor on `SubAgentDagTool` rather than
  copied. A copy would drift from `run_subagent_dag`, and the `subagent` enum is built
  from the hot-appliable agent table.
- `message` -- already required for `continue`; now required for `replan` too, as the
  recorded rationale. It is what the wound-down run's node reasons cite; without it that
  run's record shows a batch of superseded nodes and no reason for any of them.

The decision travels as today, through `SubAgentDagTool.resolve_node` to the run's
`AdjudicationDesk`, with `Adjudication` carrying a new `plan: ReplanPlan | None`.
`ReplanPlan` is a frozen dataclass holding the parsed `nodes`, their resolved dispatch
backends, the instance handles minted for them, the reason, and the run id minted for
the new run.

The backends travel **in the payload** rather than being resolved by the runner. `_run`
passes `resolve=lambda node: dispatch_backends.get(node.id)` -- a closure over a dict
`_run` owns. The tool has no legitimate route into it, and having the tool mutate it
would be action at a distance.

`blocking_for` keeps its shape: a replan on a bound foreground run goes on blocking,
because the call said it was waiting on this graph and the graph is still what is being
waited on.

## Validation

Tool-side, in the resolve call, for the reason `_execute` already states: a refused
graph must cost zero sub-agent dispatches and must be refused in the caller's own turn,
where the model can fix it. The new run's own submission re-validates and claims its ids
under `index_guard`, as every run does.

The tool needs the old run's structure and state, and reuses `dag_reader`'s own split
for it: structure from `graph.json` (which holds `spec.model_dump_json()`, so
`parse_dag_spec` restores real `DagNodeSpec` objects), state from `read_run_reconciled`,
which `ResolveDagNodeTool._read_live` already calls.

Ahead of all of it, in the resolve call itself: **is that node actually suspended?**
`dag_live.awaiting_decision` asks every graph tool the hand-off would ask, and a definite
no refuses there. Everything below costs something a refused replan must not spend -- the
reconciled read, the confirm question, the quota, the minted instances -- and none of it is
refunded when the hand-off further down then finds nobody waiting. The predicate is exactly
the condition `resolve_node` fails on (both read the run's desk), so it refuses nothing the
hand-off would have accepted; it answers `None` where no instance implements it, which
proceeds. The hand-off stays the authority for the node that stops waiting *during* the
validation this skips ahead of.

Checks, in `_execute`'s order and for its stated reasons:

1. `_is_paused` -- a replan dispatches sub-agents, so a paused delegation refuses it.
2. `parse_dag_spec` field-level validation. `task_summary` and `confirm` are inherited
   from the old run's spec: the goal has not changed, only the plan, and
   `task_summary` names the goal.
3. Structural validation of the new graph:
   - a `depends_on` entry or `{{ <id>.output }}` reference naming a node of the old run
     resolves only if that node is `completed`; `_check_earlier_dep` and `is_readable`
     already say this, reading the overlaid state described above rather than the index
     alone;
   - re-declaring any id the old run claimed is refused, with advice pointing at
     reference-or-rename;
   - `collect_static_graph_errors` for the rest -- cycles, `{{ dep.output }}`
     default-deny, input contracts, file references inside `roots`, session-wide id
     uniqueness.
4. `validate_capabilities`, agent table membership and `enabled`, MCP grant resolution
   with its downgrade notices.
5. `machines_verdict_async`, `unnamed_machine`, `with_machine_facts` -- last of the
   pre-dispatch checks, being the only one that leaves the process.
6. The `confirm` gate, when the old run's spec carried `confirm: true`. The gate is
   graph-level and its question shows the whole graph, so approving one graph is
   approving each of its steps; a replan replaces that graph, and re-asking is what
   keeps the earlier approval meaningful. With no ask channel wired it proceeds and
   logs, as the existing gate does.
7. `charge_dag_run`. A replan submits a new graph and a fresh batch of dispatches,
   which is the unit this quota counts ("a run counts once however many nodes it
   carries"). Not charging would let one run bypass the session's hourly limit
   indefinitely by replanning.
8. `_mint_missing_instances`, then the payload goes to the desk.

Steps 4 and 5 are inline in `_execute` today. They move into a shared preflight on
`SubAgentDagTool` that both paths call. Without that extraction a replan is a route
around the agent table, the capability checks, MCP resolution, the machines check, the
pause gate and the budget.

## Persistence

The new run persists as an ordinary run: its own run dir, its own `graph.json`, its own
manifest and index entry. Nothing about an existing reader changes.

The old run's `graph.json` gains a top-level key recording the replan:

```json
{"task_summary": "...", "nodes": [...], "confirm": false,
 "replan": {"run_id": "<new run id>", "from_node": "<adjudicated node>",
            "reason": "<the decision's message>", "decided_at": 1234567890000,
            "started": true}}
```

All four readers of that file -- `dag_reader.read_run`, `instance_records`,
`rpc/methods/instances`, `rpc/methods/subagent` -- read it as a raw dict and take only
the keys they want, so the added key is inert to every one of them.

One hazard to record at the write site and in `dag_reader`'s module docstring, which is
where the "structure always from `graph.json`" contract is declared: `SubAgentDagSpec`
is `extra="forbid"`, so a future reader that parses this file through the model would
reject it. The key is reserved and must stay out of the model.

## Events and the front ends

The new run emits its own `dag_run_started`, so both front ends draw it with no change
to `fromStart` and no change to the invariant that only `dag.run_started` builds nodes.

One new event links the two: `dag_run_replanned`, carrying
`{run_id, replan_run_id, from_node, reason}`. It is emitted by the tool, not by the runner
at wind-down -- the runner is not the one that knows a decision was accepted.

**It fires the moment the desk accepts the decision**, gated on `resolve_node` returning
True, and before the old run is awaited. Not after the successor starts, which is where
this design first put it. That earlier placement made the event unreachable in the web UI,
and the reason is worth keeping because it is not obvious from either side alone.

The TUI pins one run state per run id permanently, so it can absorb a link event whenever
it arrives. The web UI does not: `dagFeed` resolves a card only through `dagLive`, keyed by
run id, and its `dag.run_completed` branch deletes that entry -- deliberately, since a stale
claim there is worse than a dropped update. The sheet is stricter still, replacing whatever
a conversation was watching on every `dag.run_started`, because one run at a time is what a
sheet can show. So an event emitted after the successor starts arrives after *both* the
purge and the eviction, and resolves to nothing on either surface -- while its unit test,
which never simulates those two frames, passes. Dead code that looks done.

Emitting on acceptance costs one thing and it is the same thing already paid elsewhere: the
successor is named a moment before it exists. The wind-down's node reasons already do this,
for the same reason -- the reader needs somewhere to go -- and the id is minted before the
hand-off precisely so they can. The gate on `resolve_node` is what keeps that promise
honest: on the path where nobody was waiting, the old run continues as submitted, no
successor is dispatched, `record_link` never runs, and so nothing is announced either.
Nothing is charged either, and that one is not the gate's doing -- the pre-check above is,
since by this point `charge_dag_run` has already run. Reaching this branch at all now means
the node stopped waiting inside the validation, which is the only case that still pays.

`record_link` stays where it is, after the submission. The event is a live hint and may be
optimistic; the durable record is an audit and may not.

A new wire event is not a two-file change. The contract has one source of truth and two
generated consumers, and CI gates the drift:

| Layer | File | Gate |
|---|---|---|
| contract | `rpc-schema/openrpc.json` | `tests/test_rpc_schema_match.py` |
| server models | `raven/rpc/models.py` (payload, event, the `TurnEvent` union, `__all__`) | same |
| progress bridge | `raven/rpc/spine.py` (`_DAG_WIRE_EVENT`, `_dag_payload`) | -- |
| acp surface | `raven/acp/updates.py` | -- |
| TUI types | `ui-tui/src/rpc/generated.ts` | `npm run lint:rpc` |
| web types | `ui-web/src/rpc/generated.ts` | `npm run gen:check` |

Both generated files are committed and regenerated by their own scripts
(`npm run gen:rpc` in `ui-tui`, `node scripts/gen-rpc-client.mjs` in `ui-web`); a schema
edit that skips either one fails CI rather than the build.

On top of that, the TUI side is the `DagEvent` union in `domain/dagRun.ts`, a branch in
`foldDagEvent` recording `replannedInto`, the dispatch in `app/chatStream.ts`, and a line
of rendering; the web side is the same fold in `features/dag/` plus the dispatch in
`features/transcript/store.ts`. Without any of it the user sees two unrelated graphs, one
of which stopped for no visible reason.

The old run's own result announcement is suppressed: the story is that it became
another run, and announcing "3 completed, 1 failed, 2 skipped" narrates to the agent
what it just decided. `DagRunResult` carries `replanned_into` and `_run_detached`
checks it -- the same shape as the existing rule that a cancelled run announces
nothing, for the same reason. The resolve call's return value carries both halves: how
the old run settled, and that the new one started.

Suppression is checked before `_run_detached` reaches its `if outbox is not None`
branch, so both lanes are silenced by the same decision.

That ordering is load-bearing, and the reasoning that first put the check below the
outbox branch is worth recording because it was wrong. It ran: `await_finalized` waits
on the run's task, not on its outbox, so nothing is parked to take the `Final` that
lands there; it sits unclaimed, and `_retire` -- a done callback, firing immediately
after -- evicts that outbox before anything can drain it. Silence by orphaning, in
which case a bound run never needs the check.

That holds only for a tray nobody has released. `put_final` on a *released* tray
announces the outcome on the spot, with no window for `_retire` to win, so a replan
decided on a foreground run whose turn had already been released narrated the very
wind-down the suppression exists to prevent -- and, having no rendering path, narrated
it as a raw dataclass. An accident that holds under one of two tray states is not a
decision; the check is now one.

Moving it costs the bound caller nothing it was waiting for. Tool calls are dispatched
one at a time, so nothing else can be parked on the same run's tray; the replan branch
never calls `await_run` itself; and the taker `_dispatch` opened was satisfied by the
suspended node's own report, which a replan requires. The `Final` the old order buffered
into that tray was collected by `_retire` unread.

## Foreground runs

On a bound foreground run the binding transfers to the new run. The resolve call waits for
the old run's **task** to end -- that, not any outbox event, is the signal that it has
finalized and therefore that the new graph can be validated against a settled index --
then submits the new run with an outbox bound to the same turn, and awaits that.

Waiting on the task rather than on the outbox is what makes the wait correct for both
lanes with one mechanism: a backgrounded run has no outbox to wait on at all.

Letting the call return when the old run settles, leaving the new one in the background,
would silently cancel the foreground semantics the caller asked for: `background: false`
means it cannot continue without the outputs, and a replan is the same pursuit.

A backgrounded run needs the same "has it finalized" signal, which is a small
`await_finalized(run_id)` accessor on the graph tool, over the task it already indexes
in `_runs`.

## Losing the race on ids

Tool-side validation runs before the old run has finalized its wind-down; the new run
claims its ids afterwards, under `index_guard`. A concurrent run of the same session can
take one of those ids in that window.

The new run's submission then fails validation like any other graph, and the failure is
reported to the agent as the resolve call's result (foreground) or as an announcement
(backgrounded). The old run stays wound down. This is deliberately the simple branch
rather than re-opening the desk for another answer: an id collision means the agent has
to rename and resubmit regardless, and tool-side validation already covers the
non-racing case.

The old run's node reasons already name the minted id by then, so the link record is
written either way -- with `started: false` and the error when the submission did not
happen. A record that says the run was replanned into an id that never ran is
recoverable; a reason pointing at an id with nothing anywhere to explain it is not.

## Model-facing text

- `_exception_report`'s invocation paragraph gains the third option, with the shape of a
  `replan` call, and states that a completed node is referenced rather than re-declared.
- `subagent-dag-orchestration/SKILL.md` gains the third decision and the criterion for
  choosing it: **when you can supply what the report says is missing, `continue`; when
  what is missing is the plan itself, `replan`.** Without a stated criterion `replan`
  becomes a general-purpose escape from any failing node.
- `CONTEXT.md` gains a `replan` entry, and its `exception` entry loses the claim that a
  suspended node is "waiting for the main agent to decide whether to continue or abandon
  it" -- there are three choices now.

## Tests

Per AGENTS.md 5.1 these land in the existing files; none is created.

| File | Covers |
|---|---|
| `tests/test_subagent_dag_adjudication.py` | the desk carrying a `ReplanPlan`; the replan event firing on `resolve` |
| `tests/test_subagent_dag_control_tools.py` | the tool's parameter validation and each refusal path -- missing `nodes`, missing `message`, re-declared id, reference to a non-completed node, paused delegation, spent quota, declined confirmation; the link record written with `started: false` when the submission is refused |
| `tests/test_subagent_dag_runner.py` | the interrupted round; the wind-down status and reason mapping; `replanned_into` suppressing the announcement |
| `tests/test_subagent_dag_core.py` | the chained reference -- a new run reading a wound-down run's completed node by id |
| `ui-tui` `dagRun.ts` suite | folding `dag_run_replanned` onto the old run's state |

## Deliberately out of scope

- **Re-running a completed node.** Its dependents may have completed against the output
  it is about to replace, and there is no honest answer for them short of a cascade
  rule. Redoing the work is expressible as a new node that references the old output.
- **Any replan cap.** Ruled out in favour of the hourly dispatch quota; see above for
  what that does and does not bound.
- **Carrying an attempt budget across a replan.** Requires node identity to span ids
  that must differ.
- **A test for the bound lane's silence.** The behaviour is now an explicit check rather
  than an eviction accident (see above), but the suite still covers the backgrounded
  path only; reaching the bound one needs a released tray and a foreground turn to
  release it.
- **Drawing the successor under the tool row that spawned it.** The web UI resolves a
  DAG card through a live index keyed by the tool call id, and drops that entry when the
  run completes. The successor is submitted with the same call id -- the contextvar is
  set for `run_subagent_dag`, and `resolve_dag_node` never overwrites it -- so its events
  arrive after the drop and fall back to the unattached lane. The trail therefore ends at
  the run that was replaced. The link event still lands, so the replan itself is visible;
  giving the successor a card of its own is a new capability, not a repair.
