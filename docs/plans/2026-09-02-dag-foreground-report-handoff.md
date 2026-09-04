# DAG Foreground Report Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `run_subagent_dag` call with `background: false` returns the moment a node fails its verdict, carrying that node's report; the main agent answers with `resolve_dag_node`, which then returns the next report or the run's final result; the graph never times out while the turn that owns it is alive.

**Architecture:** Both lanes run the graph as a task. A foreground run gets an `Outbox`, a per-run tray the runner's exception announcer drops reports into and the awaiting tool call takes them from, one per take. The run is *bound* while its turn lives (no adjudication deadline, reports buffer) and *released* when `AgentLoop.run_turn` ends (deadline starts, buffered reports re-sent as turns, later events announce). The person-asking path that stood in for the agent in foreground runs is deleted.

**Tech Stack:** Python 3.13, asyncio, pydantic config, pytest + pytest-asyncio (`asyncio_mode = "auto"`), `uv run`.

**Spec:** `docs/specs/2026-09-02-dag-foreground-report-handoff-design.md` (read it first; every section below points back to it).

## Global Constraints

Copied from AGENTS.md and from the way this repo is set up. Every task's requirements include this section.

- **Where:** the worktree `/Evermind/sh_evermind/xuedizhan/Raven/.claude/worktrees/raven_v0_2_0`, branch `feat/dag_foreground_report_handoff` (based on `refactor/raven_v0_2_0`; it already carries commit `ccf9d1f0`, the progress-deadline fix this plan builds on). Run every command from that directory. The shared main checkout at `/Evermind/sh_evermind/xuedizhan/Raven` belongs to other sessions; never edit there.
- **Comments:** add one only for a hidden constraint or a *why*; match the density of the surrounding code (this package explains its reasons at length, so a non-obvious choice gets a short paragraph and a mechanical line gets nothing). Every new file opens with a module docstring. English only.
- **Dependencies:** `uv` only. Nothing in this plan adds one.
- **Tests:** always `uv run pytest ...`, never bare `pytest`. Extend the existing `tests/test_subagent_dag_*.py` files; the one new file is `tests/test_subagent_dag_adjudication.py`, named for the module it tests. Run the DAG suite with `-p no:randomly` when a single test is being watched go red then green.
- **Commits:** Conventional Commits, `<type>(<scope>): <subject>`, header <= 100 chars, whole message ASCII English, trailer `Co-authored-by: Claude (<real session model id>) <noreply@anthropic.com>`. **AGENTS.md 3.4: never commit unprompted.** Each task ends by reporting; the commit message given there is used only once the user says to commit.
- **Formatting:** pre-commit hooks are disabled in this clone, so run `uv run ruff format <files> && uv run ruff check <files>` before reporting a task done.
- **Domain terms (AGENTS.md 6):** `outbox`, `bound`, `released` are defined in `CONTEXT.md` by Tasks 1 and 4; use those words, not synonyms (`mailbox`, `attached`, `orphaned`).
- **Test drain:** a tool-level test that leaves a run in flight must run inside `async with draining_dag_runs():` (defined near the top of `tests/test_subagent_dag_runner.py`), or the suite can hang at loop close.

## Vocabulary used by every task

- **Report / Final / Stopped:** the three outbox events. `Report(node_id, text)` is one node's exception report, text exactly as the runner built it. `Final(result, stopped)` is the run's rendered result (`str | ToolResult`, the same object `_run` returns today) with `stopped=True` when the run's cancel event was set. `Stopped()` says the run task was hard-cancelled.
- **bound / released:** see the spec's Terminology section. `Outbox.bound` is True until `release()` or `stop()`.
- **the person path:** `run_dag(adjudicate=...)`, `_adjudicate_open_nodes`, the `answered_in_turn` parameters, `wiring._adjudicate_node`, and the `Adjudicate` alias in `dag_tool.py`. Task 6 deletes it.

---

### Task 1: The outbox

**Files:**
- Modify: `raven/agent/subagent/dag_adjudication.py` (append after `AdjudicationDesk`; update the module docstring)
- Create: `tests/test_subagent_dag_adjudication.py`
- Modify: `CONTEXT.md` (insert after the `exception` term, which ends at the line `except Exception as exc` in `_run_node` for that reason.` around line 1781)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Report:  node_id: str; text: str
  @dataclass(frozen=True)
  class Final:   result: Any; stopped: bool = False
  @dataclass(frozen=True)
  class Stopped: pass
  Event = Report | Final | Stopped
  AnnounceReport = Callable[[str, str], Awaitable[None]]   # (node_id, text)
  AnnounceFinal = Callable[[Any], Awaitable[None]]         # (result)

  class Outbox:
      conversation: str
      released: asyncio.Event
      def __init__(self, *, conversation: str, announce_report: AnnounceReport, announce_final: AnnounceFinal) -> None
      @property
      def bound(self) -> bool
      async def take(self) -> Event
      async def put_report(self, node_id: str, text: str) -> None
      async def put_final(self, result: Any, *, stopped: bool = False) -> None
      async def release(self, *, flush: bool) -> None
      def stop(self) -> None
  ```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_subagent_dag_adjudication.py`:

```python
"""The outbox: a foreground DAG run's tray of reports and its result (dag_adjudication.py).

The desk carries decisions from the main agent to the run; the outbox carries
reports from the run to the main agent. These tests pin the rules the tool and
the runner rely on: a taker is registered before take() yields, reports are
handed one per take, a Final sticks, and release/stop decide what is announced.
"""

from __future__ import annotations

import asyncio

from raven.agent.subagent.dag_adjudication import Final, Outbox, Report, Stopped


class _Announced:
    def __init__(self) -> None:
        self.reports: list[tuple[str, str]] = []
        self.finals: list[object] = []

    async def report(self, node_id: str, text: str) -> None:
        self.reports.append((node_id, text))

    async def final(self, result: object) -> None:
        self.finals.append(result)


def _outbox() -> tuple[Outbox, _Announced]:
    announced = _Announced()
    return Outbox(conversation="c1", announce_report=announced.report, announce_final=announced.final), announced


async def test_reports_buffer_in_order_and_are_handed_one_per_take() -> None:
    box, announced = _outbox()
    await box.put_report("a", "report a")
    await box.put_report("b", "report b")

    assert await box.take() == Report("a", "report a")
    assert await box.take() == Report("b", "report b")
    assert announced.reports == [], "a bound outbox never announces"


async def test_a_report_put_while_a_taker_waits_goes_to_that_taker() -> None:
    box, _ = _outbox()
    taker = asyncio.create_task(box.take())
    await asyncio.sleep(0)

    await box.put_report("a", "report a")

    assert await taker == Report("a", "report a")


async def test_a_final_reaches_every_pending_taker_and_sticks() -> None:
    box, _ = _outbox()
    first = asyncio.create_task(box.take())
    second = asyncio.create_task(box.take())
    await asyncio.sleep(0)

    await box.put_final("summary")

    assert await first == Final("summary", False)
    assert await second == Final("summary", False)
    assert await box.take() == Final("summary", False), "a later take gets the final again"


async def test_release_with_flush_re_sends_the_buffer_and_switches_to_announcing() -> None:
    box, announced = _outbox()
    await box.put_report("a", "report a")
    await box.put_report("b", "report b")
    await box.put_final("summary")

    await box.release(flush=True)

    assert not box.bound
    assert box.released.is_set()
    assert announced.reports == [("a", "report a"), ("b", "report b")]
    assert announced.finals == ["summary"]

    await box.put_report("c", "report c")
    assert announced.reports[-1] == ("c", "report c"), "a released outbox announces directly"


async def test_release_without_flush_drops_the_buffer_but_later_events_still_announce() -> None:
    box, announced = _outbox()
    await box.put_report("a", "report a")

    await box.release(flush=False)

    assert announced.reports == []
    await box.put_final("summary")
    assert announced.finals == ["summary"]


async def test_a_stopped_final_is_never_announced() -> None:
    box, announced = _outbox()
    await box.put_final("cancelled summary", stopped=True)
    await box.release(flush=True)
    assert announced.finals == []

    released, announced_late = _outbox()
    await released.release(flush=True)
    await released.put_final("cancelled summary", stopped=True)
    assert announced_late.finals == []


async def test_stop_wakes_a_parked_taker() -> None:
    box, _ = _outbox()
    waiting = asyncio.create_task(box.take())
    await asyncio.sleep(0)

    box.stop()

    assert await waiting == Stopped()
    assert not box.bound


async def test_stop_drops_the_buffer_and_silences_release() -> None:
    box, announced = _outbox()
    await box.put_report("a", "report a")

    box.stop()

    assert await box.take() == Stopped(), "the buffer is dropped, not handed over"
    await box.release(flush=True)
    assert announced.reports == [], "release after stop announces nothing"


async def test_a_cancelled_taker_is_forgotten() -> None:
    box, _ = _outbox()
    taker = asyncio.create_task(box.take())
    await asyncio.sleep(0)
    taker.cancel()
    await asyncio.gather(taker, return_exceptions=True)

    await box.put_report("a", "report a")
    assert await box.take() == Report("a", "report a"), "the report was buffered, not lost on a dead taker"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_adjudication.py -q -p no:randomly`
Expected: every test errors with `ImportError: cannot import name 'Final'` (or `Outbox`) from `raven.agent.subagent.dag_adjudication`.

- [ ] **Step 3: Implement the outbox**

In `raven/agent/subagent/dag_adjudication.py`, replace the module docstring with:

```python
"""Where a suspended node waits for a decision, and where a foreground run's reports wait for a reader.

Two trays, one per run, both in memory only. The desk carries decisions from the
main agent to the run: `resolve_dag_node` lands there and the runner's wait wakes.
The outbox carries the other way: a foreground run's exception reports and its
final result wait there for the tool call that is awaiting the run. A gateway
restart drops both, and the run's nodes read back `interrupted` -- the same
outcome an in-flight run already has when the process dies.
"""
```

Extend the imports:

```python
import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
```

Append after `AdjudicationDesk`:

```python
@dataclass(frozen=True)
class Report:
    """One node's exception report, exactly as the runner built it."""

    node_id: str
    text: str


@dataclass(frozen=True)
class Final:
    """The run's rendered result. ``stopped`` is the run's cancel event at the end."""

    result: Any
    stopped: bool = False


@dataclass(frozen=True)
class Stopped:
    """The run task was hard-cancelled before it produced a result."""


Event = Report | Final | Stopped

AnnounceReport = Callable[[str, str], Awaitable[None]]
AnnounceFinal = Callable[[Any], Awaitable[None]]


class Outbox:
    """A foreground run's tray of events, waiting for the tool call that will take them.

    *Bound* while the turn that started the run is alive: events are handed to a
    waiting taker or buffered, and nothing is announced, because between the
    agent's tool calls there is no safe moment to inject a turn -- an injected
    report would either duplicate what the next ``take()`` returns or queue a
    turn behind the one about to wait on it. *Released* once that turn has
    ended: the buffer is re-sent through the announcers (or dropped, when the
    turn was cancelled) and later events announce directly, which is what turns
    a released foreground run into an ordinary backgrounded one.

    ``take()`` registers its taker before its first await. That is what lets a
    caller run ``desk.resolve(...)`` and then ``take()`` in one tick without the
    runner producing the next event into an empty tray in between.
    """

    def __init__(self, *, conversation: str, announce_report: AnnounceReport, announce_final: AnnounceFinal) -> None:
        self.conversation = conversation
        self.released = asyncio.Event()
        self._announce_report = announce_report
        self._announce_final = announce_final
        self._buffer: deque[Report | Final] = deque()
        self._takers: deque[asyncio.Future] = deque()
        self._final: Final | None = None
        self._stopped = False

    @property
    def bound(self) -> bool:
        return not self.released.is_set() and not self._stopped

    async def take(self) -> Event:
        """The next event. One report per take; a final is returned to every taker, and again after."""
        if self._stopped:
            return Stopped()
        if self._buffer:
            return self._buffer.popleft()
        if self._final is not None:
            return self._final
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._takers.append(fut)
        try:
            return await fut
        except asyncio.CancelledError:
            if fut in self._takers:
                self._takers.remove(fut)
            raise

    def _hand(self, event: Event) -> bool:
        """Give ``event`` to the oldest live taker. False when nobody is waiting."""
        while self._takers:
            fut = self._takers.popleft()
            if not fut.done():
                fut.set_result(event)
                return True
        return False

    async def put_report(self, node_id: str, text: str) -> None:
        if self._stopped:
            return
        if self.released.is_set():
            await self._announce_report(node_id, text)
            return
        event = Report(node_id, text)
        if not self._hand(event):
            self._buffer.append(event)

    async def put_final(self, result: Any, *, stopped: bool = False) -> None:
        if self._stopped:
            return
        event = Final(result, stopped)
        self._final = event
        if self.released.is_set():
            if not stopped:
                await self._announce_final(result)
            return
        handed = False
        while self._takers:
            fut = self._takers.popleft()
            if not fut.done():
                fut.set_result(event)
                handed = True
        if not handed:
            self._buffer.append(event)

    async def release(self, *, flush: bool) -> None:
        """The owning turn ended. ``flush`` re-sends what it left unanswered; a cancelled turn passes False."""
        if self._stopped or self.released.is_set():
            return
        self.released.set()
        buffered = list(self._buffer)
        self._buffer.clear()
        # No taker is waiting here: a take() lives inside a tool call, and every
        # tool call of the turn that owned this run has returned by the time the
        # turn ends. Nothing is done to the taker queue on purpose -- cancelling a
        # parked take() would read, in the tool, as the user stopping the agent
        # and abort the run.
        if not flush:
            return
        for event in buffered:
            if isinstance(event, Report):
                await self._announce_report(event.node_id, event.text)
            elif not event.stopped:
                await self._announce_final(event.result)

    def stop(self) -> None:
        """The run task is being hard-cancelled: nothing buffered is worth re-sending."""
        if self._stopped:
            return
        self._stopped = True
        self._buffer.clear()
        while self._takers:
            fut = self._takers.popleft()
            if not fut.done():
                fut.set_result(Stopped())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_dag_adjudication.py -q -p no:randomly`
Expected: `9 passed`.

- [ ] **Step 5: Define the term**

In `CONTEXT.md`, directly after the `exception` (node status) entry's last line (`except Exception as exc` in `_run_node` for that reason.`), insert:

```markdown

**outbox** (`raven/agent/subagent/dag_adjudication.py`) -- a foreground DAG run's tray of
events: its nodes' exception reports and its final result, waiting for the tool call
that is awaiting the run. One per foreground run, in memory beside the run's
adjudication desk. The desk carries decisions from the main agent to the run; the
outbox carries reports from the run to the main agent. While the run is bound the
outbox hands or buffers and never announces; once released it re-sends the buffer and
announces later events as turns.
_Avoid_: "mailbox" -- the lane's inject mailbox is a different object with a different
reader.
```

- [ ] **Step 6: Format, lint, report**

Run: `uv run ruff format raven/agent/subagent/dag_adjudication.py tests/test_subagent_dag_adjudication.py && uv run ruff check raven/agent/subagent/dag_adjudication.py tests/test_subagent_dag_adjudication.py`
Expected: clean.

Report the task done. Commit only when the user says so, with:

```
feat(agent): an outbox carries a foreground run's reports to the turn awaiting it
```

---

### Task 2: The runner waits without a deadline while the run is bound

**Files:**
- Modify: `raven/agent/subagent/dag_runner.py` -- `run_dag` signature (line 137, parameter list ends at `adjudicate=`), the `_await_adjudications` call in the wave loop (around line 374), `_await_adjudications` (lines 799-903)
- Test: `tests/test_subagent_dag_runner.py` (append after `test_a_decision_restarts_the_window_for_the_nodes_still_waiting`)

**Interfaces:**
- Consumes: nothing new (`asyncio.Event` from the standard library; Task 1's `Outbox.released` is what the tool will pass in Task 3).
- Produces: `run_dag(..., released: asyncio.Event | None = None)` and `_await_adjudications(..., timeout_s, cancel, released: asyncio.Event | None = None)`. `None` and an already-set event both mean "clocked from entry" (today's behaviour).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_dag_runner.py`, right after `test_a_decision_restarts_the_window_for_the_nodes_still_waiting`:

```python
async def test_a_bound_run_waits_without_a_deadline_until_it_is_released() -> None:
    """While the turn that owns a foreground run is alive there is no clock.

    The decision lands well past the configured window but inside a window that
    starts at release; a wait clocked from entry would already have failed the
    node when the release arrived.
    """
    from raven.agent.subagent.dag_adjudication import CONTINUE, AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("n0")
    status = {"n0": "exception"}
    errors: dict[str, str] = {}
    continuations: dict[str, str] = {}
    released = asyncio.Event()

    async def _release_later() -> None:
        await asyncio.sleep(0.3)
        released.set()

    async def _decide_later() -> None:
        await asyncio.sleep(0.4)
        desk.resolve("n0", CONTINUE, "go on")

    side = [asyncio.create_task(_release_later()), asyncio.create_task(_decide_later())]
    try:
        await _await_adjudications(desk, status, errors, continuations, timeout_s=0.2, cancel=None, released=released)
    finally:
        await asyncio.gather(*side)

    assert status == {"n0": "pending"}
    assert continuations == {"n0": "go on"}
    assert errors == {}


async def test_the_window_starts_when_the_run_is_released() -> None:
    """Released and then ignored, the node fails one window after the release, not after entry."""
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("n0")
    status = {"n0": "exception"}
    errors: dict[str, str] = {}
    released = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, released.set)

    started = time.perf_counter()
    await _await_adjudications(desk, status, errors, {}, timeout_s=0.2, cancel=None, released=released)
    elapsed = time.perf_counter() - started

    assert status == {"n0": "failed"}
    assert "timed out" in errors["n0"]
    assert 0.35 < elapsed < 0.9, f"expected release (0.2s) + window (0.2s), got {elapsed:.3f}s"


async def test_an_already_released_run_is_clocked_from_entry() -> None:
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("n0")
    status = {"n0": "exception"}
    errors: dict[str, str] = {}
    released = asyncio.Event()
    released.set()

    started = time.perf_counter()
    await _await_adjudications(desk, status, errors, {}, timeout_s=0.2, cancel=None, released=released)
    elapsed = time.perf_counter() - started

    assert status == {"n0": "failed"}
    assert elapsed < 0.5
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_runner.py -k "bound_run_waits or window_starts_when or already_released" -q -p no:randomly`
Expected: all three fail with `TypeError: _await_adjudications() got an unexpected keyword argument 'released'`.

- [ ] **Step 3: Thread `released` through `run_dag` and rewrite the wait**

In `run_dag`'s signature, after the `adjudicate` parameter, add:

```python
    released: asyncio.Event | None = None,
```

In the `run_dag` docstring, after the paragraph that starts `` ``judge_node``, when given, ``, add:

```
    ``released``, when given, is the event the host sets when the turn that owns
    this run has ended. Until it is set the adjudication wait has no deadline;
    ``adjudication_timeout_s`` is measured from the release. ``None`` clocks the
    wait from entry, which is what a backgrounded run wants.
```

In the wave loop, pass it to the wait:

```python
                    await _await_adjudications(
                        desk,
                        status,
                        errors,
                        continuations,
                        timeout_s=adjudication_timeout_s,
                        cancel=cancel,
                        released=released,
                    )
```

Replace `_await_adjudications` from its `def` line through the end of the `try:` block's `while` loop (keep the per-node settlement `for nid in open_nodes:` and the `finally:` as they are, except for the two lines noted below):

```python
async def _await_adjudications(
    desk: AdjudicationDesk,
    status: dict[str, str],
    errors: dict[str, str],
    continuations: dict[str, str],
    *,
    timeout_s: float,
    cancel: asyncio.Event | None,
    released: asyncio.Event | None = None,
) -> None:
    """Block until every suspended node has an answer, or the wait runs out.

    Reached only when nothing else in the graph can run: the report went to the
    main agent the moment the node was suspended, so this wait costs the graph
    nothing it could otherwise be doing.

    Every open node waits against one deadline, all at once rather than one
    after another -- N nodes awaited in series would cost N times `timeout_s`,
    contradicting the cap `adjudication_timeout_s` is configured against.

    That deadline measures silence, not the whole round: each decision that
    lands restarts it for the nodes still waiting. Reports reach the agent one
    turn at a time, because a conversation lane is serial, so a fixed deadline
    for the round charged a node for the time its report spent queued behind
    another node's -- far enough down the queue and a node timed out having
    never been asked at all. Silence is the thing worth failing on: it is what
    says nobody is coming, which one deadline for the round cannot distinguish
    from an agent steadily working through the queue.

    There is no deadline at all while ``released`` is given and unset: the run
    is bound to a turn that is still running, and that turn is the liveness
    signal -- the agent is in a tool loop that has the report in hand, and may
    be asking the user something only they know. The clock starts when the
    event fires, which is the moment the turn ended without deciding.

    A continued node goes back to `pending` with its message parked in
    ``continuations`` -- its dependencies are still `completed`, so the next pass
    of the ready set picks it up like any other node. Abandoned and timed-out
    nodes take the ordinary failure path, which cascades to their dependents.

    `status` and the desk are two independent records of what is suspended. A
    node this call was never given a desk entry for (`exception` in `status`
    but not `desk.is_open`) can never be resolved -- nothing will ever call
    `resolve_dag_node` for it -- so it is failed outright before the wait
    below, instead of returning with nothing changed and inviting the caller
    to loop back here with no `await` in between.
    """
    for nid, st in status.items():
        if st == "exception" and not desk.is_open(nid):
            status[nid] = "failed"
            errors[nid] = "No adjudication was ever opened for this node, so it could not be resolved."
    open_nodes = sorted(desk.open_nodes())
    if not open_nodes:
        return
    waiters = {nid: desk.waiter(nid) for nid in open_nodes}
    node_tasks = {nid: asyncio.create_task(event.wait()) for nid, event in waiters.items()}
    stop = asyncio.create_task(cancel.wait()) if cancel is not None else None
    unbind = asyncio.create_task(released.wait()) if released is not None and not released.is_set() else None
    loop = asyncio.get_running_loop()
    deadline: float | None = None if unbind is not None else loop.time() + timeout_s
    cancelled = False
    try:
        node_pending = set(node_tasks.values())
        while node_pending:
            budget = None if deadline is None else max(0.0, deadline - loop.time())
            waiting_on: set[asyncio.Future] = set(node_pending)
            if stop is not None:
                waiting_on.add(stop)
            if unbind is not None and not unbind.done():
                waiting_on.add(unbind)
            done, _ = await asyncio.wait(waiting_on, timeout=budget, return_when=asyncio.FIRST_COMPLETED)
            if stop is not None and stop in done:
                cancelled = True
                break
            if unbind is not None and unbind in done:
                deadline = loop.time() + timeout_s
            decided = done & node_pending
            node_pending -= decided
            still_bound = unbind is not None and not unbind.done()
            if decided:
                deadline = None if still_bound else loop.time() + timeout_s
            elif deadline is not None and loop.time() >= deadline:
                break
```

In the `finally:` block, extend the two cleanup lines so the unbind task is reaped like the stop task:

```python
        if stop is not None and not stop.done():
            stop.cancel()
        if unbind is not None and not unbind.done():
            unbind.cancel()
```

and

```python
        await asyncio.gather(
            *node_tasks.values(),
            *([stop] if stop is not None else []),
            *([unbind] if unbind is not None else []),
            return_exceptions=True,
        )
```

- [ ] **Step 4: Run the tests to verify they pass, and that nothing else moved**

Run: `uv run pytest tests/test_subagent_dag_runner.py -k "adjudication or deadline or window or released" -q -p no:randomly`
Expected: all pass, including `test_suspended_nodes_share_one_deadline_rather_than_queueing` and `test_a_decision_restarts_the_window_for_the_nodes_still_waiting`.

Run: `uv run pytest tests/test_subagent_dag_runner.py -q`
Expected: all pass.

- [ ] **Step 5: Format, lint, report**

Run: `uv run ruff format raven/agent/subagent/dag_runner.py tests/test_subagent_dag_runner.py && uv run ruff check raven/agent/subagent/dag_runner.py tests/test_subagent_dag_runner.py`

Report the task done. Commit message for when the user asks:

```
feat(agent): a bound DAG run waits for its decision without a deadline
```

---

### Task 3: The foreground lane runs through the outbox

**Files:**
- Modify: `raven/agent/subagent/dag_tool.py` -- imports (line 64), module docstring (lines 17-20), `__init__` body after `self._desks` (line 365), `parameters` (`background` description, lines 691-699), `_execute` from `if not background:` through `self._adopt(...)` (lines 942-966), `_run_and_announce` (lines 1039-1069), `_run` signature and its `run_dag(...)` call (lines 1163-1204)
- Test: `tests/test_subagent_dag_runner.py` (append at the end of the file)
- Modify: `raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md` (two paragraphs, lines 84-86 and 120-123)

**Interfaces:**
- Consumes: `Outbox`, `Report`, `Final`, `Stopped` from Task 1; `run_dag(..., released=)` from Task 2.
- Produces, on `SubAgentDagTool`:
  ```python
  def is_foreground(self, run_id: str) -> bool
  async def await_run(self, run_id: str) -> Report | Final | Stopped | None   # None when not a bound foreground run
  def abort_run(self, run_id: str) -> None                                     # outbox.stop() then task.cancel()
  def render_event(self, run_id: str, event: Report | Final | Stopped) -> str | ToolResult
  async def _run_detached(self, spec, run_id, cancel, origin, dirs, call_id, auto_instances, dispatch_backends, outbox: Outbox | None) -> None
  async def _run(self, spec, run_id, cancel, origin, dirs, call_id, auto_instances, dispatch_backends, outbox: Outbox | None = None) -> str | ToolResult
  ```
  `self._outboxes: dict[str, Outbox]`, retired by `_retire`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_dag_runner.py`:

```python
# --- a foreground run hands its reports to the turn awaiting it ------------------


def _foreground_tool(tmp_path: Path, verdicts: list[Any], **kwargs: Any) -> SubAgentDagTool:
    """A graph tool over the real ``cat`` sub-agent, judged by a scripted verdict list.

    Each judge call pops the next verdict; once the list is spent every node is
    accomplished. ``cat`` echoes the prompt, so a node finishes in milliseconds
    and the test spends its time on the handoff, which is what is under test.
    """
    from raven.agent.subagent.dag_verdict import Verdict

    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        **kwargs,
    )
    tool.set_context("web", "default", "web:fg")
    script = list(verdicts)

    async def _judge(**_kwargs: Any) -> Verdict:
        return script.pop(0) if script else Verdict(accomplished=True)

    tool._judge_node = lambda: _judge  # type: ignore[method-assign]
    return tool


def _falls_short(what: str = "a token"):
    from raven.agent.subagent.dag_verdict import Verdict

    return Verdict(accomplished=False, category="missing_credential", what_is_missing=what)


_CHAIN = [
    {"id": "a", "subagent": "echo", "node_summary": "first", "prompt_template": "do a"},
    {"id": "b", "subagent": "echo", "node_summary": "second", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
]
_PAIR = [
    {"id": "a", "subagent": "echo", "node_summary": "first", "prompt_template": "do a"},
    {"id": "b", "subagent": "echo", "node_summary": "second", "prompt_template": "do b"},
]


async def test_a_foreground_call_returns_the_first_report_while_the_graph_keeps_running(tmp_path: Path) -> None:
    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short()])

        out = await tool.execute(task_summary="fg", nodes=_CHAIN, background=False)

        text = out.model_text
        assert "did not accomplish its task" in text and "a token" in text
        assert "resolve_dag_node" in text
        assert "returned before the graph finished" in text, "the agent is told the protocol"
        (run_id,) = list(tool._runs)
        assert not tool._runs[run_id].done(), "the graph is still running"
        assert tool.is_foreground(run_id)


async def test_resolving_the_node_and_awaiting_returns_the_final_result(tmp_path: Path) -> None:
    from raven.agent.subagent.dag_adjudication import Final

    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short()])
        await tool.execute(task_summary="fg", nodes=_CHAIN, background=False)
        (run_id,) = list(tool._runs)

        assert tool.resolve_node(run_id, "a", "continue", "use the staging token")
        event = await tool.await_run(run_id)

        assert isinstance(event, Final) and not event.stopped
        final = tool.render_event(run_id, event)
        assert "2 completed" in final.model_text
        assert await tool.await_run(run_id) in (event, None), "after the run ends there is nothing more to wait for"


async def test_a_second_suspended_node_is_handed_over_on_the_next_await(tmp_path: Path) -> None:
    from raven.agent.subagent.dag_adjudication import Final, Report

    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short("token a"), _falls_short("token b")])
        first = await tool.execute(task_summary="fg", nodes=_PAIR, background=False)
        (run_id,) = list(tool._runs)
        first_node = "a" if "node 'a'" in first.model_text else "b"
        other = "b" if first_node == "a" else "a"

        assert tool.resolve_node(run_id, first_node, "continue", "here you go")
        second = await tool.await_run(run_id)

        assert isinstance(second, Report) and second.node_id == other, "one report per take, the other node's"
        assert tool.resolve_node(run_id, other, "abandon", None)
        final = await tool.await_run(run_id)
        assert isinstance(final, Final)
        assert "1 completed" in tool.render_event(run_id, final).model_text
        assert "1 failed" in tool.render_event(run_id, final).model_text


async def test_a_foreground_run_with_no_suspension_returns_the_summary_as_before(tmp_path: Path) -> None:
    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [])
        out = await tool.execute(task_summary="fg", nodes=_CHAIN, background=False)
        assert "2 completed" in out.model_text
        assert "returned before the graph finished" not in out.model_text
        assert not tool._runs, "a finished run leaves no task behind"
        assert not tool._outboxes


async def test_cancelling_the_awaiting_call_cancels_the_run(tmp_path: Path) -> None:
    class _Sleeper:
        kind = "raven-loop"

        async def run(self, task: str, **_kwargs: Any) -> str:
            await asyncio.sleep(60)
            return "never"

    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [])
        tool._resolve_node = lambda node: _Sleeper()  # type: ignore[method-assign]
        call = asyncio.create_task(tool.execute(task_summary="fg", nodes=_CHAIN[:1], background=False))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if tool._runs:
                break
        (run_id,) = list(tool._runs)
        run_task = tool._runs[run_id]

        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.gather(run_task, return_exceptions=True)

        assert run_task.cancelled()
        assert run_id not in tool._runs and run_id not in tool._outboxes
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_runner.py -k "foreground_call_returns or resolving_the_node_and or second_suspended_node or no_suspension_returns or cancelling_the_awaiting" -q -p no:randomly`
Expected: the first three fail (`AttributeError: 'SubAgentDagTool' object has no attribute 'is_foreground'` / `_outboxes` / `await_run`); the no-suspension one fails on `tool._outboxes`; the cancel one fails because `tool._runs` is empty for a foreground call (the run is inline today) and the `(run_id,) = list(tool._runs)` unpack raises.

- [ ] **Step 3: Implement the lane**

In `raven/agent/subagent/dag_tool.py`:

Imports -- extend the adjudication import and add the fence used for announced reports:

```python
from raven.agent.subagent.dag_adjudication import AdjudicationDesk, Final, Outbox, Report, Stopped
from raven.security.trust import wrap_untrusted
```

(Check the existing import line for `AdjudicationDesk` and extend it rather than adding a second one.)

Module docstring -- replace the paragraph that begins `A run is backgrounded by default, like ``spawn``:` with:

```
A run is backgrounded by default, like ``spawn``: the call returns as soon as
the graph is accepted and the result comes back later as an announced turn.
``background=false`` blocks instead -- until the graph finishes, or until a
node reports it could not accomplish its task, whichever comes first. That
report is the call's result; the agent answers it with ``resolve_dag_node``,
which returns the next report or the final result. The run is *bound* to the
turn that started it: no adjudication deadline runs while that turn lives, and
when it ends the run is *released* and behaves as a backgrounded one from then
on (see ``raven.agent.subagent.dag_adjudication.Outbox``).
```

`__init__` -- after `self._desks: dict[str, AdjudicationDesk] = {}` add:

```python
        self._outboxes: dict[str, Outbox] = {}
```

`parameters` -- replace the `background` description string with:

```python
                    "description": (
                        "Default true: return as soon as the run starts and get the result as an "
                        "announcement when the graph finishes, leaving you free to work meanwhile. "
                        "Set false only when you cannot continue without the outputs: the call then "
                        "blocks until the graph finishes or a node reports it could not accomplish "
                        "its task, whichever comes first, and answering that report with "
                        "resolve_dag_node resumes the wait."
                    ),
```

`_execute` -- replace everything from `if not background:` down to and including the `self._adopt(run_id, task, origin.conversation)` line with:

```python
        outbox: Outbox | None = None
        if not background:
            outbox = Outbox(
                conversation=origin.conversation,
                announce_report=self._report_announcer(run_id, origin),
                announce_final=self._final_announcer(run_id, origin),
            )
            self._outboxes[run_id] = outbox

        task = asyncio.create_task(
            self._run_detached(spec, run_id, cancel, origin, dirs, call_id, auto_instances, dispatch_backends, outbox)
        )
        self._runs[run_id] = task

        def _retire(_t: "asyncio.Task") -> None:
            # A task cancelled before its first tick never enters _run, whose
            # finally is what normally retires the run's cancel/desk entries --
            # and that window is real: _adopt indexes the task immediately, so
            # a same-tick /stop or the shutdown sweep cancels it un-started.
            # Left behind, active_run_ids() lists the dead run forever and its
            # node rows stay pinned running. The pops are idempotent with the
            # finally's own.
            self._runs.pop(run_id, None)
            self._cancels.pop(run_id, None)
            self._desks.pop(run_id, None)
            self._outboxes.pop(run_id, None)

        task.add_done_callback(_retire)
        if self._adopt is not None:
            self._adopt(run_id, task, origin.conversation)
        if outbox is not None:
            # Registered before the task's first tick: create_task only schedules
            # it, and take() parks its taker before yielding, so the run cannot
            # produce an event into an empty tray.
            try:
                event = await outbox.take()
            except asyncio.CancelledError:
                # The user stopped the agent while it was blocked on this graph.
                # A blocking call means "I am waiting on this", so the graph goes
                # with the turn, as it did when the call ran the graph inline.
                self.abort_run(run_id)
                raise
            return _with_notices(self.render_event(run_id, event), notices)
```

The `controls = ""` block and the background `return _with_notices(ToolResult(...))` that follow stay exactly as they are.

Add these methods next to `resolve_node` (after it):

```python
    def is_foreground(self, run_id: str) -> bool:
        """Whether ``run_id`` is a foreground run still bound to the turn that started it."""
        outbox = self._outboxes.get(run_id)
        return outbox is not None and outbox.bound

    async def await_run(self, run_id: str) -> "Report | Final | Stopped | None":
        """The next event of a bound foreground run, or None when there is no such run to wait on."""
        outbox = self._outboxes.get(run_id)
        if outbox is None or not outbox.bound:
            return None
        return await outbox.take()

    def abort_run(self, run_id: str) -> None:
        """Hard-cancel a run whose awaiting tool call was cancelled.

        The outbox is stopped first so a release that races this (the turn's own
        finally) finds nothing to re-send for a run that is being killed.
        """
        outbox = self._outboxes.get(run_id)
        if outbox is not None:
            outbox.stop()
        task = self._runs.get(run_id)
        if task is not None and not task.done():
            task.cancel()

    def render_event(self, run_id: str, event: "Report | Final | Stopped") -> "str | ToolResult":
        """One outbox event as a tool result, addressed to the agent that is waiting."""
        if isinstance(event, Report):
            # Fenced like an announced report is (SubagentManager.announce_dag_exception):
            # the report quotes the node's output and transcript.
            return ToolResult(
                model_text=wrap_untrusted(event.text, source="subagent") + "\n\n" + _FOREGROUND_REPORT_TAIL,
                display_text=f"DAG {run_id}: node {event.node_id} needs a decision",
            )
        if isinstance(event, Final):
            return event.result
        if isinstance(event, Stopped):
            return f"DAG run {run_id} was stopped before it finished."
        raise TypeError(f"not an outbox event: {event!r}")

    def _report_announcer(self, run_id: str, origin: _DagOrigin):
        async def _announce(node_id: str, text: str) -> None:
            if self._announce_exception is None:
                logger.info("DAG run {} node {} reported after release with no announcer wired", run_id, node_id)
                return
            await self._announce_exception(run_id, node_id, text, origin.as_dict())

        return _announce

    def _final_announcer(self, run_id: str, origin: _DagOrigin):
        async def _announce(result: Any) -> None:
            if self._announce is None:
                logger.info("DAG run {} finished after release with no announcer wired", run_id)
                return
            await self._announce(run_id, str(getattr(result, "model_text", result)), origin.as_dict())

        return _announce
```

Add the module-level constant near the other module constants (after `GUIDE_SKILL_ID` or the `_CLOSE_TIMEOUT_SECONDS` line):

```python
_FOREGROUND_REPORT_TAIL = (
    "This call returned before the graph finished. The graph is still running and this node "
    "is waiting for your decision. Decide with resolve_dag_node, which returns the next such "
    "report or the run's final result. The deadline above is paused while this turn runs; if "
    "you end the turn without deciding, the report is re-sent to you as a message and the "
    "deadline starts."
)
```

Replace `_run_and_announce` with:

```python
    async def _run_detached(
        self,
        spec: SubAgentDagSpec,
        run_id: str,
        cancel: asyncio.Event,
        origin: _DagOrigin,
        dirs: _RunDirs,
        call_id: str | None,
        auto_instances: frozenset[str],
        dispatch_backends: dict[str, Any],
        outbox: Outbox | None,
    ) -> None:
        """Run a graph as its own task, then hand the result on.

        A foreground run puts it in its outbox: the tool call awaiting the run
        takes it, or, if the turn has ended by then, the outbox announces it. A
        backgrounded run announces it directly, as before.
        """
        try:
            result = await self._run(
                spec, run_id, cancel, origin, dirs, call_id, auto_instances, dispatch_backends, outbox=outbox
            )
        except asyncio.CancelledError:
            if outbox is not None:
                outbox.stop()
            raise
        if outbox is not None:
            await outbox.put_final(result, stopped=cancel.is_set())
            return
        if cancel.is_set():
            # A stop the user asked for. ``run_dag`` still returns normally,
            # with a running node recorded ``cancelled`` and a pending one
            # skipped, but announcing that would spend a turn narrating what
            # they just cancelled -- which is why a cancelled spawn stays
            # silent too.
            logger.info("DAG run {} was stopped; not announcing a result", run_id)
            return
        if self._announce is None:
            logger.info("DAG run {} finished with no announcer wired; result reaches no one", run_id)
            return
        try:
            await self._announce(
                run_id,
                str(getattr(result, "model_text", result)),
                origin.as_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - a failed announce must not also lose the log line
            logger.error("DAG run {} finished but its result could not be announced: {}", run_id, exc)
```

In `_run`, change the signature's last parameter from `foreground: bool = False,` to `outbox: Outbox | None = None,`, and just before the `run_dag(` call add:

```python
        announce_exception = self._announce_exception
        released: asyncio.Event | None = None
        if outbox is not None:

            async def _to_outbox(_run_id: str, node_id: str, report: str, _origin: dict[str, str]) -> None:
                await outbox.put_report(node_id, report)

            announce_exception = _to_outbox
            released = outbox.released
```

and in the `run_dag(...)` call replace `announce_exception=self._announce_exception,` with `announce_exception=announce_exception,`, replace `adjudicate=self._adjudicate if foreground else None,` with `adjudicate=None,`, and add `released=released,`. (Task 6 removes the `adjudicate=None` line along with the parameter.)

Search the file for any other reference to `_run_and_announce` (the `_retire` comment mentions the old flow; the test `test_an_unstarted_background_run_retires_every_per_run_entry` monkeypatches `tool._run_and_announce`) and update: in that test, replace `tool._run_and_announce = _never` with `tool._run_detached = _never`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_dag_runner.py -k "foreground_call_returns or resolving_the_node_and or second_suspended_node or no_suspension_returns or cancelling_the_awaiting or retires_every_per_run_entry" -q -p no:randomly`
Expected: all pass.

Run: `uv run pytest tests/test_subagent_dag_runner.py tests/test_subagent_dag_core.py -q`
Expected: all pass. If `test_only_the_foreground_lane_is_handed_the_adjudicator` fails here, that is expected: it pins the person path that Task 6 deletes, and its `lanes[0] is _adjudicate` assertion is now false. Leave it red until Task 6 and say so in the report.

- [ ] **Step 5: Teach the model the new contract**

In `raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md`, replace the paragraph

```
Pass `background: false` only when you genuinely cannot continue without the outputs — for
instance when the very next thing you must do is read them. That blocks your turn until every
node is done and returns the summary below as the call's result.
```

with

```
Pass `background: false` only when you genuinely cannot continue without the outputs — for
instance when the very next thing you must do is read them. That blocks your turn until the
graph finishes, or until a node reports it could not do its job, whichever comes first. In the
second case the call returns that node's report instead of the summary; see below for what to
do with it.
```

and replace the paragraph

```
All of this is about a backgrounded run. In a `background: false` call there is no
message to you and no `resolve_dag_node` to call: the question goes straight to the user
and the answer is applied before your call returns, so what you get back already reflects
whatever they decided.
```

with

```
In a `background: false` call the report does not arrive as a message: it is the call's own
return value, and the graph keeps running while you read it. Answer it the same way, with
`resolve_dag_node` -- which, for a blocking run, waits and returns the next report or the
final summary, so keep calling it until you have the summary. While your turn is running the
graph waits for you without a deadline; ask the user first if only they can supply what is
missing. If you end your turn with a report unanswered, the run carries on as a background
run: the report is re-sent to you as a message and the usual deadline starts.
```

- [ ] **Step 6: Format, lint, report**

Run: `uv run ruff format raven/agent/subagent/dag_tool.py tests/test_subagent_dag_runner.py && uv run ruff check raven/agent/subagent/dag_tool.py tests/test_subagent_dag_runner.py`

Report the task done, naming the one test expected red until Task 6. Commit message for when the user asks:

```
feat(agent): a foreground DAG call returns a node's report and resumes on resolve
```

---

### Task 4: Release a run when its turn ends

**Files:**
- Modify: `raven/agent/subagent/dag_tool.py` (add `release_turn` after `abort_run`)
- Modify: `raven/agent/loop/main.py` -- `run_turn` body (lines 764-780) and a new `_release_dag_runs` method after it
- Test: `tests/test_subagent_dag_runner.py` (append), `tests/test_agent_loop_run_emit.py` (append)
- Modify: `CONTEXT.md` (insert after the `outbox` term from Task 1)

**Interfaces:**
- Consumes: `Outbox.release(flush=)`, `Outbox.bound`, `Outbox.conversation` (Task 1); `self._outboxes` (Task 3).
- Produces: `SubAgentDagTool.release_turn(conversation: str, *, flush: bool) -> None` (async) and `AgentLoop._release_dag_runs(session_key: str, *, flush: bool) -> None` (async).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_dag_runner.py`:

```python
async def _both_suspended(tool: SubAgentDagTool, run_id: str) -> None:
    for _ in range(300):
        desk = tool._desks.get(run_id)
        if desk is not None and desk.open_nodes() == {"a", "b"}:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("both nodes should have suspended by now")


async def test_release_re_sends_the_unanswered_report_and_clocks_the_run(tmp_path: Path) -> None:
    """A released foreground run becomes a backgrounded one, with full information."""
    from raven.config.raven import SubagentDagConfig

    re_sent: list[tuple[str, str, dict]] = []
    results: list[str] = []

    async def _announce_exception(run_id: str, node_id: str, report: str, origin: dict) -> None:
        re_sent.append((node_id, report, origin))

    async def _announce(run_id: str, summary: str, origin: dict) -> None:
        results.append(summary)

    async with draining_dag_runs():
        tool = _foreground_tool(
            tmp_path,
            [_falls_short("token a"), _falls_short("token b")],
            announce=_announce,
            announce_exception=_announce_exception,
            verdict_config=SubagentDagConfig(adjudication_timeout_seconds=0.3),
        )
        first = await tool.execute(task_summary="fg", nodes=_PAIR, background=False)
        (run_id,) = list(tool._runs)
        await _both_suspended(tool, run_id)
        handed = "a" if "node 'a'" in first.model_text else "b"
        buffered = "b" if handed == "a" else "a"

        await tool.release_turn("web:fg", flush=True)

        assert not tool.is_foreground(run_id)
        assert [node for node, _, _ in re_sent] == [buffered], "only the report nobody took is re-sent"
        assert re_sent[0][2]["session_key"] == "web:fg"
        assert "returned before the graph finished" not in re_sent[0][1], "an announced report carries no tail"

        await asyncio.wait_for(tool._runs[run_id], timeout=5)
        assert len(results) == 1 and "2 failed" in results[0], "clocked after release, both nodes time out and the summary announces"


async def test_release_without_flush_drops_the_report_but_still_announces_the_result(tmp_path: Path) -> None:
    from raven.config.raven import SubagentDagConfig

    re_sent: list[str] = []
    results: list[str] = []

    async def _announce_exception(run_id: str, node_id: str, report: str, origin: dict) -> None:
        re_sent.append(node_id)

    async def _announce(run_id: str, summary: str, origin: dict) -> None:
        results.append(summary)

    async with draining_dag_runs():
        tool = _foreground_tool(
            tmp_path,
            [_falls_short("token a"), _falls_short("token b")],
            announce=_announce,
            announce_exception=_announce_exception,
            verdict_config=SubagentDagConfig(adjudication_timeout_seconds=0.3),
        )
        await tool.execute(task_summary="fg", nodes=_PAIR, background=False)
        (run_id,) = list(tool._runs)
        await _both_suspended(tool, run_id)

        await tool.release_turn("web:fg", flush=False)

        await asyncio.wait_for(tool._runs[run_id], timeout=5)
        assert re_sent == []
        assert len(results) == 1


async def test_release_touches_only_the_named_conversation(tmp_path: Path) -> None:
    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short()])
        await tool.execute(task_summary="fg", nodes=_CHAIN, background=False)
        (run_id,) = list(tool._runs)

        await tool.release_turn("web:somebody-else", flush=True)

        assert tool.is_foreground(run_id), "another conversation's turn ending must not release this run"
```

Append to `tests/test_agent_loop_run_emit.py`:

```python
# ── run_turn releases the conversation's foreground DAG runs when it ends ────────


class _ReleaseRecorder(Tool):
    """Stands in for run_subagent_dag: records release_turn calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    @property
    def name(self) -> str:
        return "run_subagent_dag"

    @property
    def description(self) -> str:
        return "records releases"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return "unused"

    async def release_turn(self, conversation: str, *, flush: bool) -> None:
        self.calls.append((conversation, flush))


def _loop_with_recorder(tmp_path):
    from raven.agent.loop._shared import LoopOutcome

    loop = AgentLoop(provider=_FakeChatProvider([]), workspace=tmp_path)
    _stub_edges(loop)
    recorder = _ReleaseRecorder()
    loop.tools.register(recorder)

    async def _ends_normally(req, emit, drain, **kwargs):
        return LoopOutcome()

    loop._run_turn = _ends_normally
    return loop, recorder


async def test_run_turn_releases_the_conversations_dag_runs_when_it_ends(tmp_path):
    loop, recorder = _loop_with_recorder(tmp_path)

    await loop.run_turn(_req("hi"), _EmitCollector(), _drain)

    assert recorder.calls == [("cli:c", True)]


async def test_a_cancelled_turn_releases_without_flushing(tmp_path):
    import asyncio

    loop, recorder = _loop_with_recorder(tmp_path)

    async def _cancelled(req, emit, drain, **kwargs):
        raise asyncio.CancelledError()

    loop._run_turn = _cancelled

    with pytest.raises(asyncio.CancelledError):
        await loop.run_turn(_req("hi"), _EmitCollector(), _drain)

    assert recorder.calls == [("cli:c", False)]


async def test_a_direct_chat_turn_releases_nothing(tmp_path):
    loop, recorder = _loop_with_recorder(tmp_path)
    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="hi",
        direct_target=("echo", "h1"),
    )

    await loop.run_turn(req, _EmitCollector(), _drain)

    assert recorder.calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_runner.py -k "release_" -q -p no:randomly`
Expected: `AttributeError: 'SubAgentDagTool' object has no attribute 'release_turn'`.

Run: `uv run pytest tests/test_agent_loop_run_emit.py -k "releases or cancelled_turn or direct_chat_turn" -q -p no:randomly`
Expected: the first two fail with `assert [] == [...]` (nothing calls the recorder yet); the third passes trivially -- that is fine, it pins the guard once the other two go green.

- [ ] **Step 3: Implement the release**

In `raven/agent/subagent/dag_tool.py`, after `abort_run`:

```python
    async def release_turn(self, conversation: str, *, flush: bool) -> None:
        """The turn on ``conversation`` has ended: release every foreground run it still binds.

        ``flush`` is False when the turn was cancelled: the user just stopped the
        agent, and re-raising the run's open questions at them is noise. A run
        released this way still announces what happens to it later.
        """
        for outbox in list(self._outboxes.values()):
            if outbox.bound and outbox.conversation == conversation:
                await outbox.release(flush=flush)
```

In `raven/agent/loop/main.py`, replace the body of `run_turn` after its docstring (from `session_key = ...` to the end of the method) with:

```python
        session_key = req.conversation or f"{req.source.channel}:{req.source.chat_id}"
        flush = True
        try:
            # The tools a session brought with it become visible here, for the same
            # reason the model binding does: this is where the turn's task begins.
            # The request handler that accepted them cannot open the scope itself --
            # it submits the turn onto the spine and the turn runs on a task that
            # inherits nothing from it.
            with use_binding(self.binding_for_session(session_key)), self.tools.session_scope_for(session_key):
                return await self._run_turn(
                    req,
                    emit,
                    drain,
                    stream=stream,
                    inline_tool_stream=inline_tool_stream,
                    usage_sink=usage_sink,
                    text_sink=text_sink,
                )
        except asyncio.CancelledError:
            flush = False
            raise
        finally:
            # Every way a turn ends passes here, which is what makes this the
            # place a foreground DAG run learns its turn is over. A direct chat
            # runs on an instance's own lane, concurrently with the main agent's
            # turn, and can own no graph, so its end must not release the main
            # turn's runs.
            if req.direct_target is None:
                await self._release_dag_runs(session_key, flush=flush)

    async def _release_dag_runs(self, session_key: str, *, flush: bool) -> None:
        """Release the foreground DAG runs the turn on ``session_key`` still binds.

        ``flush=False`` performs no awaits inside the tool, so this is safe to
        run while a CancelledError is propagating. A failure here is logged
        rather than raised: the turn has already ended, and its outcome must not
        be replaced by bookkeeping on its graphs.
        """
        tool = self.tools.get("run_subagent_dag")
        release = getattr(tool, "release_turn", None)
        if release is None:
            return
        try:
            await release(session_key, flush=flush)
        except Exception:  # noqa: BLE001 - see docstring
            logger.opt(exception=True).warning("DAG runs of {} could not be released", session_key)
```

Make sure `asyncio` and `logger` are imported in `main.py` (grep `^import asyncio` and `logger` near the top; add `import asyncio` if missing).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_dag_runner.py -k "release_" -q -p no:randomly`
Expected: 3 passed.

Run: `uv run pytest tests/test_agent_loop_run_emit.py -q`
Expected: all pass.

- [ ] **Step 5: Define the terms**

In `CONTEXT.md`, directly after the `outbox` entry added in Task 1, insert:

```markdown

**bound / released** (foreground DAG run; `raven/agent/subagent/dag_tool.py`,
`raven/agent/loop/main.py`) -- a `background: false` run is *bound* while the turn that
started it is still running, and *released* once that turn has ended, however it ended
(`AgentLoop.run_turn` releases every run the conversation's turn still binds). A bound
run's suspended nodes wait without a deadline and its outbox buffers; a released run
behaves as a backgrounded one: the adjudication window is clocked from the release, the
unanswered reports are re-sent as turns (dropped instead when the turn was cancelled), and
later events announce. `resolve_dag_node` blocks only on a bound run.
_Avoid_: "orphaned" -- a released run is not lost, it has changed lane.
```

- [ ] **Step 6: Format, lint, report**

Run: `uv run ruff format raven/agent/subagent/dag_tool.py raven/agent/loop/main.py tests/test_subagent_dag_runner.py tests/test_agent_loop_run_emit.py && uv run ruff check raven/agent/subagent/dag_tool.py raven/agent/loop/main.py tests/test_subagent_dag_runner.py tests/test_agent_loop_run_emit.py`

Report the task done. Commit message for when the user asks:

```
feat(agent): a foreground DAG run is released when the turn that started it ends
```

---

### Task 5: `resolve_dag_node` decides and keeps waiting on a foreground run

**Files:**
- Modify: `raven/agent/subagent/dag_control_tools.py` -- imports, `_ControlTool.execute` return annotation, `ResolveDagNodeTool` (lines 148-235)
- Test: `tests/test_subagent_dag_control_tools.py` -- extend `_ResolvableDagTool` (around line 253) and append tests after `test_resolve_confirms_a_continue`

**Interfaces:**
- Consumes: `SubAgentDagTool.is_foreground`, `await_run`, `abort_run`, `render_event` (Task 3), through the duck-typed registered tool.
- Produces: `ResolveDagNodeTool.blocking_for(params) -> bool`; `ResolveDagNodeTool.execute(...) -> str | ToolResult`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_subagent_dag_control_tools.py`, replace the `_ResolvableDagTool` class with:

```python
class _ResolvableDagTool(_DagTool):
    """A run_subagent_dag double: read_run succeeds, resolve_node is recorded, and a
    foreground run hands scripted events to await_run."""

    def __init__(self, resolves: bool = True, *, foreground: bool = False, events: list[Any] | None = None) -> None:
        super().__init__(_finished_run())
        self._resolves = resolves
        self._foreground = foreground
        self.events: list[Any] = list(events or [])
        self.resolved: tuple[str, str, str, str | None] | None = None
        self.awaited: list[str] = []
        self.aborted: list[str] = []
        self.release_taker = asyncio.Event()

    def resolve_node(self, run_id: str, node_id: str, decision: str, message: str | None) -> bool:
        self.resolved = (run_id, node_id, decision, message)
        return self._resolves

    def is_foreground(self, run_id: str) -> bool:
        return self._foreground and run_id == "r1"

    async def await_run(self, run_id: str) -> Any:
        self.awaited.append(run_id)
        if not self.is_foreground(run_id):
            return None
        if self.events:
            return self.events.pop(0)
        await self.release_taker.wait()
        return None

    def abort_run(self, run_id: str) -> None:
        self.aborted.append(run_id)

    def render_event(self, run_id: str, event: Any) -> str:
        return f"RENDERED {run_id} {event}"
```

Add `import asyncio` to the file's imports. Update `_LoopWithRun.__init__` to pass keyword arguments through:

```python
    def __init__(self, resolves: bool = True, **kwargs: Any) -> None:
        self._dag_tool = _ResolvableDagTool(resolves=resolves, **kwargs)
        super().__init__(tool=self._dag_tool)
```

Append after `test_resolve_confirms_a_continue`:

```python
async def test_resolve_blocks_only_for_a_bound_foreground_run():
    background = ResolveDagNodeTool(loop=_LoopWithRun())
    foreground = ResolveDagNodeTool(loop=_LoopWithRun(foreground=True))

    assert background.blocking_for({"run_id": "r1"}) is False
    assert foreground.blocking_for({"run_id": "r1"}) is True
    assert foreground.blocking_for({"run_id": "other"}) is False
    assert ResolveDagNodeTool(loop=_Loop()).blocking_for({"run_id": "r1"}) is False, "no graph tool, nothing to block on"


async def test_a_foreground_resolve_returns_the_next_event_rendered():
    loop = _LoopWithRun(foreground=True, events=["next report"])
    tool = ResolveDagNodeTool(loop=loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="continue", message="use staging")

    assert loop.resolved == ("r1", "a", "continue", "use staging")
    assert loop._dag_tool.awaited == ["r1"]
    assert out == "RENDERED r1 next report"


async def test_a_background_resolve_does_not_wait():
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="continue", message="use staging")

    assert out == "Node 'a' of run r1 will run again with your message."
    assert loop._dag_tool.awaited == ["r1"], "asked, and told there is nothing to wait on"


async def test_a_refused_resolve_never_waits():
    loop = _LoopWithRun(resolves=False, foreground=True, events=["would be wrong"])
    tool = ResolveDagNodeTool(loop=loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="abandon")

    assert "no longer waiting" in out.lower()
    assert loop._dag_tool.awaited == []


async def test_cancelling_a_waiting_resolve_aborts_the_run():
    loop = _LoopWithRun(foreground=True)
    tool = ResolveDagNodeTool(loop=loop)
    call = asyncio.create_task(tool.execute(run_id="r1", node_id="a", decision="abandon"))
    await asyncio.sleep(0)
    assert loop._dag_tool.awaited == ["r1"]

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert loop._dag_tool.aborted == ["r1"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_control_tools.py -k "resolve" -q -p no:randomly`
Expected: `test_resolve_blocks_only_for_a_bound_foreground_run` fails (`blocking_for` is the base class's, always False); `test_a_foreground_resolve_returns_the_next_event_rendered` fails (`out` is the plain continue text, `awaited == []`); `test_a_background_resolve_does_not_wait` fails on `awaited == ["r1"]`; `test_cancelling_a_waiting_resolve_aborts_the_run` fails (the call returns immediately, no CancelledError). The refused-resolve test passes already and stays as a guard. The pre-existing resolve tests keep passing.

- [ ] **Step 3: Implement**

In `raven/agent/subagent/dag_control_tools.py`:

Imports -- add:

```python
import asyncio

from raven.contracts.tool import Tool, ToolResult
```

(extend the existing `from raven.contracts.tool import Tool` line.)

In `ResolveDagNodeTool`, add before `execute`:

```python
    def blocking_for(self, params: dict[str, Any]) -> bool:
        """Blocking only when the named run is a bound foreground run.

        The registry consults this to decide whether to put a ceiling on the
        call, and the turn stream reports it as the call's blocking flag; both
        must say "may go silent" for exactly the calls that will wait on the
        graph and for no others.
        """
        tool = _registered_tool(self._loop)
        is_foreground = getattr(tool, "is_foreground", None)
        run_id = params.get("run_id")
        return bool(is_foreground is not None and isinstance(run_id, str) and is_foreground(run_id))
```

Replace the tail of `execute` -- from `if not resolve_node(self._loop, run_id, node_id, decision, message):` to the end of the method -- with:

```python
        if not resolve_node(self._loop, run_id, node_id, decision, message):
            return (
                f"Node '{node_id}' of run {run_id} is no longer waiting for a decision: it timed "
                f'out, the run was cancelled, or the id is wrong. dag_status("{run_id}") shows '
                "where every node stands."
            )
        # A bound foreground run: the turn that started it is this one, blocked
        # on the graph by choice, so the decision is followed by the next thing
        # the graph has to say -- another node's report, or the final result.
        # Registered before the runner wakes: resolve_node above set the node's
        # event, which only schedules the wake, and await_run parks its taker
        # before yielding.
        await_run = getattr(tool, "await_run", None)
        event = None
        if await_run is not None:
            try:
                event = await await_run(run_id)
            except asyncio.CancelledError:
                abort = getattr(tool, "abort_run", None)
                if abort is not None:
                    abort(run_id)
                raise
        if event is not None:
            return tool.render_event(run_id, event)
        if decision == CONTINUE:
            return f"Node '{node_id}' of run {run_id} will run again with your message."
        return (
            f"Node '{node_id}' of run {run_id} is abandoned; its dependents are skipped and the "
            "rest of the graph continues. Use cancel_dag to stop the whole run."
        )
```

Change the `execute` signature's return annotation to `-> "str | ToolResult"`. Update the `ResolveDagNodeTool` docstring to: `"""Answer a suspended node: continue it with a message, or abandon it. On a bound foreground run, then wait for the next report or the final result."""`. Update the `description` property to end with: `"On a run started with background=false this call also waits, and returns the next report or the run's final result."`

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_dag_control_tools.py -q -p no:randomly`
Expected: all pass.

- [ ] **Step 5: Format, lint, report**

Run: `uv run ruff format raven/agent/subagent/dag_control_tools.py tests/test_subagent_dag_control_tools.py && uv run ruff check raven/agent/subagent/dag_control_tools.py tests/test_subagent_dag_control_tools.py`

Report the task done. Commit message for when the user asks:

```
feat(agent): resolve_dag_node waits for the next report on a bound foreground run
```

---

### Task 5b: `tool_call` hands its arguments to the target's blocking verdict

**Why this task exists (a plan defect found by Task 5's review):** `resolve_dag_node` is
schema-hidden and reached only through `tool_call`. `ToolCallTool.blocking_for` forwarded
only the target's *name*, so the target's `blocking_for` ran with `{}`;
`ResolveDagNodeTool.blocking_for` then always answered False, the registry wrapped the
outer `tool_call` in its 300 s default ceiling, and a foreground wait longer than that was
cancelled by the ceiling -- which the control tool reads as the user stopping the agent,
and aborts the run.

**Files:**
- Modify: `raven/agent/tools/tool_search.py` -- `ToolSearchController.target_is_blocking` (around line 303) and `ToolCallTool.blocking_for` (around line 413)
- Test: `tests/test_tool_search.py` (append after `test_tool_call_with_an_unknown_or_meta_target_is_not_blocking`)

**Interfaces:**
- Consumes: `ToolRegistry.is_blocking(name, params)` (existing); `ResolveDagNodeTool.blocking_for(params)` from Task 5 reads `params["run_id"]`.
- Produces: `ToolSearchController.target_is_blocking(name, arguments=None) -> bool`; `ToolCallTool.blocking_for(params)` passes `params.get("arguments")` through. A JSON-string `arguments` is parsed the way `call()` parses it; anything that is not an object counts as no arguments.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tool_search.py` (the file already defines `_FakeTool(name, description)` and `_controller(reg)`; add `from typing import Any` if it is not already imported):

```python
class _ArgSensitive(_FakeTool):
    """Blocking only when asked about run r1 -- the shape ResolveDagNodeTool has."""

    def blocking_for(self, params: dict[str, Any]) -> bool:
        return params.get("run_id") == "r1"


def test_tool_call_forwards_its_arguments_to_the_target_verdict() -> None:
    """A target whose blocking verdict depends on its arguments must see them.

    `resolve_dag_node` blocks only for a bound foreground run, which it can tell
    only from `run_id`; forwarded without arguments it always said "not
    blocking", and the registry then put its default ceiling on a wait that has
    none -- cancelling it, which the tool reads as the user stopping the agent.
    """
    reg = ToolRegistry()
    reg.register(_ArgSensitive("resolve_dag_node", "answer a suspended node"))
    ctrl = _controller(reg)
    reg.register(ToolCallTool(ctrl))

    assert reg.is_blocking(TOOL_CALL_NAME, {"name": "resolve_dag_node", "arguments": {"run_id": "r1"}}) is True
    assert reg.is_blocking(TOOL_CALL_NAME, {"name": "resolve_dag_node", "arguments": {"run_id": "r2"}}) is False
    assert reg.is_blocking(TOOL_CALL_NAME, {"name": "resolve_dag_node", "arguments": '{"run_id": "r1"}'}) is True, (
        "models sometimes emit the nested arguments as a JSON string; call() tolerates it, so must the verdict"
    )
    assert reg.is_blocking(TOOL_CALL_NAME, {"name": "resolve_dag_node"}) is False
    assert reg.is_blocking(TOOL_CALL_NAME, {"name": "resolve_dag_node", "arguments": "not json"}) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_tool_search.py -k forwards_its_arguments -q -p no:randomly`
Expected: FAIL on the first assertion (`assert False is True`): the verdict never sees `run_id`.

- [ ] **Step 3: Forward the arguments**

In `raven/agent/tools/tool_search.py` (`json` is already imported there), replace `target_is_blocking` with:

```python
    def target_is_blocking(self, name: Any, arguments: Any = None) -> bool:
        """Whether the tool ``tool_call`` would forward to is a blocking interaction.

        A meta-tool target is refused by :meth:`call`, so it reports non-blocking
        here too -- which also stops the registry lookup recursing back into this
        controller.

        ``arguments`` travel with the question because a target's verdict may turn
        on them: `resolve_dag_node` blocks only for a bound foreground run, which
        it can tell only from ``run_id``. Asked without them it answered "not
        blocking" for every call, and the registry put its default ceiling on a
        wait that has none. A JSON string is tolerated the way :meth:`call`
        tolerates it; anything that is not an object counts as no arguments.
        """
        if not isinstance(name, str) or name in META_TOOL_NAMES:
            return False
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        return self._registry.is_blocking(name, arguments if isinstance(arguments, dict) else {})
```

and `ToolCallTool.blocking_for` with:

```python
    def blocking_for(self, params: dict[str, Any]) -> bool:
        return self._ctrl.target_is_blocking(params.get("name"), params.get("arguments"))
```

Then `grep -rn "target_is_blocking" raven/ tests/` -- every other caller (if any) keeps working through the default `arguments=None`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tool_search.py -k forwards_its_arguments -q -p no:randomly`
Expected: PASS.

Run: `uv run pytest tests/test_tool_search.py tests/test_subagent_dag_control_tools.py -q`
Expected: all pass, including the two pre-existing `tool_call` blocking tests.

- [ ] **Step 5: Format, lint, report**

Run: `uv run ruff format raven/agent/tools/tool_search.py tests/test_tool_search.py && uv run ruff check raven/agent/tools/tool_search.py tests/test_tool_search.py`

Report the task done. Commit message for when the user asks:

```
fix(agent): tool_call hands its arguments to the target's blocking verdict
```

---

### Task 6: Delete the person path

**Files:**
- Modify: `raven/agent/subagent/dag_runner.py` -- `run_dag` (`adjudicate` parameter and docstring), the wave loop idle block (lines 355-381), `_run_ready_groups(...)` args (`answered_in_turn=adjudicate is not None`, around line 427), `_exception_report` (lines 522-576), delete `_adjudicate_open_nodes` (lines 578-651), `_apply_verdict` (lines 653-742), `_run_group` and `_run_node` signatures and calls (`answered_in_turn`), `_run_node`'s `_apply_verdict(...)` call
- Modify: `raven/agent/subagent/dag_adjudication.py` -- `AdjudicationDesk.open(report=)`, `report()`, `_reports`
- Modify: `raven/agent/subagent/dag_tool.py` -- delete the `Adjudicate` alias and its comment (lines 92-98), the `adjudicate` parameter of `__init__` and `self._adjudicate` (lines 355-362), the `adjudicate=None,` line in `_run`'s `run_dag(...)` call
- Modify: `raven/agent/loop/wiring.py` -- remove `adjudicate=self._adjudicate_node,` at both constructions (lines 454 and 693), delete `_adjudicate_node` (lines 792-820)
- Modify: `raven/agent/subagent/manager.py` -- `adopt_background_run` docstring (lines 575-584)
- Modify: `docs/specs/2026-08-26-dag-node-verdict-design.md` -- insert a note before the line beginning `**A foreground graph is adjudicated inside its own turn.**` (line 201)
- Test: `tests/test_subagent_dag_runner.py` -- delete the ten person-path tests, rewrite one, fix the `_run_two_node_dag` helper

**Interfaces:**
- Consumes: nothing new.
- Produces: `run_dag` without `adjudicate`; `_exception_report(*, run_id, node, verdict, attempt, remaining, blocked, timeout_s)`; `AdjudicationDesk.open(node_id)` with no `report`; `SubAgentDagTool.__init__` without `adjudicate`.

- [ ] **Step 1: Write the failing test and remove the tests that pin the old shape**

In `tests/test_subagent_dag_runner.py`:

Delete the ten tests from `test_a_foreground_run_asks_a_person_and_continues_the_node` through the end of `test_a_foreground_node_with_nobody_to_ask_says_so` (up to but not including `class _StepsThenSilentExec`), together with the `# --- a foreground graph is adjudicated from the wave loop, by asking a person ---` banner above them. `test_the_in_turn_report_drops_the_deadline_and_the_tool_instruction` sits inside that span: do not delete it, replace it with:

```python
def test_the_report_has_one_shape_addressed_to_the_agent():
    """Both lanes hand the report to the main agent now, so there is one text.

    The deadline line and the resolve_dag_node line are what a person being asked
    synchronously could not use; nobody is asked that way any more.
    """
    from raven.agent.subagent.dag_runner import _exception_report
    from raven.agent.subagent.dag_verdict import Verdict

    spec = parse_dag_spec(
        {
            "task_summary": "one node",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "first", "prompt_template": "do a"}],
        }
    )
    shared = {
        "run_id": "r1",
        "node": spec.nodes[0],
        "verdict": Verdict(accomplished=False, category="missing_credential", what_is_missing="a token"),
        "attempt": 1,
        "remaining": 2,
        "blocked": ["b"],
        "timeout_s": 600.0,
    }

    report = _exception_report(**shared)

    assert "deciding within 600s" in report
    assert "restarted by each decision" in report
    assert "resolve_dag_node" in report
    for kept in ("missing_credential", "a token", "2 continuation(s) left", "blocked while this waits: b"):
        assert kept in report
    with pytest.raises(TypeError):
        _exception_report(**shared, answered_in_turn=True)
```

In `_run_two_node_dag`, delete the `adjudicate=None,` parameter and the `adjudicate=adjudicate,` argument to `run_dag`.

- [ ] **Step 2: Run the tests to verify the new one fails and the file still imports**

Run: `uv run pytest tests/test_subagent_dag_runner.py -k "one_shape" -q -p no:randomly`
Expected: FAIL at `pytest.raises(TypeError)` -- `_exception_report` still accepts `answered_in_turn`.

- [ ] **Step 3: Delete the path in the runner**

In `raven/agent/subagent/dag_runner.py`:

`run_dag` signature -- delete the line `adjudicate: "Callable[[str, str, float | None], Awaitable[str | None]] | None" = None,`. In the docstring, nothing mentions `adjudicate` by name; leave it.

Wave loop idle block -- replace

```python
                # Both do the same job from the same place -- nothing else can
                # run, every suspended node has already given its slot back --
                # and differ only in who answers: the model through the desk, or
                # the person watching a blocking call.
                if adjudicate is not None:
                    await _adjudicate_open_nodes(
                        desk,
                        status,
                        errors,
                        continuations,
                        adjudicate=adjudicate,
                        origin=origin,
                        timeout_s=adjudication_timeout_s,
                        cancel=cancel,
                    )
                else:
                    await _await_adjudications(
                        desk,
                        status,
                        errors,
                        continuations,
                        timeout_s=adjudication_timeout_s,
                        cancel=cancel,
                        released=released,
                    )
                continue
```

with

```python
                # Nothing else can run and every suspended node has already given
                # its slot back, so waiting here costs the graph nothing.
                await _await_adjudications(
                    desk,
                    status,
                    errors,
                    continuations,
                    timeout_s=adjudication_timeout_s,
                    cancel=cancel,
                    released=released,
                )
                continue
```

In the `_run_ready_groups(...)` call, delete `answered_in_turn=adjudicate is not None,`.

`_exception_report` -- delete the `answered_in_turn: bool = False,` parameter, the docstring paragraph beginning `` ``answered_in_turn`` is the foreground shape ``, and the block

```python
    if answered_in_turn:
        lines.append(f"attempt {attempt}; {remaining} continuation(s) left")
        lines.append(
            "Reply with what the node should try next, or 'abandon' to give up on it and everything waiting on it."
        )
        return "\n".join(lines)
```

Delete `_adjudicate_open_nodes` entirely (from its `async def` to the blank lines before `async def _apply_verdict`).

`_apply_verdict` -- delete the `answered_in_turn: bool = False,` parameter; replace

```python
    deliverable = announce_exception is not None if not answered_in_turn else True
```

with

```python
    deliverable = announce_exception is not None
```

; replace the comment above it (the paragraph beginning `# `origin` is part of this predicate`) with:

```python
    # `origin` is part of this predicate because whoever is going to be asked is
    # reached through it: a node suspended on a report that cannot be delivered
    # waits out its whole timeout and then blames the answerer for not answering a
    # question they never received.
```

; delete the `answered_in_turn=answered_in_turn,` argument in the `_exception_report(...)` call; replace

```python
        status[node.id] = "exception"
        # The report is parked on the desk because the foreground lane does not
        # ask here: this node returns first, releasing the dispatch slot it holds,
        # and the question is put from the wave loop once nothing else can run.
        desk.open(node.id, report)
```

with

```python
        status[node.id] = "exception"
        desk.open(node.id)
```

; and change `if announce_exception is not None and origin is not None and not answered_in_turn:` to `if announce_exception is not None and origin is not None:`.

`_run_group` and `_run_node` -- delete the `answered_in_turn: bool = False,` parameter from both signatures, the `answered_in_turn=answered_in_turn,` argument in `_run_group`'s call to `_run_node`, and the `answered_in_turn=answered_in_turn,` argument in `_run_node`'s call to `_apply_verdict`.

Then grep the file for leftovers: `grep -n "answered_in_turn\|adjudicate\b\|_adjudicate_open_nodes\|asks a person\|foreground lane" raven/agent/subagent/dag_runner.py`. Expected: no matches except the word `adjudicate` inside prose that means "decide" (none is expected; fix any hit).

- [ ] **Step 4: Trim the desk**

In `raven/agent/subagent/dag_adjudication.py`, `AdjudicationDesk`: delete `self._reports`, the `report: str = ""` parameter of `open` and its `if report:` block, the `report()` method, and the two `self._reports.pop(node_id, None)` lines in `take` and `close`. Replace `open`'s docstring with `"""Start waiting on ``node_id``. The event fires when an answer lands."""`. Run `grep -rn "desk.report(\|\.report(nid\|open(node.id, report\|_reports" raven/ tests/` and fix any hit.

- [ ] **Step 5: Remove the parameter from the tool and the wiring**

In `raven/agent/subagent/dag_tool.py`: delete the `Adjudicate` alias and the comment block above it (lines 92-98); delete `adjudicate: "Adjudicate | None" = None,` from `__init__`; delete the comment `# Only handed to a foreground run: ...` and `self._adjudicate = adjudicate`; delete `adjudicate=None,` from the `run_dag(...)` call in `_run`.

In `raven/agent/loop/wiring.py`: delete `adjudicate=self._adjudicate_node,` at both constructions, and delete the whole `_adjudicate_node` method (from `async def _adjudicate_node(` up to the line before `def _dag_guide_skill_id(`).

Run: `grep -rn "adjudicate\b\|Adjudicate\b\|_adjudicate_node" raven/ --include=*.py`. Expected: no matches.

- [ ] **Step 6: Docstrings and the superseded spec**

In `raven/agent/subagent/manager.py`, replace `adopt_background_run`'s docstring with:

```python
        """Put a task this manager did not start under the same reach as a spawn.

        Every ``run_subagent_dag`` run -- backgrounded or blocking, the latter
        runs as a task too now -- dispatches the same detached CLI children a
        spawn does, so ``/stop`` and the shutdown sweep have to find it too --
        see :meth:`cancel_all` for what an unreachable one leaves behind.
        Indexed here rather than only on the DAG tool so every entry point's
        existing teardown covers it with no extra wiring.
        """
```

In `docs/specs/2026-08-26-dag-node-verdict-design.md`, insert before the paragraph that begins `**A foreground graph is adjudicated inside its own turn.**`:

```markdown
> **Superseded 2026-09-02.** The five paragraphs that follow, up to "A host with no ask
> broker gets the plain failure path", describe a foreground lane that asked the person
> watching the call. That lane is gone: a foreground call now returns the report to the
> main agent and `resolve_dag_node` resumes the wait. See
> `docs/specs/2026-09-02-dag-foreground-report-handoff-design.md`.
```

- [ ] **Step 7: Run everything**

Run: `uv run pytest tests/test_subagent_dag_runner.py tests/test_subagent_dag_adjudication.py tests/test_subagent_dag_verdict.py tests/test_subagent_dag_core.py tests/test_subagent_dag_control_tools.py tests/test_subagent_dag_live.py tests/test_subagent_dag_machines.py tests/test_rpc_dag.py tests/test_agent_loop_run_emit.py -q`
Expected: all pass, including `test_the_report_has_one_shape_addressed_to_the_agent`.

Run: `uv run pytest tests/ -q -x --ignore=tests/integration 2>&1 | tail -5`
Expected: a printed summary with no failures attributable to these files. (Pre-existing failures on this box are listed in the session notes; compare against `origin/refactor/raven_v0_2_0` if anything unrelated is red.)

- [ ] **Step 8: Format, lint, report**

Run: `make lint-python` (or `uv run ruff format raven tests && uv run ruff check raven tests`).

Report the task done. Commit message for when the user asks:

```
refactor(agent): drop the person-asking lane of DAG adjudication
```

---

### Task 7: Verify the whole, live

**Files:** none modified.

- [ ] **Step 1: The full DAG-adjacent suite, serially**

Run: `uv run pytest tests/test_subagent_dag_*.py tests/test_rpc_dag.py tests/test_agent_loop_run_emit.py tests/test_agent_loop_session_model.py -q -p no:randomly`
Expected: all pass.

- [ ] **Step 2: A live foreground run**

Start the app the way the session notes describe (`raven web` from the worktree's environment, or `raven tui`), then in a conversation ask for a two-node graph with `background: false` whose first node is given a task it cannot complete (for example, "read the file /nonexistent/secret.txt and summarize it; if you cannot, say so"). Observe:

1. The `run_subagent_dag` tool row completes with the node's report; the DAG panel keeps updating.
2. The agent calls `resolve_dag_node`; that tool row stays open (blocking) until the graph finishes; the panel settles on the final result.
3. End a turn with a report unanswered (tell the agent "stop, just answer me"): the report arrives as a new message shortly after the turn ends, and `dag_status` shows the node still `exception`.

Record what was seen in the task report; this is the check the spec's section 10 asks for and the UI is expected to need no change.

- [ ] **Step 3: Report**

Report the results of both steps. Nothing to commit in this task.

---

## Self-review against the spec

| Spec section | Task |
|---|---|
| 1 Control flow (task in both lanes; take one event; resolve decides then waits) | 3, 5 |
| 2 The outbox (events, sync taker registration, bound/released/stop rules, sticky Final) | 1 |
| 3 Binding to the turn (`run_turn` try/except/finally, `_release_dag_runs`, direct_target guard) | 4 |
| 4 The runner (remove person path; `released` in the wait; one report shape) | 2, 6 |
| 5 The tool and the control tool (outboxes, `_run_detached`, `is_foreground`/`await_run`/`abort_run`/`release_turn`, `blocking_for`) | 3, 4, 5 |
| `tool_call` forwards arguments so `resolve_dag_node.blocking_for` sees `run_id` (plan defect found in Task 5's review) | 5b |
| 6 Model-facing text (tail, `background` description, guide skill) | 3 |
| 7 Cancellation and stop (awaiting call cancelled aborts the run; release after stop is silent) | 1, 3, 5 |
| 8 Failure modes | covered by tests in 1, 3, 4, 5 |
| 9 Testing | 1-6 |
| 10 Verification outside tests | 7 |
| Terminology (`outbox`, `bound / released` in CONTEXT.md) | 1, 4 |
| Files: manager docstring, superseded-spec note | 6 |
