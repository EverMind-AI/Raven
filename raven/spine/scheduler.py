"""Lane and worker: the per-conversation serial+cancel execution domain.

A lane runs one turn at a time and is the unit of cancellation. The worker owns
the lifecycle events and is the sole resolver of a turn's terminal future. The
pool and event sink are placeholders filled in by later sub-steps.
"""

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import get_args

from loguru import logger

from raven.spine.events import RunnerEvent, TurnEnded, TurnEvent, TurnFailed, TurnStarted
from raven.spine.runner import Emit, TurnOutcome, TurnRunner
from raven.spine.turn import BusyPolicy, Origin, TurnRequest

EventSink = Callable[[TurnEvent], Awaitable[None]]

# Tuple form (not the union) so a type checker can't flag the guard below as
# unreachable and invite deleting it — it is the only lifecycle-emit enforcement.
_RUNNER_EVENT_TYPES = get_args(RunnerEvent)

# Proactive origins share the system pool. SUBAGENT here is the result-reinjection
# turn; a subagent's own execution runs off the scheduler behind a separate gate.
_SYSTEM_ORIGINS = (Origin.SENTINEL, Origin.CRON, Origin.HEARTBEAT, Origin.SUBAGENT)

_DEFAULT_IDLE_TTL = 300.0  # seconds a lane may sit idle before the reaper reclaims it
_SWEEP_INTERVAL = 60.0  # seconds between reaper sweeps
_DEPTH_WARN_THRESHOLD = 50  # warn once when a lane's pending queue reaches this depth


class SchedulerDrainingError(Exception):
    """Raised by submit once shutdown has sealed the scheduler — new turns are
    not accepted while draining.
    """


class OriginPools:
    """Per-origin concurrency gates: a USER pool and a system pool for proactive
    origins, sized independently. No global cap (total concurrency is their sum)
    and no borrowing between them, so a user turn never waits on an LLM slot
    behind a proactive task.
    """

    def __init__(self, user: int, system: int):
        self._user = asyncio.Semaphore(user)
        self._system = asyncio.Semaphore(system)

    def for_origin(self, origin: Origin) -> asyncio.Semaphore:
        if origin is Origin.USER:
            return self._user
        if origin in _SYSTEM_ORIGINS:
            return self._system
        # fail-loud: a new origin must consciously choose a pool, not be
        # silently funnelled into the system pool by a fallback.
        raise ValueError(f"no pool mapping for origin {origin!r}")


class Lane:
    def __init__(
        self, runner: TurnRunner, pools: OriginPools, sink: EventSink, conversation_id: str
    ):
        self._runner = runner
        self._pools = pools
        self._sink = sink
        self._conversation_id = conversation_id
        self._pending: deque[tuple[TurnRequest, asyncio.Future]] = deque()
        self._worker: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._running_fut: asyncio.Future | None = None
        self._idle_since: float | None = None  # set when the worker drains; the reaper's clock
        # Injects submitted while a turn runs, waiting to be drained (merged) at a
        # tool-loop gap. Once drained, an inject is chained to the running turn
        # inside _run_turn (a per-turn local), not held here.
        self._inject_mailbox: deque[tuple[TurnRequest, asyncio.Future]] = deque()

    def submit(self, req: TurnRequest, policy: BusyPolicy = BusyPolicy.APPEND) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._idle_since = None  # active again: reset the reaper's silence clock
        running = self._run_task is not None and not self._run_task.done()
        if policy is BusyPolicy.INTERRUPT and running:
            # Preempt: cancel the running turn (only its task — the worker stays
            # the sole resolver of its future) and jump the interrupter to the
            # front, ahead of any APPEND backlog.
            self._run_task.cancel()
            self._enqueue(req, fut, front=True)
        elif policy is BusyPolicy.INJECT and running:
            # Destined for the running turn: hold it for the runner to drain at a
            # tool-loop gap. If the turn ends without draining it, the worker
            # falls it back to an APPEND turn. The running turn's worker is live,
            # so no (re)start is needed.
            self._inject_mailbox.append((req, fut))
            return fut
        else:
            # APPEND, or INTERRUPT/INJECT on an idle lane (no turn to act on): just
            # queue and run like a normal turn.
            self._enqueue(req, fut)
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._run_worker())
        return fut

    def _enqueue(self, req: TurnRequest, fut: asyncio.Future, *, front: bool = False) -> None:
        # The single entry for every _pending growth — APPEND (tail), INTERRUPT
        # (front), and the worker's inject fallback — so the depth check sees each
        # +1 and warns exactly once per upward crossing of the threshold (no flag
        # needed). All _pending growth must route through here or the == check can
        # be skipped.
        if front:
            self._pending.appendleft((req, fut))
        else:
            self._pending.append((req, fut))
        if len(self._pending) == _DEPTH_WARN_THRESHOLD:
            logger.warning(
                "lane {} pending queue reached depth {}",
                self._conversation_id,
                _DEPTH_WARN_THRESHOLD,
            )

    def cancel_turn(self, fut: asyncio.Future) -> None:
        """Cancel one turn (its handle's): drop it if queued, cancel its task if
        running. Idempotent on an already-resolved future. Never resolves the
        running turn's future — the worker stays its sole resolver.
        """
        if fut.done():
            return
        for i, (_req, queued) in enumerate(self._pending):
            if queued is fut:
                del self._pending[i]
                fut.set_result(None)
                return
        for i, (_req, pending_inject) in enumerate(self._inject_mailbox):
            if pending_inject is fut:
                # Still in the mailbox (not yet merged): the inject can cancel
                # itself. Once drained/merged it is no longer here, so cancelling
                # its handle is a no-op — it cannot kill the host turn.
                del self._inject_mailbox[i]
                fut.set_result(None)
                return
        if fut is self._running_fut and self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()

    def cancel_running(self) -> int:
        """Cancel the running turn's task — never its future (the worker stays the
        sole resolver) — returning 1 if there was one to cancel, else 0.
        """
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
            return 1
        return 0

    def running_future(self) -> asyncio.Future | None:
        """The in-flight turn's terminal future, or None if the lane is idle.
        Shutdown awaits these within its grace window.
        """
        return self._running_fut

    def drain_pending(self) -> int:
        """Resolve every not-yet-running turn (queued + mailboxed injects) as
        cancelled and return the count, leaving the running turn untouched.

        Already-drained injects are chained to the running turn and follow its
        fate inside _run_turn — not here, so each future resolves exactly once.
        """
        stopped = 0
        while self._pending:
            _req, fut = self._pending.popleft()
            fut.set_result(None)  # a queued turn never ran: resolve as cancelled
            stopped += 1
        while self._inject_mailbox:
            _req, fut = self._inject_mailbox.popleft()
            fut.set_result(None)  # undrained inject: resolve cancelled, no revival
            stopped += 1
        return stopped

    def cancel(self) -> int:
        """/stop: cancel the running turn and drain the queue, resolving every
        waiting future as cancelled; return how many turns were stopped.
        """
        return self.cancel_running() + self.drain_pending()

    def idle_for(self, now: float) -> float | None:
        """Seconds since the worker drained and the lane went idle, or None while
        it is still active. The reaper reads this to reclaim long-silent lanes.
        """
        if self._idle_since is None:
            return None
        return now - self._idle_since

    async def _run_worker(self) -> None:
        while self._pending:
            req, fut = self._pending.popleft()
            self._running_fut = fut
            # Create the task synchronously (no await before this) so the turn is
            # cancel-visible via _run_task the moment it leaves the queue.
            self._run_task = asyncio.create_task(self._run_turn(req))
            outcome: TurnOutcome | None = None
            try:
                outcome = await self._run_task  # None on cancel/failure, outcome on success
            except asyncio.CancelledError:
                if not self._run_task.cancelled():
                    # The worker itself was cancelled (process shutdown): cascade
                    # to the payload and await its cleanup — its finally emits
                    # TurnFailed and resolves any chained inject — so it leaves no
                    # zombie, then re-raise. (Normal shutdown cancels payloads, not
                    # workers; this path is the hard-kill case, a single cancel.)
                    self._run_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._run_task
                    raise
                # payload cancelled: the turn emitted its own terminal
            finally:
                # Sole resolver: resolve on every exit, including a cancel that
                # lands before the task body (and its handlers) ever runs. (A
                # merged inject is resolved inside _run_turn, not here.)
                fut.set_result(outcome)
                # An inject the turn never drained falls back to a fresh APPEND
                # turn (no message lost); USER injects log the fallback so a
                # silent "why didn't my inject inject" is debuggable.
                while self._inject_mailbox:
                    inject_req, inject_fut = self._inject_mailbox.popleft()
                    if inject_req.origin is Origin.USER:
                        logger.info(
                            "inject fell back to append (not merged): origin={}",
                            inject_req.origin,
                        )
                    self._enqueue(inject_req, inject_fut)
                self._run_task = None
                self._running_fut = None
        # Idle exit: stamp the reaper's silence clock, with no await between the
        # queue check and return — a submit racing the exit must not be lost
        # (lost-wakeup), and the same gap keeps the stamp atomic against submit
        # clearing it.
        self._idle_since = time.monotonic()

    def _make_emit(self, req: TurnRequest) -> Emit:
        async def emit(event: RunnerEvent) -> None:
            if not isinstance(event, _RUNNER_EVENT_TYPES):
                raise TypeError(
                    f"a runner may not emit lifecycle events; got {type(event).__name__}"
                )
            # Stamp the identity the runner leaves unset (only-None, never override):
            # source = the turn's reply address, conversation_id = its lane key (what
            # the hub correlates a stream by).
            if event.source is None:
                event = replace(event, source=req.source)
            if event.conversation_id is None:
                event = replace(event, conversation_id=self._conversation_id)
            await self._sink(event)

        return emit

    async def _run_turn(self, req: TurnRequest) -> TurnOutcome | None:
        chained: list[asyncio.Future] = []

        def drain() -> list[TurnRequest]:
            # Read-and-remove the pending injects, chaining each to this turn.
            # Removing them from the mailbox is what keeps a /stop from also
            # resolving them — each future then has a single resolver.
            drained = list(self._inject_mailbox)
            self._inject_mailbox.clear()
            chained.extend(fut for _req, fut in drained)
            return [req for req, _fut in drained]

        outcome: TurnOutcome | None = None
        started = False
        try:
            async with self._pools.for_origin(req.origin):
                await self._sink(TurnStarted(conversation_id=self._conversation_id))
                started = True
                run_start = time.monotonic()
                outcome = await self._runner.run(req, self._make_emit(req), drain)
        except asyncio.CancelledError:
            if started:  # only pair a TurnStarted; a pre-start cancel emits nothing
                await self._sink(
                    TurnFailed(error="cancelled", cancelled=True, conversation_id=self._conversation_id)
                )
            raise
        except Exception as exc:
            if started:
                await self._sink(
                    TurnFailed(error=str(exc), cancelled=False, conversation_id=self._conversation_id)
                )
            return None
        finally:
            # A drained inject shares this turn's outcome (None on cancel/failure);
            # resolved here, in the turn that merged it, not by the worker.
            for inject_fut in chained:
                inject_fut.set_result(outcome)
        latency_ms = (time.monotonic() - run_start) * 1000
        await self._sink(
            TurnEnded(
                usage=outcome.usage,
                latency_ms=latency_ms,
                explicit_reply=outcome.explicit_reply,
                conversation_id=self._conversation_id,
            )
        )
        return outcome


class TurnHandle:
    """Returned by submit; the caller's view of one turn."""

    def __init__(self, lane: Lane, fut: asyncio.Future):
        self._lane = lane
        self._fut = fut

    async def result(self) -> TurnOutcome | None:
        return await self._fut

    def cancel(self) -> None:
        self._lane.cancel_turn(self._fut)


class Scheduler:
    """The single entry: submit a request, get a handle. Routes each request to
    its conversation lane (created on demand), which serialises and gates it.
    """

    def __init__(self, runner: TurnRunner, pools: OriginPools, sink: EventSink):
        self._loop = asyncio.get_running_loop()  # home loop; submit must come from here
        self._runner = runner
        self._pools = pools
        self._sink = sink
        self._lanes: dict[str, Lane] = {}
        self._draining = False
        self._reaper: asyncio.Task | None = None

    def submit(self, req: TurnRequest) -> TurnHandle:
        if asyncio.get_running_loop() is not self._loop:
            # Off-loop (e.g. a channel's ws thread running its own loop) would
            # build the lane on the wrong loop — fail loud, don't bridge silently.
            raise RuntimeError("submit must be called from the scheduler's event loop")
        if self._draining:
            logger.info("submit rejected: scheduler draining (origin={})", req.origin)
            raise SchedulerDrainingError("scheduler is draining; new turns are not accepted")
        policy = self._effective_busy(req)
        conversation_id = self._conversation_id(req)
        lane = self._lanes.get(conversation_id)
        if lane is None:
            lane = Lane(self._runner, self._pools, self._sink, conversation_id)
            self._lanes[conversation_id] = lane
        handle = TurnHandle(lane, lane.submit(req, policy))
        # Lazy-start the reaper alongside the first lane (same pattern as the
        # lane worker); it self-terminates when no lanes remain.
        if self._reaper is None or self._reaper.done():
            self._reaper = self._loop.create_task(self._reap_loop())
        return handle

    def cancel_conversation(self, conversation_id: str) -> int:
        """/stop: cancel the running turn and drain the queue for a conversation's
        lane, returning how many turns were stopped (0 if no such lane exists).
        The spine-native equivalent of the bus drainer's per-session _handle_stop.
        """
        lane = self._lanes.get(conversation_id)
        return lane.cancel() if lane is not None else 0

    def has_inflight(self, conversation_id: str) -> bool:
        """True if a turn is currently running for this conversation's lane.
        The inbound gate uses this to submit a mid-turn message as
        BusyPolicy.INJECT instead of queuing a fresh turn.
        """
        lane = self._lanes.get(conversation_id)
        return lane is not None and lane.running_future() is not None

    async def _reap_loop(self) -> None:
        # Self-terminating, like the lane worker: runs while there are lanes to
        # reclaim and exits when none remain; the next submit restarts it.
        while self._lanes:
            await asyncio.sleep(_SWEEP_INTERVAL)
            self._sweep(time.monotonic())

    async def shutdown(self, grace: float) -> None:
        """Drain the scheduler: seal it, cancel unstarted work, let running turns
        finish within grace, then cascade-cancel the stragglers. Every turn's
        future resolves on one of the four exit paths — shutdown resolves the
        unfinished ones as cancelled, so result() never hangs.
        """
        self._draining = True  # phase 1: seal — submit now fails fast
        for lane in self._lanes.values():
            lane.drain_pending()  # phase 2: clear queued + mailboxed work
        # Seal + drain run with no await between them, so unstarted work is
        # resolved atomically before the grace window lets a running turn finish
        # and fall its mailbox back to a fresh turn.
        if self._reaper is not None and not self._reaper.done():
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper  # await the cancellation so shutdown leaves it truly done
        running = [f for lane in self._lanes.values() if (f := lane.running_future()) is not None]
        if running:  # phase 3: grace window for in-flight turns
            await asyncio.wait(running, timeout=grace)
        for lane in self._lanes.values():  # phase 4: cascade-cancel the stragglers
            lane.cancel_running()
        survivors = [fut for fut in running if not fut.done()]
        if survivors:
            await asyncio.wait(survivors)

    def _effective_busy(self, req: TurnRequest) -> BusyPolicy:
        """Resolve the busy policy actually applied. INJECT and INTERRUPT are
        USER-only — a proactive origin must not interrupt or inject into a user's
        turn — so a non-USER request asking for either is demoted to APPEND. The
        demotion is logged with the requested and applied policy so "I asked for
        INTERRUPT, why nothing happened" is debuggable.
        """
        if req.busy is BusyPolicy.APPEND or req.origin is Origin.USER:
            return req.busy
        logger.info(
            "busy policy demoted: origin={} requested={} applied={}",
            req.origin,
            req.busy,
            BusyPolicy.APPEND,
        )
        return BusyPolicy.APPEND

    def _conversation_id(self, req: TurnRequest) -> str:
        if req.conversation is not None:
            return req.conversation
        # The scheduler is channel-agnostic: a channel that keys by a thread or
        # topic (a sub-conversation within a chat) formats that key itself and
        # passes it as the explicit conversation above. Here we only derive the
        # neutral default, knowing nothing channel-specific.
        return f"{req.source.channel}:{req.source.chat_id}"

    def _sweep(self, now: float) -> int:
        """Reap lanes idle (worker drained and gone) past _DEFAULT_IDLE_TTL; return
        how many were dropped. Synchronous and await-free, so it cannot interleave
        with the equally synchronous submit: a request can never vanish into a lane
        being reaped — submit runs either wholly before (lane active, skipped) or
        wholly after (a fresh lane is built). The atomicity is structural, not locked.
        """
        reaped = 0
        for conversation_id, lane in list(self._lanes.items()):
            idle = lane.idle_for(now)
            if idle is not None and idle >= _DEFAULT_IDLE_TTL:
                del self._lanes[conversation_id]
                reaped += 1
        return reaped
