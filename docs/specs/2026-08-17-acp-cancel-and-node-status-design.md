# Cancelled node status and ACP turn cancellation - design

Date: 2026-08-17
Status: designed
Base: `origin/main` at `5432d692`.

## Goal

Two things that are the same bug seen from two ends, delivered together:

1. A DAG node that was **stopped while running** is recorded as `cancelled`,
   distinct from `skipped`, which keeps its existing meaning of "never ran".
2. An **ACP-transport** node or spawn actually stops when it is cancelled. Today
   raven abandons the local wait and the remote agent runs the turn to
   completion. Raven will send `session/cancel` and wait, under a bounded
   budget, for the turn to settle.

The gateway also gains the ACP pool teardown it never had, because part 2's
shutdown behaviour is undefined without it.

## Why this is needed

### The status vocabulary lies about what happened

Both cancellation paths collapse every non-terminal node into `skipped`
(`raven/agent/subagent_dag/runner.py:245-248` for the cooperative stop,
`runner.py:323-327` for the hard one). `skipped` already means something
specific and different - it is what `_cascade_failures` (`runner.py:407-418`)
writes for a node whose upstream failed, i.e. a node that never ran.

The conflation is not cosmetic. `raven/rpc/methods/subagent.py:274-278` drops
every `skipped` node from the instance list, and states its reason:

> A skipped node never ran: it has no transcript, no cost and no clock - a row
> for it pads the list with entries that open onto nothing.

That reasoning is exactly false for a node that was stopped mid-flight. It has
a transcript prefix, it cost tokens, and it has a start time. Today those rows
are silently discarded, so the one node the operator just stopped is the one
node they cannot open.

### The in-memory status map never holds `running`

The two cancellation paths test `st in ("pending", "running")`, but nothing ever
writes `"running"` into that map: `status` moves from `pending` straight to
`completed` (`runner.py:568`) or `failed` (`runner.py:572`). The `"running"`
arm is dead code, and at cancel time a dispatched node and a node still queued
on the semaphore are indistinguishable.

So recording `cancelled` correctly is not a relabelling. It needs a real
discriminator first.

### An ACP node is never actually cancelled

`AcpClient.request` handles cancellation by removing its pending future
(`raven/agent/acp/client.py:253-262`). It sends nothing to the agent and kills
nothing. The connection is pooled and shared by every session of that agent
(`raven/agent/acp/pool.py:1-14`), so the CLI transport's answer - `killpg` the
process group (`raven/agent/subagent/backends/cli_agent.py:161-183`) - is not
available: it would abort every unrelated in-flight session on the same
process.

The result is that raven marks the node stopped, releases its concurrency slot,
and the agent keeps working. Its answer arrives on a request id nobody is
waiting for and `_resolve` discards it (`client.py:406-408`). Worse, for a
stateful agent the half-finished turn stays in the agent's own session, and a
later `session/load` on the same handle resumes into it.

The protocol has the mechanism. From the SDK typings shipped with the adapters
raven launches (`@agentclientprotocol/sdk`, `dist/acp.d.ts`), the agent-side
contract for `session/cancel` is:

> This is a notification sent by the client to cancel an ongoing prompt turn.
> Upon receiving this notification, the Agent SHOULD: stop all language model
> requests as soon as possible; abort all tool call invocations in progress;
> send any pending `session/update` notifications; respond to the original
> `session/prompt` request with `StopReason::Cancelled`.

It is a baseline method with no capability flag, so nothing has to be
negotiated at handshake. That the path works in practice is already recorded in
this repo: `raven/agent/acp/permissions.py:11-16` documents codex-acp settling
a turn with `stopReason: "cancelled"` within a turn when its approval handler
cancels.

### The gateway leaks ACP servers, and its teardown order defeats the fix

`close_pool()` has exactly two production callers, `raven/cli/tui_commands.py:820`
and `raven/rpc/bootstrap.py:196`. The gateway is neither: its `gw_teardown` is
`raven/cli/_gateway_spine.py:151` (scheduler plus hub) and its `web_teardown`
comes from `raven.web_rpc.spine`. Neither closes the pool. ACP servers are
launched with `start_new_session=True`, so - as `bootstrap.py:191-193` already
states - they outlive the process unless the pool is closed. Every gateway exit
orphans them.

On the paths that *do* close the pool, the order is wrong for this work:
teardown closes the pool before anything cancels the in-flight tasks, and
`AcpClient.close` fails every pending future with `AcpConnectionError`
(`client.py:193`). An in-flight ACP node at shutdown is therefore recorded
`failed`, not cancelled, and a settle would have nothing left to talk to.

## Decisions taken during design

| Question | Decision |
|---|---|
| Discriminator for `cancelled` | `status` gains a real `running` value, written where the `running` event is already published |
| `skipped` meaning | Unchanged: never ran. Cascade victims and nodes still queued at the stop |
| Relation to `interrupted` | Kept distinct. `cancelled` is recorded at runtime; `interrupted` is inferred by a reader for a node the registry calls running on a run nothing is executing (`raven/rpc/models.py:456-460`) |
| Instance-row filter | Only `skipped` is filtered out. A `cancelled` row stays and is openable |
| Reuse error text | A `cancelled` node gets its own branch, worded as "stopped mid-run". It does **not** mention remote session residue - that is one transport's detail and the message is shared by all |
| ACP cancel mechanism | `session/cancel` notification, then a bounded wait for the prompt to settle. Not process kill: the connection is shared |
| Cancel modes | Two. Normal: notify then wait. Draining: notify, do not wait |
| Why a module-level drain flag | Asyncio delivers cancellation as a bare `CancelledError` into the target task. A contextvar set by the canceller does not reach it, and no argument can ride along. Process-global state is the only mechanism that works |
| Settle budget | `5.0` seconds, a ceiling rather than an expectation. Replaced by a measured value in the final task |
| Budget configurability | None. It is a protocol-conformance bound, not a workload property |
| Settle timeout handling | The node is still recorded `cancelled`; the stateful binding is unbound so the next dispatch opens a fresh session; a warning names the agent |
| Vendored agentscope DAG | Out of scope. See below |
| MR shape | One MR |

### Why the vendored agentscope DAG is out of scope

The intent was to keep `ui-webui/service/agentscope/subagent/_dag/` in step. It
turns out there is nothing to keep in step: that tree contains no cancellation
at all (no occurrence of `cancel` anywhere under `_dag/`), and no ACP backend.
Its `_runner.py` writes `skipped` only from `_cascade_failures`, which is the
one meaning this change leaves untouched.

Adding cancellation to a DAG implementation that has none is a separate
feature, not a synchronisation of this one.

## Design

### Part 1 - `cancelled` as a first-class node status

**Discriminator.** `_run_node` sets `status[node.id] = "running"` at the point
it already publishes the `running` event and writes the registry row - inside
`async with semaphore`, so a node still waiting for a concurrency slot stays
`pending`. Everything that reads the map already filters on `st == "pending"`
(the ready-set computation and `_cascade_failures`), so a dispatched node drops
out of both, which is what those filters want.

**Assignment.** Both cancellation paths split the collapse they do today:

- `running` -> `cancelled` (it ran and was cut off)
- `pending` -> `skipped` (it never ran)

**Cascade.** `_cascade_failures` is deliberately left alone. Adding
`"cancelled"` to its tuple looks symmetric but is unreachable: `cancelled` is
only ever written by the two cancellation handlers, and each is immediately
followed by the run ending, so no scheduling round can observe a cancelled node
and still have pending work to cascade to. In the cooperative path the same
handler already moves every pending node to `skipped` in the same pass.

**Tally.** `_tally` gains a `cancelled` count, and the two readers that
recompute the same summary independently (`_reader.py:129-140`,
`_resume.py:69-74`) gain it too, so one run does not read three different ways
depending on the entry point.

**Wire.** `DagNodeStatus` and `DagSnapshotNodeStatus` (`raven/rpc/models.py:391`
and `:460`) both gain `"cancelled"`. These are strict models, so emitting the
new status without this is a validation failure, not a soft degradation.

**Reuse.** `_unreadable` (`raven/agent/subagent_dag/_graph.py:304-349`) gains a
`cancelled` branch. The existing `skipped` message says the run "skipped" the
node; for a cancelled one that is wrong about what happened and about what to
do next.

**Instance rows.** `_DAG_WIRE_STATUS` (`raven/rpc/methods/subagent.py:194-201`)
maps `cancelled` to `cancelled`, and the filter at `subagent.py:277` stays
keyed on `skipped` alone.

**Frontend.** The client already half-anticipates this: `InstanceRowStatus`
carries `'cancelled'` and both locales already translate
`subagent-monitor.nodeStatus.cancelled`. What is missing is the graph
vocabulary - `DagNodeStatus`, `DAG_STATUS_LABEL_KEY`, `STATUS_STYLE`, and
`toStatus`, which today coerces `cancelled` to `interrupted`. That coercion was
correct while nothing on the wire could send `cancelled`; it stops being
correct here, because the two now mean different things.

### Part 2 - ACP turn cancellation

**Client.** `AcpClient.request` gains a `cancel_session: str | None` parameter.
When the request is cancelled and the parameter is set, the client sends
`session/cancel` for that session, then - unless draining - waits up to
`_CANCEL_SETTLE_S` for the pending future to resolve. The `CancelledError` is
re-raised either way. Awaiting inside a cancel handler is sound here and is the
pattern `cli_agent._kill_process_group` already relies on; it holds because
every canceller in this codebase cancels once and then gathers.

**Settle failure.** A session whose cancel did not settle is recorded on the
client and readable once via `take_unsettled_cancel(session_id)`. The remote
turn is still running, and `session_lock` is released as soon as the caller
exits its `async with`, so a later prompt on that session would be a protocol
violation. The backend consumes the flag and unbinds the handle, which is the
same recovery `_open_session` already performs when a `session/load` fails
(`acp_agent.py:466-478`).

**Drain flag.** Module state in `client.py` - not `pool.py`, which imports the
client. `begin_drain()` puts the process in the mode where a cancel notifies
but does not wait; `close_pool()` clears it, which also gives the test suite's
existing autouse fixture (`tests/test_subagent_acp.py:44-53`) isolation for
free.

Draining still sends the notification. It costs one line on stdin, and an
adapter that persists session state gets to record the turn as cancelled rather
than have it truncated by the `SIGKILL` that follows.

**Shutdown order.** Every host follows the same sequence:

1. `begin_drain()`
2. `cancel_all()` - in-flight work stops, notifications go out, nothing waits
3. close the pool - `killpg` per connection

The gateway (`raven/cli/gateway_commands.py:695-720`) needs all three: it has
`cancel_all` in the wrong position and no pool close at all. `serve`
(`raven/cli/serve_commands.py:314-322`) closes the pool via `stack.teardown()`
but never cancels anything, so it also leaves detached CLI children behind; it
gains the first two steps. The TUI (`tui_commands.py:816-825`) already closes
the pool and needs the first two.

## Verification

- The stub server gains two modes: `cancel_aware`, which holds the prompt open
  until it receives `session/cancel` and only then answers with
  `stopReason: "cancelled"`, and `cancel_deaf`, which ignores the notification
  entirely. The first proves the notification is sent and awaited; the second
  proves the budget expires and the handle is unbound.
- The existing `cancelled` stub mode is left alone. It answers on its own
  initiative and therefore proves nothing about this path.
- The settle budget is measured against
  `@agentclientprotocol/claude-agent-acp@0.66.0` and
  `@agentclientprotocol/codex-acp@1.1.14` before the constant is final. `5.0` is
  a prior, not a measurement, and is marked as such until that task lands.

## Out of scope

- Cancellation for `ui-webui/service/agentscope/subagent/_dag/`, which has none.
- Any model-facing way to cancel a run. `request_cancel` stays reachable only
  from the stop RPC.
- The `openai_api` and `raven_loop` backends. The former abandons an HTTP
  request, which is all a client can do; the latter is in-process and takes the
  `CancelledError` directly.
