"""Spine wiring for the ACP turn path: the runner, the outlet that maps each
typed spine event to its ``session/update`` frame, and the sink that ends the
turn after the render barrier.

This is where Raven-X deliberately diverges from the main repo's ACP agent
(plan §2.2): the main repo's ``UpdateTranslator`` re-parses the wire-dict
events its subscription emitter serialised one layer up, because that sink
also has to intercept three brokers and an MCP bridge. Raven-X serves none of
those on ACP, so the translation sits on the typed ``Deliverable`` /
``TurnEvent`` vocabulary directly -- a renamed field breaks at import/test
time instead of silently dropping an event, and the pieces the dict layer
drops (tool failure, Notice, MediaOut) are still visible here.

Mirrors ``raven/tui_rpc/spine.py`` (``build_tui``): same Scheduler + DeliveryHub
assembly, same render barrier (``close_stream`` then ``wait_idle`` before the
turn is declared over), same "exactly one terminating event resolves a prompt,
the second is a no-op" gate. On a cancel the sink's own TurnFailed settles the
prompt first -- ``session/cancel`` waits out the turn's unwind before its own
backstop settle (see ``AcpMethods._session_cancel``), so the second settle
finding the future done is the normal shape, not a bug.

Two invariants hold the turn together (main repo ``updates.py``, kept):

* a prompt is never answered with a JSON-RPC error -- a failed turn still ends
  with a ``stopReason``, the failure lands as message content;
* the frames for a turn are all on the wire before its prompt response.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from raven.acp import protocol
from raven.acp.loops import AcpLoops
from raven.acp.redact import redact
from raven.acp.tool_kinds import locations, title_for, tool_kind
from raven.spine import (
    Deliverable,
    MediaOut,
    Notice,
    NoticeKind,
    OriginPools,
    Reasoning,
    Scheduler,
    Text,
    ToolEvent,
    ToolPhase,
    TurnEnded,
    TurnFailed,
    TurnRequest,
    TurnStarted,
)
from raven.spine.delivery import Capabilities, DeliveryHub
from raven.spine.events import TurnEvent
from raven.spine.runner import Drain, Emit, TurnOutcome
from raven.spine.scheduler import TurnHandle

DEFAULT_USER_POOL = 1
"""Concurrent user turns a connection runs when the caller names no size.

One, because a caller that has not thought about it may still be serving every
session from a single engine, and two concurrent turns on one engine silently
clear each other's in-flight tool state (saturation, evidence round, seen sets,
retry budgets are instance attributes that ``run_turn`` resets at turn start)
-- no error, just a polluted generated distribution that ``arm_env.json``
cannot record (AGENTS.md §0.2). Anything above one therefore requires a
per-session registry, which :func:`build_acp` enforces rather than trusts.
"""

MAX_RESULT_PREVIEW = 64 * 1024
"""Backstop for a tool that did not truncate its own result preview: one
runaway result must not become a multi-megabyte frame."""


class TurnAlreadyRunningError(RuntimeError):
    """A second ``session/prompt`` arrived while one was still in flight."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} already has a prompt in flight")
        self.session_id = session_id


@dataclass
class AcpSession:
    """One live session on this connection.

    ``session_id`` is both the wire ``sessionId`` and the spine conversation id
    (``acp:<chat_id>``) -- one identity, so the sink can correlate a turn ending
    to its session with no id map.
    """

    session_id: str
    chat_id: str
    future: asyncio.Future[str] | None = None
    handle: TurnHandle | None = None


class AcpSessions:
    """The connection's session registry and the prompt-settling gate."""

    def __init__(self) -> None:
        self._sessions: dict[str, AcpSession] = {}

    def add(self, session: AcpSession) -> None:
        self._sessions[session.session_id] = session

    def get(self, session_id: str) -> AcpSession | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        """Un-register a session whose opening failed.

        Only for the rollback: a session left in the registry without an engine
        is one whose next prompt fails mid-turn, which is the failure the
        engine-on-open ordering exists to avoid.
        """
        self._sessions.pop(session_id, None)

    def sessions(self) -> tuple[AcpSession, ...]:
        return tuple(self._sessions.values())

    def begin_turn(self, session_id: str) -> asyncio.Future[str]:
        """Open the one turn slot a session has, or refuse."""
        session = self._sessions[session_id]
        if session.future is not None and not session.future.done():
            raise TurnAlreadyRunningError(session_id)
        session.future = asyncio.get_running_loop().create_future()
        return session.future

    def bind_handle(self, session_id: str, handle: TurnHandle) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.handle = handle

    def pending(self, session_id: str) -> bool:
        """True while a prompt is waiting and nothing has settled it yet."""
        session = self._sessions.get(session_id)
        return session is not None and session.future is not None and not session.future.done()

    def settle(self, session_id: str, stop: str) -> bool:
        """Resolve the pending prompt with ``stop``; False if nothing waited.

        The exactly-one gate: the second settle for a turn (the sink's
        TurnFailed followed by the cancel path's backstop is normal timing)
        finds the future already done and no-ops.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        return self.settle_future(session.future, stop)

    @staticmethod
    def settle_future(future: asyncio.Future[str] | None, stop: str) -> bool:
        """Resolve one specific prompt's future: the identity-addressed settle.

        For a caller that captured the future and then awaited something: the
        session's CURRENT slot may belong to the next prompt by the time the
        await returns, and ``settle`` would answer the wrong one.
        """
        if future is None or future.done():
            return False
        future.set_result(stop)
        return True

    def end_turn(self, session_id: str) -> None:
        """Drop the turn slot; the prompt handler calls this on its way out."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.future = None
            session.handle = None

    def close(self) -> None:
        """Settle every pending prompt as cancelled (client gone / shutdown)."""
        for session in self._sessions.values():
            if session.future is not None and not session.future.done():
                session.future.set_result("cancelled")


class AcpTurnRunner:
    """Runs the turn NON-streaming and captures the rich per-turn usage.

    Not an ``AgentTurnRunner`` subclass any more: that base pins exactly one
    engine for the runner's lifetime, and this one resolves the engine per turn
    from the session registry. ``TurnRunner`` is a structural Protocol, so
    ``run`` is the whole contract.

    ``stream=False`` is the answer-semantics decision, not a simplification.
    The consuming raven accumulates every ``agent_message_chunk`` as the
    reply, and with ``stream=True`` the token stream carries everything the
    model says out loud across the whole turn -- mid-turn narration before
    tool calls, and the DR verify/finalize pass re-stating the answer, so the
    report arrived twice (measured on a real research turn during the Phase 2
    e2e). Non-streaming, ``run_turn`` emits exactly one closing ``Text``: the
    turn's final committed reply -- the same text run.py reads out of the
    session JSONL today, on the same code path the measured product surface
    (``raven agent -m``, REPL) runs. The cost is no incremental answer or
    reasoning deltas; tool_call activity and progress notices still stream.

    ``usage_sink`` carries the context/cost numbers (``context_used`` /
    ``context_max`` / ``cost_usd``) that ``TurnOutcome.usage`` does not; the
    sink turns them into the ACP ``usage_update``. Unlike ``TuiTurnRunner``
    there is no synthetic message-tool ``tool.complete``: on ACP it would be a
    ``tool_call_update`` for a toolCallId no ``tool_call`` introduced.
    """

    def __init__(self, loops: AcpLoops, usages: dict[str, dict[str, Any]]) -> None:
        self._loops = loops
        self._usages = usages

    async def run(self, req: TurnRequest, emit: Emit, drain: Drain) -> TurnOutcome:
        cid = req.conversation or f"{req.source.channel}:{req.source.chat_id}"
        agent_loop = await self._loops.get(cid)
        usage_sink: dict[str, Any] = {}
        outcome = await agent_loop.run_turn(req, emit, drain, stream=False, usage_sink=usage_sink)
        self._usages[cid] = dict(usage_sink)
        return outcome


def _looks_failed(preview: str) -> bool:
    # The registry-wide failure convention (model-facing text of a failed tool
    # starts with "Error"), same predicate the CLI progress display applies.
    p = preview.lstrip()
    return p.startswith('{"error"') or p.startswith("Error")


class AcpOutlet:
    """The ACP send surface: each typed spine event becomes one
    ``session/update`` notification on its session's stream.

    Deliverables arrive with ``conversation_id`` == the wire ``sessionId``, so
    no lookup sits between an event and its frame. What the TUI outlet eats and
    this one does not: tool failure status (from the result-preview
    convention), progress/tool-hint notices (as thought chunks -- progress
    narration must never land in ``agent_message_chunk``, where the consuming
    raven would read it as answer text), and MediaOut (named by path).
    """

    def __init__(
        self,
        channel: str,
        emit: Callable[[dict[str, Any]], None],
        *,
        cwd_for: Callable[[str], str | None] | None = None,
    ) -> None:
        self.name = channel
        self.capabilities = Capabilities(streaming=True)
        self._emit = emit
        # Resolved per session, not per connection: each session's file tools
        # are rooted in its own subtree, so one anchor for the whole connection
        # would report every path against the wrong directory.
        self._cwd_for = cwd_for

    def update(self, session_id: str, update: dict[str, Any]) -> None:
        self._emit(protocol.notification("session/update", {"sessionId": session_id, "update": update}))

    def say(self, session_id: str, text: str) -> None:
        self.update(session_id, _text_chunk("agent_message_chunk", text))

    async def deliver(self, out: Deliverable) -> None:
        cid = out.conversation_id or ""
        if isinstance(out, Reasoning):
            if out.content:
                self.update(cid, _text_chunk("agent_thought_chunk", out.content))
        elif isinstance(out, ToolEvent):
            if out.phase is ToolPhase.START:
                self.update(cid, self._tool_call(out, cid))
            else:
                self.update(cid, self._tool_call_update(out))
        elif isinstance(out, Text):
            # A non-streamed reply (clarification gate short-circuit, empty-turn
            # fallback) becomes a message chunk like any streamed delta.
            if out.content:
                self.update(cid, _text_chunk("agent_message_chunk", out.content))
        elif isinstance(out, Notice):
            if out.kind in (NoticeKind.PROGRESS, NoticeKind.TOOL_HINT) and out.detail:
                self.update(cid, _text_chunk("agent_thought_chunk", out.detail))
            # INJECTED / DELIVERY_FAILED: internal bookkeeping, no wire shape.
        elif isinstance(out, MediaOut):
            paths = ", ".join(m.path for m in out.media)
            if paths:
                self.update(cid, _text_chunk("agent_message_chunk", f"[files: {paths}]"))

    async def send_stream_chunk(self, chat_id: str, stream_id: str, delta: str, *, done: bool = False) -> None:
        # Dormant while AcpTurnRunner runs non-streaming (see its docstring);
        # kept because the hub contract and a future streaming surface both
        # want it. done=True only lets the hub close its stream state; ACP has
        # no stream-done frame -- the prompt response is the turn's end.
        if done or not delta:
            return
        self.update(stream_id, _text_chunk("agent_message_chunk", delta))

    def _tool_call(self, ev: ToolEvent, session_id: str) -> dict[str, Any]:
        # status "in_progress", not "pending": by the time this event exists the
        # call is running, and a pending row that never changes reads as a hang.
        # rawInput is deliberately absent -- for exec it is the whole command
        # line, and this is a publishing surface; the redacted title carries
        # what a client needs to draw the row.
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call",
            "toolCallId": ev.tool_call_id,
            "title": redact(title_for(ev.name or None, ev.arguments)),
            "kind": tool_kind(ev.name or None),
            "status": "in_progress",
        }
        # The name itself, which neither of the two fields above carries:
        # ``kind`` is ten values wide and a tool this build never filed lands on
        # ``other``, while the title is written for a person to read.
        if ev.name:
            update["_meta"] = {"raven.toolName": ev.name}
        found = locations(ev.arguments, self._cwd_for(session_id) if self._cwd_for else None)
        if found:
            update["locations"] = found
        return update

    def _tool_call_update(self, ev: ToolEvent) -> dict[str, Any]:
        preview = ev.result_preview or ""
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": ev.tool_call_id,
            "status": "failed" if _looks_failed(preview) else "completed",
        }
        if preview:
            # Scanned before it is cut: cutting first can slice a credential so
            # it no longer matches the pattern that would have caught it, and
            # then the head of it is published as ordinary text.
            scanned = redact(preview)
            text = scanned[:MAX_RESULT_PREVIEW]
            if ev.truncated or len(scanned) > MAX_RESULT_PREVIEW:
                text += "\n[truncated]"
            update["content"] = [{"type": "content", "content": {"type": "text", "text": text}}]
        return update


def _text_chunk(kind: str, text: str) -> dict[str, Any]:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def _usage_update(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """The ACP ``usage_update``, only when the window numbers are both real.

    A ``size`` of zero would have a client drawing a full bar or dividing by
    it, and a turn that reported no usage has nothing to say here -- an update
    of zeroes is not the same statement as no update. USD is hardcoded because
    the figure is: ``cost_usd`` is dollars by name.
    """
    if not isinstance(usage, dict):
        return None
    used = usage.get("context_used")
    size = usage.get("context_max")
    if not isinstance(used, int) or not isinstance(size, int) or size <= 0 or used < 0:
        return None
    update: dict[str, Any] = {"sessionUpdate": "usage_update", "used": used, "size": size}
    cost = usage.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        update["cost"] = {"amount": float(cost), "currency": "USD"}
    return update


def _make_acp_sink(
    hub: DeliveryHub,
    outlet: AcpOutlet,
    sessions: AcpSessions,
    usages: dict[str, dict[str, Any]],
    channel: str,
    loops: AcpLoops,
) -> Callable[[TurnEvent], Awaitable[None]]:
    """The scheduler's EventSink: deliverables route through the hub, and a
    turn's ending settles its session's prompt after the render barrier, so
    every frame belonging to the turn is on the wire before its stopReason."""

    async def _finish(conversation_id: str) -> None:
        await hub.close_stream(conversation_id)
        await hub.wait_idle(channel)

    def _settle(conversation_id: str, stop: str) -> None:
        sessions.settle(conversation_id, stop)
        # A settled session is idle, and idle is the only state the engine cap
        # can evict from. This is the one moment the registry can learn that a
        # turn it had to keep an over-cap engine for is over.
        loops.reclaim()

    async def sink(event: TurnEvent) -> None:
        if isinstance(event, TurnEnded):
            cid = event.conversation_id or ""
            await _finish(cid)
            usage = usages.pop(cid, None)
            if sessions.get(cid) is None:
                # A turn on a lane this connection does not own (a subagent
                # result re-injection); its deliverables were already routed.
                return
            update = _usage_update(usage)
            if update is not None:
                outlet.update(cid, update)
            _settle(cid, "end_turn")
            return
        if isinstance(event, TurnFailed):
            cid = event.conversation_id or ""
            await _finish(cid)
            usages.pop(cid, None)
            if sessions.get(cid) is None:
                return
            if event.cancelled:
                _settle(cid, "cancelled")
                return
            # The failure is content, never a JSON-RPC error: an error in reply
            # to a turn-shaped request makes clients tear down the whole turn.
            # It also keeps the empty-turn invariant -- the consuming raven
            # treats a turn with no message chunk as AcpEmptyTurnError. Said
            # only while the prompt is still unsettled: after a cancel already
            # answered it, this text would land on a turn that is over.
            if sessions.pending(cid):
                outlet.say(cid, redact(f"The turn failed: {event.error or 'internal error'}"))
            _settle(cid, "end_turn")
            return
        if isinstance(event, TurnStarted):
            # The session/prompt request is itself the record that a turn began.
            return
        await hub.dispatch(event)

    return sink


def build_acp(
    loops: AcpLoops,
    emit: Callable[[dict[str, Any]], None],
    *,
    channel: str = "acp",
    user_pool: int = DEFAULT_USER_POOL,
) -> tuple[Scheduler, DeliveryHub, AcpSessions, Callable[[], Awaitable[None]]]:
    """Wire the spine pieces an ACP turn flows through.

    Returns the scheduler (``session/prompt`` submits to it), the hub, the
    session registry, and a ``teardown`` the server awaits on exit. ``emit``
    writes one finished frame; ``loops`` resolves each turn's engine and anchors
    that session's relative tool-call paths for ``locations``.
    """
    if user_pool > 1 and not loops.per_session:
        # Fail at startup rather than serve turns that quietly corrupt each
        # other: a pool above one on a single shared engine is the one
        # misconfiguration that produces no error and no record of itself.
        raise ValueError(
            f"user_pool={user_pool} needs one engine per session; "
            "this registry serves a single shared engine, on which concurrent "
            "turns would clear each other's in-flight tool state"
        )
    hub = DeliveryHub()
    outlet = AcpOutlet(channel, emit, cwd_for=loops.cwd_for)
    hub.register(outlet)
    sessions = AcpSessions()
    # The cap may not evict an engine whose session is mid-turn; only the
    # session registry knows which those are.
    loops.set_busy(sessions.pending)
    usages: dict[str, dict[str, Any]] = {}
    scheduler = Scheduler(
        AcpTurnRunner(loops, usages),
        OriginPools(user=user_pool, system=1),
        _make_acp_sink(hub, outlet, sessions, usages, channel, loops),
    )

    async def teardown() -> None:
        await scheduler.shutdown(grace=0.0)
        await hub.aclose()
        # Last, and only after the scheduler has stopped: closing an engine
        # drains its memory writes, and a turn still running would be adding to
        # them.
        await loops.aclose()

    return scheduler, hub, sessions, teardown


__all__ = [
    "DEFAULT_USER_POOL",
    "AcpOutlet",
    "AcpSession",
    "AcpSessions",
    "AcpTurnRunner",
    "TurnAlreadyRunningError",
    "build_acp",
]
