# Retire the `run_subagent_dag` foreground lane - design

Date: 2026-11-21. Status: proposed.

Supersedes `docs/specs/2026-09-02-dag-foreground-report-handoff-design.md`: the
foreground lane that document specifies is removed whole, not reworked. It also
supersedes the passages that describe that lane as current in
`docs/specs/2026-09-04-dag-replan-design.md` (the `blocking_for` note and
"Foreground runs"), `docs/specs/2026-08-26-dag-node-verdict-design.md` (the
foreground adjudication passage), and
`docs/specs/2026-09-01-dag-adjudication-route-design.md` (the failure-mode
table's foreground row).

## Problem

`run_subagent_dag` has two execution modes. The default dispatch mode returns a
receipt and announces the result as a later turn. `background: false` turns the
tool call into the run's reader: the call blocks, exception reports are handed
to it, and `resolve_dag_node` keeps waiting after recording a decision.

That second mode is a lifecycle, not a parameter:

- `raven/agent/subagent/dag_adjudication.py` carries `Outbox`, `Report`,
  `Final`, `Stopped`, and the announce/release plumbing (about 280 lines).
- `raven/agent/subagent/dag_tool.py` carries `is_foreground`, `await_run`,
  `abort_run`, `release_turn`, `render_event`, `retire_reports`, the
  `_outboxes` index, `_FOREGROUND_REPORT_TAIL`, and the outbox branches in
  `_dispatch`, `_run_detached`, and `_run`.
- `raven/agent/subagent/dag_control_tools.py` waits after a decision, a
  reassign, and a replan when the run is bound, and overrides
  `blocking_for` so the registry does not timer-kill that wait.
- `raven/agent/subagent/dag_runner.py` takes a `released` event and suspends
  the adjudication deadline until the owning turn ends.
- `raven/agent/loop/main.py` releases bound runs at the end of every turn.

No RPC method and no front-end passes `background`; the model learns the switch
from the tool schema and the `subagent-dag-orchestration` skill. The only
remaining production callers that need a result from the same process are:

1. `raven playbook run` (`raven/cli/playbook_commands.py:543`), whose contract
   is "run one playbook to completion, in this process, and print the result".
2. Task-mode workflow capture (`raven/agent/subagent/dag_tool.py:1819-1821`),
   which forces `background = False` so `_finish_playbook_turn` can read
   `capture.dags` when the turn ends.

The mode therefore buys nothing for ordinary model work and couples the DAG
tool to turn lifetime, cancellation, adjudication timing, and the tool
registry's timeout policy.

## Intent

One dispatch lane: every run is a task, every report and result travels by
announce, and `run_subagent_dag` returns a receipt. No model-reachable sync
mode and no foreground machinery. The CLI keeps its run-to-completion contract
through a private completion signal, and Task capture compiles when its DAG
finishes rather than blocking the turn.

## Goals

- Remove the `background` parameter from the tool schema and from `execute`.
- Delete the outbox, `Report`/`Final`/`Stopped`, bound/released, all foreground
  tool methods, the `resolve_dag_node` wait tail, and the runner's `released`
  deadline deferral.
- Keep `raven playbook run` synchronous from the user's point of view, with
  unchanged output, by waiting on a per-run completion future that is not part
  of the tool surface.
- Keep Task-mode capture working: compile and save the Workflow when the
  captured DAG finishes, whether that is before or after the turn ends.
- Update every model-visible or operator-visible description of the mode.
- Delete tests that specify the foreground lane; adapt tests that used it only
  as a way to await a run.

## Non-goals

- No change to graph parsing, validation, scheduling, concurrency, verdicts,
  replan, reassign, run storage, or announce text.
- No change to `spawn`.
- No new RPC or UI surface, and no change to the `dag.*` progress events.
- No change to Task generation, Persona generation, or the confirmation gate,
  except the announcement-origin guard in section 4: an announced result is not
  a request to create a playbook.
- No change to the run-record or playbook file formats.

## Anti-goals

- A `background`-like switch reachable through `tool_call` or a hidden keyword.
- Rebuilding the outbox under another name.
- Blocking a turn on a graph as a side effect of capture.
- Keeping a "foreground" concept in names, comments, tests, or docs.

## Constraints

- `tool_call` forwards arbitrary arguments to `execute`. The model-facing
  `execute` must not accept a wait/background keyword, and the private wait must
  be a different method.
- The completion wait must be per run. There is no per-session mode.
- A capture finalizer runs after its turn may have ended. The capture object
  already survives through the `capture` tuple held by the run task; no new
  global registry is needed.
- A node exception during a capture run must not deadlock. The turn is no longer
  waiting on the run, so the ordinary announce route can deliver the report and
  the model can answer it in a later turn.
- Repository rules apply: English source and comments, `uv run pytest`, the
  test file naming rules in `AGENTS.md`.

## Approaches considered

### A. One lane, completion future for hosts, capture finalizes on run end (chosen)

The tool never waits. `execute_to_completion` exists for non-model callers and
awaits a per-run future. Task capture records evidence at run end and invokes a
finalizer that compiles and saves the Workflow.

- Pros: one runtime path; the model cannot block a turn; deletes the whole
  foreground lifecycle; CLI and tests keep a clean await.
- Cons: Task-mode capture becomes asynchronous. The turn's reply can no longer
  summarize the graph's outputs, and the artifact is saved after the run ends.
  A process exit before the run finishes loses the deferred capture.

### B. One lane, capture still waits privately

Capture turns dispatch and then wait on the same completion future. To avoid a
deadlock when a node suspends, capture mode would set `control_reachable` to
false so nodes fail instead of waiting for an adjudication nobody can deliver
while the turn is blocked.

- Pros: Task mode keeps its in-turn result and compile.
- Cons: keeps a sync path the user explicitly rejected; capture turns lose
  adjudication; cancellation semantics remain special.

### C. One lane, no wait anywhere

The CLI dispatches and polls the run directory, rendering the result itself;
capture finalizes as in A.

- Pros: no in-process wait at all.
- Cons: a second renderer and polling loop in the CLI, with worse error
  semantics and latency. Not justified.

A is chosen because it removes the mode without removing the two legitimate
host needs. B keeps the thing being retired; C pays more code for less
correctness.

## Design

### 1. The one lane

```
run_subagent_dag(nodes, ...)
  validate -> preflight -> confirm -> charge -> mint run id
  create _run_detached task
  return receipt: "DAG run <id> started in the background (N nodes). ..."
```

- `SubAgentDagTool.parameters` drops `background`.
- `execute` drops the `background` keyword and the
  `if capture_workflow: background = False` branch.
- `_dispatch` no longer receives or branches on a mode. It always creates the
  task, registers it with `_runs` and the host adopter, and returns the
  existing receipt.
- `resolve_dag_node` records a decision and returns its text immediately. A
  report continues to arrive through `announce_dag_exception`, as it already
  does for every dispatched run.
- The runner's adjudication deadline is always clocked from the report. The
  `released` parameter and the "no deadline while bound" branch are deleted.

A stale caller may still send `background`; because `execute` has `**kwargs`,
the key lands there. It is ignored and the receipt gains one line saying the
switch has been retired and the run was started in the background. This is the
only compatibility gesture; it is model-visible only when a model sends the
old key.

### 2. Deleted machinery

`raven/agent/subagent/dag_adjudication.py`

- Delete `Report`, `Final`, `Stopped`, `Event`, `AnnounceReport`,
  `AnnounceFinal`, `_describe`, and `Outbox`. Keep `AdjudicationDesk` and the
  replan/reassign types. Update the module docstring and remove imports that
  only the outbox used.

`raven/agent/subagent/dag_tool.py`

- Delete `_FOREGROUND_REPORT_TAIL`, `_successful_final` (replace it with a
  result/success helper that does not depend on `Final`), `is_foreground`,
  `await_run`, `abort_run`, `release_turn`, `render_event`, `retire_reports`,
  and the `_outboxes` index. Remove `_outboxes` from `_rekey_run`.
- Remove the outbox argument and branches from `_dispatch`, `_run_detached`,
  and `_run`, including the `_to_outbox` closure and the `released` event.
- Keep `blocking_interaction = True`: the tool still asks the user for
  confirmation before dispatch, and that human wait must not be timer-killed.
  Update the comment so it no longer claims the tool waits on the graph.
- Rewrite the module docstring: one lane, announce-only reports.

`raven/agent/subagent/dag_control_tools.py`

- Remove the `blocking_for` override, so the registry default (not blocking)
  applies.
- Remove the `await_run` / `render_event` / `abort_run` tails from the continue,
  abandon, reassign, and replan paths, and remove the `was_bound` branch.
- Remove `retire_reports` calls.
- Update `ResolveDagNodeTool`'s class docstring and `description`: it answers a
  suspended node and returns; it never waits.

`raven/agent/subagent/dag_runner.py`

- Remove `released` from `run_dag` and `_await_adjudications`, the `unbind`
  task, the `deadline = None` branch, and the pass-through arguments.
- Update the comments that describe a bound run.

`raven/agent/loop/main.py`

- Delete `_release_dag_runs` and its call in the turn's `finally`.
- Capture creation attaches the run-finished callback (see section 4) and the
  failure path respects a deferred capture.

`Other text`

- `raven/agent/subagent/dag_live.py`: the `owning_tool` docstring no longer
  names `is_foreground`, `retire_reports`, or `await_run`.
- `raven/agent/subagent/manager.py`: the two comments comparing the announce
  route with a foreground route.
- `raven/rpc/spine.py`: the stall-frame comment.
- `raven/agent/tools/tool_search.py`: the `target_is_blocking` docstring no
  longer uses `resolve_dag_node` as its example.
- `raven/agent/loop/wiring.py`: the plugin-gate comment that mentions a
  foreground graph.
- `CONTEXT.md`: delete the `outbox` and `bound / released` terms.
- `raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md`: remove the
  `background` parameter from the call shape, the `background: false` advice,
  the "in a background: false call" sentence, and the "blocking call's return
  value" wording. Reports always arrive as messages; results always arrive as
  announcements.

### 3. Private completion wait for non-model callers

Add `SubAgentDagTool.execute_to_completion(...)`, with the same arguments and
validation as `execute` except that it also waits for the run's final rendered
result. It is not in `parameters`, and `execute` never forwards a wait flag, so
a `tool_call` cannot reach it.

- `_dispatch` creates an `asyncio.Future` only when the private wait is asked
  for, passes it to `_run_detached`, and returns whether a run actually
  started.
- `_run_detached` resolves the future on every exit:
  - the rendered result after a normal finish, a stop that produces a result,
    or a stint's final result;
  - the error text that `_run_detached` already produces for an unexpected
    exception;
  - `CancelledError` when the run task itself is cancelled.
- A validation error, a declined confirmation, a budget refusal, or an
  adoption refusal returns immediately; no future is created and nothing
  waits.
- Cancelling the waiter does not cancel the run. The wait is an observer, and
  a dispatched run survives its caller exactly as it does for `execute`. A
  host that wants to stop the run calls `request_cancel(run_id)` or
  `cancel_dag_run`; `raven playbook run` needs no special case because Ctrl-C
  ends the process and the in-process task with it.
- The run still announces when an announcer is wired. The CLI has no
  announcer, so the future is its only consumer. In tests, both may observe
  the result.

`raven/playbook/executor.py`

- Rename `background` to `wait_for_result` (the old name now describes a mode
  that does not exist).
- `_dispatch_playbook` calls `execute_to_completion` when the flag is true and
  `execute` otherwise.
- `raven/cli/playbook_commands.py` passes `wait_for_result=True`. The command's
  output and its "run to completion" contract are unchanged.
- The conversation path keeps the default false.

### 4. Task-mode capture

The capture turn no longer waits. It dispatches, returns the receipt, and the
workflow is compiled when the captured run finishes.

`PlaybookRunCapture` gains four fields, all excluded from `as_dict` except the
ones already serialized:

- `active_run_id: str | None` -- the run that owns this capture. Set after a
  captured dispatch succeeds; the last dispatch in a turn wins.
- `deferred: bool` -- the turn ended while `active_run_id` was still in flight.
- `finalized: bool` -- the capture has been compiled (or deliberately skipped)
  and its run record saved; prevents double finalization.
- `on_finished: Callable[[str], Awaitable[None]] | None` -- the run-finished
  callback, `repr=False` and `compare=False`.

Flow:

1. `execute` sees `capture_workflow`, dispatches normally, and sets
   `active_run_id` to the run id after adoption succeeds.
2. `_run_detached` schedules `on_finished(submitted_run_id)` after `_run`
   returns; it does not await it. The finalizer is a detached task, so the
   run is retired and stops reading as live before the compile's model call
   starts. The tool holds the scheduled task in a small set and discards it
   when done, which is also the task's strong reference.
3. The wiring callback (`_capture_run_finished`) ignores the call when the id
   is not the active one, clears `active_run_id`, and calls `_finalize_capture`
   when `deferred` is true.
4. At turn end, `_finish_playbook_turn` checks `active_run_id`. If it is set,
   it sets `deferred = True` and returns without finishing or saving. Otherwise
   it finalizes as today.
5. `_finalize_capture` is the current body of `_finish_playbook_turn`: compile
   when `capture.dags` is non-empty and no stored playbook was selected, set
   `saved_playbook`, `finish()`, save the run record. It returns immediately if
   `finalized` is set, and sets it before doing the work.
6. A failed capture run has no evidence; finalizing it saves the record with no
   Workflow, which is today's outcome for a capture run that did not succeed.
   A capture run later stopped by `cancel_dag` records the same way: the run
   result is the stopped tally, no evidence is recorded, and no Workflow is
   compiled.
7. `deferred` is set only on the success path, inside `_finish_playbook_turn`.
   A turn that fails or is cancelled with a capture run still in flight never
   reaches that method, and the turn's existing failure path finalizes the
   capture as failed/cancelled and marks it finalized. The run's later
   completion then finds `finalized` set and neither compiles nor saves. The
   run itself continues as an ordinary dispatched run and announces its
   result.
8. If several captured runs are dispatched in one turn, only the last one owns
   `active_run_id`; earlier completions do not finalize. The last completion
   uses `capture.dags[-1]`, the same rule the synchronous turn used.

A hard process exit before a deferred run finishes loses that capture. This is
the same guarantee the blocking turn had: a killed process saved nothing, and
the run record of a killed turn was already best-effort.

#### Announcement turns must not generate a Harness

An announced result is delivered as a `TurnRequest` with
`Origin.SUBAGENT` (`raven/agent/subagent/manager.py:2703-2716`), and
`_resolve_playbook_turn` (`raven/agent/loop/wiring.py:997-1067`) currently
guards only `is_subagent_process()` and `direct_target`. With
`agentHarness: generate` and the default generation mode `task`, an
announcement turn would run `TaskPlaybookGenerator` on the run's own summary,
save a Task artifact, and write a run record with no DAG evidence.

That interaction predates this change for any background run, but every Task
capture run now takes it. `_resolve_playbook_turn` must return an empty
`HarnessResolution` for `Origin.SUBAGENT`: an announcement is a result to read,
not a request to create a playbook. No other generation path changes.

### 5. Behavior changes

- The model no longer sees a `background` parameter and cannot make a DAG call
  block a turn.
- A node exception is always an announced report answered in a later turn. The
  blocking report-as-return-value shape disappears.
- `resolve_dag_node` never waits; its result is the decision acknowledgement
  (or the replan/reassign text).
- Task-mode capture turns return a receipt instead of the graph's result. The
  graph's result arrives as its announcement; the Workflow is saved after the
  run finishes.
- `raven playbook run` is unchanged from the user's point of view.
- Runs keep the existing default adjudication timeout (600s) from the moment
  the report is produced.

### 6. Tests

Delete:

- All `Outbox` tests in `tests/test_subagent_dag_adjudication.py`; keep the
  desk tests.
- The foreground report-handoff section in `tests/test_subagent_dag_runner.py`,
  the release-at-turn-end section, the `Stopped`-event tests, and the
  `released`-deadline tests.
- `tests/test_subagent_dag_control_tools.py` tests for the wait tails,
  `blocking_for`, and the foreground resolve/reassign/replan paths.
- The release tests in `tests/test_agent_loop_run_emit.py`.

Adapt:

- The 73 `background=False` sites in tests become
  `execute_to_completion(...)` where the test is only waiting for a result
  (`test_subagent_dag_runner.py`, `test_subagent_dag_mcp_scope.py`,
  `test_subagent_dag_core.py`, `test_playbook_stint.py`,
  `test_agent_playbook_delegate.py`, `test_subagent_dag_control_tools.py`).
- Tests that inspect `_desks` immediately after dispatch must read the run id
  from the receipt or `active_run_ids()` and wait until the desk opens; a
  background task no longer starts before `execute` returns.
- `tests/test_cli_playbook_commands.py`: assert `_wait_for_result is True`.
- `tests/test_security_untrusted_context.py`: drop `"background": False` from
  the DAG call payload.
- `tests/test_unified_playbook.py`: `_successful_final` uses the new shape.
- `tests/test_subagent_dag_core.py`: rewrite
  `test_the_dag_description_says_background_is_the_default` to assert the
  single-lane wording; update the re-key test that lists `_outboxes`;
  remove `background=True` arguments.
- `tests/test_rpc_dag.py`: the comment about a bound foreground run.

Add:

- The schema has no `background`, and the description no longer says
  "By default".
- `execute` returns a receipt and `resolve_dag_node` returns immediately.
- A legacy `background` key is ignored and adds the retirement notice.
- `execute_to_completion` returns the final result; a refused graph returns
  without waiting.
- Capture: a turn ends with the run in flight; when the run completes, the
  Workflow is compiled and the run record saved. A failed run saves no
  Workflow. A cancelled turn does not compile later. A turn cancelled while a
  capture run is in flight finalizes the capture and a later run completion
  does nothing.
- The capture finalizer is not awaited by the run task: the run stops
  `active_run_ids()` reporting it before the compile begins.
- `_resolve_playbook_turn` returns an empty resolution for `Origin.SUBAGENT`,
  so an announced result does not generate a Task artifact.
- The CLI executor selects `execute_to_completion`.

### 7. Risks and rollback

- The Task-mode experience changes: the reply is a receipt and the summary
  arrives as a separate turn. This is the largest user-visible change.
- A model that learned `background: false` from an old skill gets a receipt
  that explains the switch is gone; it must read the result in the announced
  turn, as every other run works.
- No persistent format changes: run records, playbooks, and the `dag.*`
  contract keep their shapes.
- Rollback is a single revert. The foreground lane's code is deleted, not
  flagged off, so a rollback must restore it from history.

### 8. Acceptance criteria

- `run_subagent_dag`'s schema contains no `background`, and its description
  states that the graph runs in the background with no qualification.
- `rg "Outbox|is_foreground|await_run|abort_run|release_turn|retire_reports|_outboxes|_FOREGROUND_REPORT_TAIL|released"` finds no production references to the foreground lane.
- `uv run pytest` passes for the targeted DAG, control-tool, playbook, CLI, and
  loop suites; the full suite passes before the PR.
- `raven playbook run` prints the final graph result, not a receipt.
- A Task-mode end-to-end test shows the Workflow saved after the run finishes
  and the run record written once.
- `make check-source-language` and `make check-large-files` pass.

## Decisions taken with the user

- 2026-11-21: the whole foreground lane is deleted; no synchronous entry
  point remains.
- 2026-11-21: approach A is chosen over B (hidden capture wait) and C (no wait
  anywhere).

## Calls made by the builder

These settle the questions a builder check raised; each is a decision, not a
requirement the user stated.

- A turn that fails or is cancelled with a capture run in flight finalizes the
  capture as failed/cancelled and marks it finalized. `deferred` exists only on
  the success path, so there is no "cancelled while deferred" state.
- A capture run stopped later by `cancel_dag` finalizes with no Workflow and
  records `completed`, matching today's non-compiling outcome for a stopped
  run.
- Cancelling an `execute_to_completion` waiter does not cancel the run; the
  run is an ordinary dispatched task. A host that wants it stopped cancels it
  by run id.
- The capture finalizer runs as a detached task, held by a set on the DAG
  tool, so the run's liveness ends before the compiler's model call and a
  shutdown does not wait on a compile.
- `_resolve_playbook_turn` gains an `Origin.SUBAGENT` guard. This is a small
  behavior fix outside the original non-goal, required because every capture
  result is now announced.

## Left to the builder

- The final name of the private wait (`execute_to_completion` is a proposal).
- Whether the capture finalizer lives on the capture object, in the wiring, or
  in a small protocol; the requirement is that it is per capture and survives
  the turn.
- The test helper shape used to migrate the convenience `background=False`
  sites.
- Whether the tool keeps `timeout_seconds = 1800.0`; `blocking_interaction`
  must stay because the confirmation gate waits on a person.
- The exact wording of the retirement notice for a stale `background` key.
