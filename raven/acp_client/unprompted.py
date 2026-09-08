"""Show and record what an ACP agent does between prompts.

Every other route into an instance's conversation is opened by raven asking for
something: a spawn, a graph node, a message typed into a direct chat. Each
attaches a sink for the run it started and detaches it at the end, which reads
the stream as request/response -- a session is spoken about only while raven is
waiting to be answered.

An on-call agent does not work that way. It arms a wake, the turn that armed it
ends, and later it wakes on its own schedule, does a round of work and says what
it found, with nobody having asked. Those updates carry a session id raven knows
and arrive with no sink attached, so ``_SessionRouter`` dropped them as a late
usage report -- measured 2026-08-26 as 35 discarded frames across one campaign.

Recording the turn in the instance log turned out to be half the fix. The pane
reads that log only on entry (and keeps its cache), and every refresh trigger it
has rides the wire events of a turn somebody started -- so a round written by
this process alone stayed invisible until the next fresh entry, measured twice
on live wakes after the log write was already correct.

So the other half is the wire. The client already renders a turn it did not
start: ``message.start`` / ``token.delta`` / ``message.complete`` stamped with a
``target`` are exactly how a typed direct-chat turn reaches its pane --
markRunning, live append, then a settled re-read of the record. This recorder
emits those three for an unprompted turn, on the session the instance registry
says the handle belongs to, and the client needs no change at all: it cannot
tell this turn from one it asked for, which is the point.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

#: A frame that ends an unprompted turn when it does arrive. ``message.complete``
#: translates to a usage update and a stop reason, and only the update reaches
#: the wire -- but the usage half is conditional (``updates=() if usage is None``),
#: so a turn that reports none ends with no frame at all. Measured 2026-08-26: a
#: wake round arrived complete, was buffered, and was never written while this
#: was the only thing being waited for.
_END = "usage_update"

#: So the fallback end is silence. Sized against the longest gap a healthy turn
#: produces, which is a tool call: an ``exec`` measured at 8s emits nothing
#: between its call frame and its result frame, and the 5s this started at read
#: that silence as the end of the turn -- one wake round landed as six log rows.
#: The cost of the margin is only that the settled re-read runs late; the text
#: itself streams out as it arrives.
_IDLE_S = 20.0

_SAY = "agent_message_chunk"
_THINK = "agent_thought_chunk"

_TURN_SEQ = itertools.count(1)

#: Emits one event to one conversation's subscribers, fire-and-forget.
#: ``SubagentManager._emit_event`` is the production value.
EventSink = Callable[[str, dict[str, Any]], None]


@dataclass
class _Pending:
    """One unprompted turn, as it arrives."""

    said: list[str] = field(default_factory=list)
    thought: list[str] = field(default_factory=list)
    #: One entry per distinct `toolCallId`, not one per frame. ACP sends
    #: `tool_call` to open a call and `tool_call_update` to carry its status and
    #: its result, against the same id -- the measured adapter stub emits one
    #: open and two updates for a single call. Counting frames therefore reported
    #: one call as two or three, in the very row this turn writes to be truthful
    #: about what it did. An id-less frame falls back to its own identity so a
    #: dialect that omits the field still counts one per opening frame.
    tool_ids: set[str] = field(default_factory=set)
    #: Set once the wire has been told this turn exists (message.start), which
    #: is also the promise that a message.complete will follow.
    turn_id: str | None = None

    @property
    def tools(self) -> int:
        return len(self.tool_ids)

    @property
    def empty(self) -> bool:
        return not self.said and not self.thought and not self.tool_ids

    def answer(self) -> str:
        """What to record as this turn's reply. Never empty for a turn that ran.

        A wake often does its whole job through tools and says nothing -- looks
        at a ledger, sees the run is still going, arms the next look. Measured
        over one 18-minute campaign: five of its eight unprompted turns produced
        no `agent_message_chunk` at all.

        Those cannot fall through to no reply. `build_turn` writes the assistant
        row only for a non-None answer, so an empty one leaves the `user`
        boundary row standing alone -- and a lone boundary does the very damage
        the boundary was added to prevent, one turn further on: the next turn's
        reply is drawn as the answer to *this* turn's marker, and the log reads
        as two questions with one answer between them.

        So a turn with no words is recorded as the fact that it had none, which
        is also the true account of what the operator would have seen.
        """
        said = "".join(self.said).strip()
        if said:
            return said
        calls = f"{self.tools} tool call{'' if self.tools == 1 else 's'}"
        return f"[unprompted: {calls}, no message]" if self.tools else "[unprompted: no message]"


class UnpromptedRecorder:
    """Collects a session's unprompted updates; shows and logs each turn.

    One per connection, holding one buffer per session. Buffers are small (the
    text of a turn) and end with the turn, so an agent that never acts unprompted
    costs an empty dict.
    """

    def __init__(
        self,
        agent: str,
        registry: Any,
        session_dir_for: Any,
        emit: EventSink | None = None,
        announce: Callable[[str, str, str], Any] | None = None,
    ) -> None:
        self._agent = agent
        self._registry = registry
        # Given rather than resolved here: which directory an instance's log
        # lives in is the manager's answer, and duplicating the rule is how two
        # copies of it drift.
        self._session_dir_for = session_dir_for
        self._emit_sink = emit
        # ``(session_key, handle, text) -> awaitable | None``: wake the
        # conversation that owns the instance with what it said. Only turns
        # that said words go through -- a wake round that just looked at a
        # ledger (five of eight turns in the measured campaign) is a heartbeat,
        # not a report, and each wake costs a full main-agent turn. Without
        # this half, a report of finished work reaches only the log: measured
        # 2026-09-01, a watch instance announced finished GPU runs five times
        # while the main agent slept eleven hours beside them.
        self._wake_cb = announce
        self._pending: dict[str, _Pending] = {}
        self._idle: dict[str, asyncio.Task] = {}
        # session id -> (session_key, handle), resolved once per turn rather than
        # per frame: the registry is a file, and a streaming turn is many frames.
        self._placed: dict[str, tuple[str, str]] = {}

    async def __call__(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        session_id = params.get("sessionId")
        update = params.get("update")
        if not isinstance(session_id, str) or not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        buf = self._pending.setdefault(session_id, _Pending())
        if buf.turn_id is None:
            await self._announce(session_id, buf)
        if kind == _SAY:
            text = _text_of(update)
            if text:
                buf.said.append(text)
                self._send(session_id, "token.delta", {"text": text})
        elif kind == _THINK:
            buf.thought.append(_text_of(update))
        elif kind in ("tool_call", "tool_call_update"):
            call_id = update.get("toolCallId")
            # The update's own position in the stream when the agent named no id:
            # two updates of one unnamed call still count twice there, which is
            # the best a frame with nothing to join on can do, and it is why the
            # opening frame is the one worth having an id.
            buf.tool_ids.add(str(call_id) if call_id else f"anon:{len(buf.tool_ids)}")
        elif kind == _END:
            self._cancel_idle(session_id)
            self._pending.pop(session_id, None)
            await self._finish(session_id, buf)
            return
        self._restart_idle(session_id)

    async def flush(self, session_id: str) -> None:
        """Write whatever is buffered for this session, now.

        Called when a run of raven's own takes the session over: whatever the
        agent was saying unprompted is finished as far as this recorder is
        concerned, and leaving it buffered would splice it onto the next
        unprompted turn -- two rounds in one row, out of order with the prompted
        one between them.
        """
        self._cancel_idle(session_id)
        buf = self._pending.pop(session_id, None)
        if buf is not None:
            await self._finish(session_id, buf)

    # ---- lifecycle ----

    async def _announce(self, session_id: str, buf: _Pending) -> None:
        """Tell the wire this turn exists, before any of its content.

        ``message.start`` with a target is what makes the client mark the
        instance as replying and start its live read -- the same first event a
        typed direct-chat turn produces. Emitted before the first delta so no
        content outruns the state that renders it.
        """
        buf.turn_id = f"unprompted:{next(_TURN_SEQ)}"
        placed = await self._locate(session_id)
        if placed is None:
            return
        self._placed[session_id] = placed
        await self._mark_row(placed[0], placed[1], "running")
        self._send(session_id, "message.start", {"turn_id": buf.turn_id})

    async def _finish(self, session_id: str, buf: _Pending) -> None:
        placed = self._placed.pop(session_id, None) or await self._locate(session_id)
        if placed is None:
            if not buf.empty:
                logger.info(
                    "acp agent {!r}: unprompted turn on session {!r} belongs to no instance this "
                    "session knows; not recorded",
                    self._agent,
                    session_id,
                )
            return
        session_key, handle = placed
        if not buf.empty:
            self._write(session_key, handle, buf)
            await self._wake(session_key, handle, buf)
        await self._mark_row(session_key, handle, "completed")
        if buf.turn_id is not None:
            # The promise message.start made. Without it the pane keeps a
            # replying dot forever and never runs the settled re-read that
            # replaces the live snapshot with the record.
            self._send_to(session_key, handle, "message.complete", {"turn_id": buf.turn_id})

    def rebind(self, *, emit: EventSink | None, announce: Callable[[str, str, str], Any] | None) -> None:
        """Point this resident recorder at the backend that now owns the connection.

        The recorder lives as long as the pooled connection, which is the
        process; the backend and the manager behind it live one generation. A
        runtime swap builds generation N+1 and reuses the connection, so the
        sinks captured at construction would keep routing wakes to a manager
        whose scheduler has been drained -- the wake raises, is logged, and the
        owner sleeps. Replacing them is how the surviving transport is rewired.
        """
        self._emit_sink = emit
        self._wake_cb = announce

    async def _wake(self, session_key: str, handle: str, buf: _Pending) -> None:
        """Route a turn that said words back into its owning conversation.

        Gated on ``said`` rather than on ``empty``: a tool-only wake round is a
        heartbeat and stays in the log, while words are the agent reporting
        something to somebody -- and between prompts the only somebody is the
        conversation that owns the instance. Failure costs the wake, never the
        record: the log write has already happened.
        """
        if self._wake_cb is None or not any(s.strip() for s in buf.said):
            return
        try:
            ret = self._wake_cb(session_key, handle, buf.answer())
            if ret is not None:
                await ret
        except Exception as exc:  # noqa: BLE001 - a lost wake must not cost the record
            logger.warning("acp agent {!r}: could not announce unprompted turn: {}", self._agent, exc)

    # ---- the two outputs ----

    def _write(self, session_key: str, handle: str, buf: _Pending) -> None:
        """Append the finished turn, or say why it could not be placed.

        Failing silently here would be the same defect this whole module exists
        to fix, one level up: work that happened with no trace of it anywhere.
        """
        if self._session_dir_for is None:
            # Guessing the directory would be worse -- the manager's rule groups
            # a session by project, and a guess writes a real transcript
            # somewhere nobody reads it.
            logger.warning(
                "acp agent {!r}: an unprompted turn on {}/{} was not recorded: no session "
                "directory was bound (the dispatching manager did not hand one over)",
                self._agent,
                self._agent,
                handle,
            )
            return
        try:
            from raven.agent.subagent.instance_log import append_turn

            append_turn(
                Path(self._session_dir_for(session_key)),
                agent=self._agent,
                handle=handle,
                session_key=session_key,
                # Its own kind, beside `spawn` and `dag`: a reader has to be able
                # to tell a round the agent decided to run from one somebody
                # asked for, and the two read identically otherwise.
                kind="unprompted",
                title="",
                # A marker, not None. The conversation folder starts a message at
                # a ``user`` row, so a turn without one does not merely omit its
                # question -- the damage lands on the neighbours: this turn merges
                # into the previous one on a fresh read, and the next typed turn
                # is drawn as a continuation of this. The DAG lane shipped exactly
                # that hole and was fixed by passing the task; the wake's own
                # message never reaches this process, so the boundary is a stated
                # fact rather than the text.
                prompt="[unprompted: the agent acted on its own schedule]",
                messages=None,
                answer=buf.answer(),
            )
            logger.info("acp agent {!r}: recorded an unprompted turn on {}/{}", self._agent, self._agent, handle)
        except Exception as exc:  # noqa: BLE001 - an audit trail must not take down the run it describes
            logger.warning("acp agent {!r}: could not record unprompted turn: {}", self._agent, exc)

    def _send(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        placed = self._placed.get(session_id)
        if placed is not None:
            self._send_to(placed[0], placed[1], kind, payload)

    def _send_to(self, session_key: str, handle: str, kind: str, payload: dict[str, Any]) -> None:
        """One wire event, stamped the way ``turn.send`` stamps a direct turn's.

        The ``target`` is what routes it: the client demultiplexes these four
        event types on that field, into the pane of the instance it names.
        """
        if self._emit_sink is None:
            return
        try:
            self._emit_sink(
                session_key,
                {"type": kind, "payload": {**payload, "target": {"agent": self._agent, "handle": handle}}},
            )
        except Exception as exc:  # noqa: BLE001 - a lost frame costs a render, never the turn
            logger.warning("acp agent {!r}: could not emit {}: {}", self._agent, kind, exc)

    # ---- plumbing ----

    def _cancel_idle(self, session_id: str) -> None:
        task = self._idle.pop(session_id, None)
        if task is not None:
            task.cancel()

    def _restart_idle(self, session_id: str) -> None:
        self._cancel_idle(session_id)

        async def _later() -> None:
            try:
                await asyncio.sleep(_IDLE_S)
            except asyncio.CancelledError:
                return
            self._idle.pop(session_id, None)
            buf = self._pending.pop(session_id, None)
            if buf is not None:
                await self._finish(session_id, buf)

        self._idle[session_id] = asyncio.ensure_future(_later())

    async def _mark_row(self, session_key: str, handle: str, status: str) -> None:
        """Best-effort, exactly as a spawn's own status write is.

        The registry row is what the instance strip and a freshly entered pane
        read; the wire events above are what a pane already open reacts to. Both,
        because either alone leaves one of those two views stale.
        """
        try:
            await self._registry.upsert_spawn(session_key, self._agent, handle, status)
        except Exception as exc:  # noqa: BLE001 - a status row must never fail a turn
            logger.warning("acp agent {!r}: status write failed ({}): {}", self._agent, status, exc)

    async def _locate(self, session_id: str) -> tuple[str, str] | None:
        """Which ``(session_key, handle)`` this ACP session id was bound to.

        Read from the instance registry rather than kept alongside it: the
        binding is written when a run first resolves a handle to a session, it
        outlives every individual run, and a second copy here would be one more
        thing to keep in step with an unbind.
        """
        try:
            records = self._registry._load()  # noqa: SLF001 - no public reverse lookup exists
        except Exception as exc:  # noqa: BLE001 - a registry read must not kill the connection
            logger.warning("acp agent {!r}: could not place unprompted turn: {}", self._agent, exc)
            return None
        for key, rec in records.items():
            if not isinstance(rec, dict) or rec.get("agentId") != session_id:
                continue
            session_key, agent, handle = key
            if agent == self._agent:
                return session_key, handle
        return None


def _text_of(update: dict[str, Any]) -> str:
    content = update.get("content")
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


__all__ = ["UnpromptedRecorder"]
