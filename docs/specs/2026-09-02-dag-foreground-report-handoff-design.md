# DAG foreground runs hand their exception reports to the main agent - design

Date: 2026-09-02. Branch base: `refactor/raven_v0_2_0`.

Supersedes the paragraphs of `docs/specs/2026-08-26-dag-node-verdict-design.md`,
section 3, that begin "A foreground graph is adjudicated inside its own turn" and end
"A host with no ask broker gets the plain failure path". Everything else in that spec
stands.

## Problem

A `run_subagent_dag` call with `background: false` blocks the main agent's turn until
the graph finishes. When a node of such a graph fails its verdict, the runner suspends
it exactly as it would in a backgrounded run, but the exception report never reaches
the main agent: its own tool call is still on the stack, the conversation lane is
serial, and the turn that would call `resolve_dag_node` cannot start until this one
ends. The 2026-08-26 design worked around this by asking the *person* watching the
call instead (`_adjudicate_open_nodes`, `wiring._adjudicate_node`,
`AskUserTool.ask_direct`).

That workaround inverts the intended control relationship. The report's own closing
line tells the reader to "ask the user first if only they can supply what is missing":
deciding whether a human is needed is the main agent's job. The foreground lane skips
the agent not because a person decides better, but because the agent could not be
reached. It also left the foreground lane with its own fixed round deadline (the
serial-questions shape), which the 2026-09-02 progress-deadline fix to
`_await_adjudications` does not cover.

## Decisions taken

These were settled in conversation before this document was written.

1. **The report becomes the tool call's return value.** A foreground call returns as
   soon as a node suspends, carrying that node's report. The graph keeps running.
2. **`resolve_dag_node` in a foreground run decides and then keeps waiting.** It returns
   the next report, or the run's final result. In a backgrounded run it returns at once,
   as today. One tool, two shapes, keyed on the run rather than on an argument.
3. **First suspension returns, not first idle.** The main agent learns the moment a
   node falls short, while other branches are still running, so it can go and ask the
   user before the graph has anything left to do.
4. **A foreground run does not time out while the turn that owns it is alive.** The
   adjudication window starts only when that turn ends. A backgrounded run is clocked
   from the start, as today.
5. **One report per take.** When several nodes have suspended, each `resolve_dag_node`
   returns the next buffered report immediately; the agent is never handed a batch.
6. **Cancelling the tool call that is awaiting a foreground run cancels the run.** The
   user stopping the agent while it is blocked on a graph stops the graph, which is
   what a foreground call means today.
7. **The person-asking path is removed**, not kept as a fallback. Its only reason to
   exist was the agent being unreachable.

## Terminology

Added to `CONTEXT.md` (Runtime terms) in the same change:

- **outbox** -- a foreground run's tray of events (its nodes' exception reports and its
  final result), waiting for the tool call that will take them. One per foreground run,
  in memory beside the run's adjudication desk. The desk carries decisions from the main
  agent to the run; the outbox carries reports from the run to the main agent.
  _Avoid_: "mailbox" -- the lane's inject mailbox is a different object with a different
  reader.
- **bound / released** (foreground run) -- a foreground run is *bound* while the turn
  that started it is still running, and *released* once that turn has ended, however it
  ended. A bound run's suspended nodes wait without a deadline and its outbox buffers;
  a released run behaves as a backgrounded one: its window is clocked and its events
  announce as new turns.

`exception` (node status), `verdict`, `adjudication` keep their existing definitions.
With the person path gone, the `exception` definition's "waiting for the main agent to
decide" is now true in both lanes.

## 1. Control flow

Both lanes now run the graph as a task. The difference is where its events go.

```
run_subagent_dag(background=false)
  validate, charge, mint run_id, register cancel event      (unchanged)
  outbox = Outbox(...)                                       (new)
  task = create_task(_run_detached(..., outbox))             (same task shape as background)
  adopt(task)                                                (both lanes now)
  event = await outbox.take()
    Report(node_id, text) -> return text + protocol tail
    Final(result)         -> return result (today's summary; unchanged when no node suspended)
    Stopped               -> return "DAG run <id> was stopped before it finished."

resolve_dag_node(run_id, node_id, decision, message)
  ownership check, desk.resolve(...)                         (unchanged)
  if the run is foreground and bound:
      event = await outbox.take(); render as above
  else:
      return today's text at once
```

The runner is told nothing about lanes. Its `announce_exception` callback is the only
thing that differs: the background lane passes `SubagentManager.announce_dag_exception`
(inject a turn), the foreground lane passes a closure over `outbox.put_report`.

Because the report is delivered at suspension time and the runner only waits once the
ready set is empty, a suspended node's report is always in the agent's hands (or in the
outbox) before the runner starts waiting on it. The desk is opened before the announce,
as today, so a decision that arrives before the wait begins is parked and taken when the
wait starts.

## 2. The outbox

`raven/agent/subagent/dag_adjudication.py` gains:

```python
@dataclass(frozen=True)
class Report:  node_id: str; text: str
@dataclass(frozen=True)
class Final:   result: str | ToolResult; stopped: bool     # stopped = the run's cancel event was set
@dataclass(frozen=True)
class Stopped: pass

class Outbox:
    released: asyncio.Event          # set by release(); read by the runner's wait
    async def take(self) -> Report | Final | Stopped
    async def put_report(self, node_id: str, text: str) -> None
    async def put_final(self, result, *, stopped: bool) -> None
    async def release(self, *, flush: bool) -> None
    def stop(self) -> None
    @property
    def bound(self) -> bool          # not released and not stopped
```

Constructor arguments: `announce_report(node_id, text)` and `announce_final(text)`,
async callables the tool has already closed over the run id and origin. They are the
released-mode routes and are the manager's `announce_dag_exception` and
`announce_dag_result` respectively.

Rules:

- **`take()` registers its taker synchronously, before its first await.** This is the
  correctness hinge. `resolve_dag_node` calls `desk.resolve()` (which sets the node's
  event and thereby *schedules* the runner's wake-up) and then `take()` in the same
  tick; because the taker is registered before the loop yields, the runner cannot
  produce the next event into an outbox with nobody waiting. Takers form a FIFO; the
  normal case is one.
- **Bound: hand or buffer, never announce.** While the turn is alive there is no safe
  moment to inject a turn: the agent is between tool calls, and an injected report would
  either duplicate what the next `take()` returns or queue a turn behind the one that is
  about to wait on it. So a `put_report`/`put_final` with a taker waiting hands the event
  over; with none, it appends to the buffer.
- **A handed report is re-sent to the next taker until it is decided.** `answered()`
  retires it; until then the handed tray records the debt. `take()`'s order is the
  queue, then a landed final, then that re-send, then park. Without the re-send a
  decision on some *other* node parks the call with nothing able to wake it: the runner
  waits for every open node and only begins waiting once nothing else can run, so while
  a handed node is undecided no report can be produced and no final can arrive, and a
  bound run has no deadline. The agent reaching for the other node is ordinary -- the
  report it holds names what is blocked behind it and `dag_status` lists the rest.
- **`release(flush=True)`** (the owning turn ended normally, or on an error): set
  `released`; announce every buffered report through `announce_report`, in order; if a
  `Final` is buffered, announce it through `announce_final` unless it is `stopped`.
  From now on `put_report` announces directly and `put_final` announces unless
  `stopped`. This is the point at which a released foreground run becomes a
  backgrounded one, with full information: the agent is re-sent the very reports it did
  not answer, as separate turns, and can answer them with `resolve_dag_node` in its
  ordinary background shape.
- **`release(flush=False)`** (the owning turn was cancelled): set `released`, drop the
  buffer, announce nothing. The user just stopped the agent; re-raising the run's
  open questions at them is noise. Later events still announce (a Final of a run that
  survived the turn cancel reaches the agent). This branch performs no awaits, so it is
  safe to run while a `CancelledError` is propagating.
- **`stop()`** (the run task is being hard-cancelled): mark stopped, drop the buffer,
  wake every taker with `Stopped`. Idempotent; a later `release()` on a stopped outbox
  is a no-op, which is what keeps a `/stop` from announcing stale reports for a run it
  is in the middle of killing.
- **A `Final` is terminal and sticky.** Every pending taker receives it, and any later
  `take()` returns it again at once. This closes the race where the run finishes between
  `resolve_node` returning True and `take()` being entered.

## 3. Binding to the turn

The signal that a turn has ended has one choke point: `AgentLoop.run_turn`
(`raven/agent/loop/main.py`). All three spine runner adapters call it
(`raven/agent/spine_runner.py:31`, `raven/gateway/spine.py:107,109`,
`raven/rpc/spine.py:212,217`), it is where the turn's task begins, and a
`try/except/finally` around its inner call sees every outcome: return, exception, and
cancellation. This is more reliable than the `after_send` hook (success path only, and
skipped for SUBAGENT and SENTINEL origins) and cheaper than a lane-level `TurnEnded`
subscription (four sink constructions to wire).

```python
flush = True
try:
    with use_binding(...), self.tools.session_scope_for(session_key):
        return await self._run_turn(...)
except asyncio.CancelledError:
    flush = False
    raise
finally:
    if req.direct_target is None:
        await self._release_dag_runs(session_key, flush=flush)
```

`_release_dag_runs` finds the registered `run_subagent_dag` tool (if any) and calls
`release_turn(session_key, flush=...)` on it, which releases every bound outbox whose
run origin has that conversation.

Keying by conversation is sound because a conversation's lane runs one main-agent turn
at a time, so "the bound foreground runs of conversation X" are exactly the ones this
turn started. `direct_target` turns are skipped: they run on an instance's own lane,
concurrently with the main agent's turn, and can never own a graph (the DAG tool is
main-agent only), so releasing on their end would wrongly release the main turn's runs.
`session_key` here is `req.conversation or f"{channel}:{chat_id}"`, the same formula
`_set_tool_context` gives the DAG tool each turn (`wiring.py:1136`), so the two sides
agree by construction.

A turn that started only backgrounded runs releases nothing: those have no outbox.

## 4. The runner

`raven/agent/subagent/dag_runner.py`:

- **Removed:** the `adjudicate` parameter of `run_dag`; `_adjudicate_open_nodes`; the
  `answered_in_turn` parameter of `_run_node`, `_run_group`, `_apply_verdict` and
  `_exception_report`, with the branches it selected. The wave loop's idle step is
  `_await_adjudications`, unconditionally. `_apply_verdict` announces whenever
  `announce_exception` and `origin` are wired; `deliverable` reduces to the former.
- **One report shape.** `_exception_report` keeps the backgrounded text, including
  "deciding within Ns, restarted by each decision" and the `resolve_dag_node` line.
  That text is what a released run announces, and at that point it is exactly right.
  The foreground-specific framing is appended by the tool, not here (section 6).
- **`_await_adjudications` gains `released: asyncio.Event | None`.** `None` (the
  background lane) means the deadline runs from entry, as today. An event that is
  already set means the same. An event that is not yet set means *no deadline*: the
  wait is on the node events, the cancel event and `released.wait()`; when `released`
  fires, the deadline is set to `now + timeout_s` and the wait continues under it. The
  progress restart from the 2026-09-02 fix is kept: a decision landing under a live
  deadline restarts it; a decision landing while unclocked leaves it unclocked.
- `run_dag` threads `released` through to `_await_adjudications`. Nothing else in the
  runner learns about lanes.

Docstrings that describe the person path (`_apply_verdict`'s "the foreground lane does
not ask here", the desk's "the foreground lane asks a person from the wave loop") are
rewritten to describe the outbox.

`ExceptionAnnouncer` becomes a `Protocol`, because the fact the two announcers disagree
about travels as a keyword: `awaiting_decision`, required, no default. A terminal report
is a notification rather than a question, and the foreground lane's route back is a tool
result -- so a question it cannot answer must not travel it, while the background lane
announces both kinds. Requiring it is the point: both announcers route on it, the wrong
value is invisible at the call site, and the lane that ignores it is not a reason to let
the next caller guess.

## 5. The tool and the control tool

`raven/agent/subagent/dag_tool.py`:

- `__init__` loses `adjudicate`. Gains `self._outboxes: dict[str, Outbox]`, retired by
  the same `_retire` done-callback that retires `_cancels` and `_desks`.
- `_execute`: both lanes create the task and adopt it. The foreground lane builds the
  outbox first, passes a closure over `outbox.put_report` as the runner's
  `announce_exception` -- forwarding the verdict rather than acting on it, since
  whether a notification is dropped depends on the lane and the outbox is what knows
  the lane -- then
  `await outbox.take()` and renders. `_with_notices` wraps the result as today.
- `_run_and_announce` becomes `_run_detached(..., outbox: Outbox | None)`: run `_run`;
  on `CancelledError` call `outbox.stop()` (if any) and re-raise; on any other exception
  `_run` itself raises, log it with its traceback and take
  `f"Error running DAG {run_id}: {exc}"` as the run's result -- the same shape `_run`
  already gives a `run_dag` failure, and the only way the awaiting call can learn of it,
  since the outbox is its one route back and an exception that dies with the task would
  leave `take()` waiting forever; then, with an outbox,
  `await outbox.put_final(result, stopped=cancel.is_set())`; without one, the existing
  background announce (silent when `cancel.is_set()`). A `background: false` call thus
  returns an error text where it used to raise; `execute`'s `run_mcp_scope` still drops
  the run's MCP scope on that path.
- After `outbox.take()` returns, `_execute` retires the run's bookkeeping itself when the
  task is already done: the task's done-callback runs one loop iteration later than the
  taker's wake-up, so without this the call returned with the finished run still listed
  in `_runs`. The pops are idempotent with the callback's own.
- New methods for the control tool: `is_foreground(run_id) -> bool` (an outbox exists
  and is bound), `await_run(run_id) -> event | None` (None when not foreground-bound),
  `abort_run(run_id) -> None` (`outbox.stop()` then `task.cancel()`), and
  `release_turn(conversation, *, flush)` for section 3.
- `_render(event)`: `Report` -> the runner's text plus the protocol tail; `Final` ->
  its result unchanged; `Stopped` -> one line naming the run.
- While awaiting `outbox.take()` in `_execute`, a `CancelledError` calls
  `abort_run(run_id)` before re-raising (decision 6).

`raven/agent/subagent/dag_control_tools.py`, `ResolveDagNodeTool`:

- `blocking_for(params)` returns `tool.is_foreground(params.get("run_id"))`, and
  False when no graph tool is registered. The registry then skips its timeout ceiling
  only for the calls that will actually block, and the turn stream's `blocking` flag
  stays honest for background resolves.
- `execute`: after `resolve_node(...)` returns True, `event = await tool.await_run(run_id)`;
  `None` -> today's text; otherwise render exactly as the graph tool does (shared
  helper). A `CancelledError` during that await calls `tool.abort_run(run_id)` and
  re-raises.
- When `resolve_node` returns False the tool never awaits: the run is over or the node
  is not waiting, and today's "no longer waiting" text is returned.

`raven/agent/loop/wiring.py`: the two `SubAgentDagTool(...)` constructions drop
`adjudicate=`; `_adjudicate_node` is deleted. `_confirm_graph` and its `ask_direct` use
are untouched. `SubagentManager.adopt_background_run`'s docstring is updated to say
both lanes adopt; the name stays.

## 6. Model-facing text

**Report tail** (appended by the tool to a `Report` returned as a tool result; never
part of an announced report):

> This call returned before the graph finished. The graph is still running and this
> node is waiting for your decision. Decide with resolve_dag_node, which returns the
> next such report or the run's final result. The deadline above is paused while this
> turn runs; if you end the turn without deciding, the report is re-sent to you as a
> message and the deadline starts.

**`background` parameter description** (`dag_tool.py`, `parameters`):

> Default true: return as soon as the run starts and get the result as an announcement
> when the graph finishes, leaving you free to work meanwhile. Set false only when you
> cannot continue without the outputs: the call then blocks until the graph finishes or
> a node reports it could not accomplish its task, whichever comes first, and answering
> that report with resolve_dag_node resumes the wait.

**Guide skill** (`raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md`): the
"Pass `background: false` only when..." paragraph and the "All of this is about a
backgrounded run..." paragraph are rewritten to the new contract: a foreground call may
return a node report instead of the summary; answer it with `resolve_dag_node`, whose
return is the next report or the final result; ending the turn with a report unanswered
hands the run to the background path (the report is re-sent, the deadline starts).

## 7. Cancellation and stop

| Event | Bound foreground run | Released (or background) run |
|---|---|---|
| `turn.cancel` (TUI/web) while a tool call awaits the outbox | tool aborts the run: outbox stopped, task cancelled, nodes killed, graph closed `stopped` | n/a (nothing awaits) |
| `turn.cancel` while the agent is between calls | turn's `finally` releases with `flush=False`; run continues as background; buffered reports dropped; later events announce | unchanged |
| gateway `/stop` (lane cancel + `cancel_by_session`) | as above, and the adopted task is cancelled by `cancel_by_session`; `stop()` before `release()` means nothing stale is announced regardless of order | task cancelled; silent |
| `cancel_dag` (soft, `cancel.set()`) | `run_dag` returns with cancelled nodes; `Final(stopped=True)` reaches the awaiting call as today's summary | silent, as today |
| shutdown sweep | task cancelled; takers woken with `Stopped` | task cancelled |
| gateway restart | everything in memory is gone; nodes read back `interrupted` | unchanged |

## 8. Failure modes

- **The agent ends its turn with a report unanswered (release).** `release(flush=True)`
  re-sends every buffered report as its own turn and starts the window. The agent
  answers with `resolve_dag_node` in background shape. If it never does, the nodes fail
  after the window and the summary announces. No hang, no leak, no silent loss.
- **The run finishes while the agent is between calls.** The `Final` is buffered; the
  next `take()` returns it, or the release flushes it. The tool call that started the
  run never sees it twice: `Final` is sticky but each taker takes once.
- **A node exhausts its continuations.** It is marked failed and does not suspend, so no
  report returns the foreground call; the agent learns of it from the run's summary, and
  the background lane still announces the notification.
- **The agent calls `resolve_dag_node` for a run that has finished.** `resolve_node`
  returns False; the tool returns "no longer waiting" and does not await.
- **The agent starts a second foreground graph before answering the first.** Both are
  bound to the turn; each `resolve_dag_node` awaits only its own run's outbox; the turn's
  end releases both.
- **The judge call itself fails.** Unchanged: fails open to `accomplished`.
- **A stuck turn.** A bound run waits as long as the turn does; the turn's own bounds
  (iteration cap, user stop) end both. This is the intended reading of decision 4.

## 9. Testing

Delete the person-path tests in `tests/test_subagent_dag_runner.py`, which sit
together between `test_a_foreground_run_asks_a_person_and_continues_the_node` and
`test_a_foreground_node_with_nobody_to_ask_says_so` (ten tests, including
`test_only_the_foreground_lane_is_handed_the_adjudicator`). In
`test_the_in_turn_report_drops_the_deadline_and_the_tool_instruction`, keep the
`to_agent` assertions (they pin the one remaining report shape) and drop the
`answered_in_turn` half.

Add, runner level (`tests/test_subagent_dag_runner.py`):

- `_await_adjudications` with an unset `released` event does not time out across a
  window; setting the event starts the window; a decision under the live window restarts
  it; `released=None` behaves exactly as today (the existing shared-deadline and
  progress-restart tests keep passing).
- `_apply_verdict` announces whenever the callback is wired, with one report shape.

Add, outbox unit (`tests/test_subagent_dag_adjudication.py`, new file):

- `take()` registers before yielding: a `put_*` in the same tick after `take()` is
  entered reaches that taker.
- Reports buffer in order while bound and are handed one per take.
- A handed, undecided report is re-sent to a taker that asks again; an unseen queued
  report goes ahead of that re-send.
- `release(flush=True)` announces buffered reports in order and a non-stopped Final;
  `release(flush=False)` announces nothing; `stop()` then `release()` announces nothing.
- `Final` is sticky: two takers both receive it; a later take receives it again.

Add, tool level (`tests/test_subagent_dag_runner.py`, which already hosts the graph
tool's lane tests and the ones being deleted):

- A foreground call returns the first report with the protocol tail while the graph is
  still running.
- `resolve_dag_node(continue)` returns the final summary once the graph finishes;
  `resolve_dag_node(abandon)` likewise.
- Two suspended nodes: the second resolve returns the buffered second report at once.
- `blocking_for` is True only for a bound foreground run.
- A background resolve returns immediately, as today.
- Cancelling the awaiting call cancels the run task and the graph closes `stopped`.
- `release_turn(flush=True)` announces the buffered report through the manager route;
  `release_turn(flush=False)` does not.

Add, loop level (the existing `AgentLoop` turn tests):

- `AgentLoop.run_turn` calls `release_turn` on normal end, error end and cancel, with
  the right `flush`, and not for a `direct_target` turn.

## 10. Verification outside tests

- A live foreground run through `raven web` or the TUI: the tool row completes when the
  report returns while the DAG panel keeps updating, then settles on the final result.
  The background lane already has this shape (`_close_graph`'s docstring), so no UI
  change is expected; this confirms it.
- `make lint-python`; `uv run pytest tests/test_subagent_dag_*.py tests/test_rpc_dag.py
  tests/test_agent_loop*.py`.

## Files

| File | Change |
|---|---|
| `raven/agent/subagent/dag_adjudication.py` | `Report`, `Final`, `Stopped`, `Outbox` (section 2); desk docstrings |
| `raven/agent/subagent/dag_runner.py` | remove the person path; `released` in `_await_adjudications` and `run_dag`; one report shape (section 4) |
| `raven/agent/subagent/dag_tool.py` | outboxes, `_run_detached`, `is_foreground` / `await_run` / `abort_run` / `release_turn`, rendering, `background` description (sections 5, 6) |
| `raven/agent/subagent/dag_control_tools.py` | `ResolveDagNodeTool.blocking_for` and the foreground await (section 5) |
| `raven/agent/loop/main.py` | `run_turn` try/except/finally and `_release_dag_runs` (section 3) |
| `raven/agent/loop/wiring.py` | drop `adjudicate=` at both constructions; delete `_adjudicate_node` |
| `raven/agent/subagent/manager.py` | `adopt_background_run` docstring: both lanes adopt |
| `raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md` | the two foreground paragraphs (section 6) |
| `CONTEXT.md` | `outbox`, `bound / released` (Terminology) |
| `docs/specs/2026-08-26-dag-node-verdict-design.md` | a one-paragraph note at the start of the superseded passage pointing here |
| `tests/test_subagent_dag_adjudication.py` | new: outbox unit tests |
| `tests/test_subagent_dag_runner.py` | delete the person-path tests; add the runner- and tool-level tests (section 9) |
| the `AgentLoop` turn tests | `run_turn` release behaviour (section 9) |

## Out of scope

- Batching several buffered reports into one tool result (decision 5 chose one per take).
- Announcing intermediate reports of a *bound* run (ruled out by the tool-loop gap).
- Renaming `adopt_background_run`.
- Persisting outboxes or desks across a gateway restart.
