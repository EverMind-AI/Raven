"""Spine wiring for the ACP turn path: the runner, the outlet that maps each
spine event to its ``session/update`` frame, and the sink that answers the
suspended ``session/prompt``.

The twin of ``raven/tui_rpc/spine.py``, and structurally the same object: an
``Outlet`` that renders spine's deliverables into one wire vocabulary, plus a
``build_*`` that mounts it on a ``DeliveryHub`` behind a ``Scheduler``. spine
never imports acp; acp imports spine.

Why the sink is custom rather than ``make_hub_sink``: that sink returns early on
``TurnStarted`` / ``TurnFailed`` / ``TurnEnded``, so an object registered only as
an outlet never learns a turn ended -- and a suspended ``session/prompt`` would
never be answered. ``build_tui`` solves the same problem the same way for
``message.complete``. Here the sink additionally settles the prompt's future, so
the request's ``stopReason`` is written only after the render barrier: every
``session/update`` belonging to the turn is on the wire before the client is told
the turn is over.

Frames are written through a *synchronous* ``emit``. That is the property the
protocol needs: the writer is a write plus a flush with no suspension point, so
the order of frames on the wire equals the order they were produced in.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import quote

from loguru import logger

from raven.acp import protocol
from raven.acp.session import SessionTable
from raven.acp.tool_kinds import absolute_path, locations, title_for, tool_kind
from raven.agent.spine_runner import AgentTurnRunner
from raven.spine import (
    Deliverable,
    MediaOut,
    OriginPools,
    Reasoning,
    Scheduler,
    Text,
    ToolEvent,
    ToolPhase,
    TurnEnded,
    TurnFailed,
    TurnOutcome,
    TurnRequest,
    TurnStarted,
)
from raven.spine.delivery import Capabilities, DeliveryHub
from raven.spine.events import TurnEvent
from raven.spine.runner import Drain, Emit

Frames = Callable[[dict[str, Any]], None]

# A tool result preview is written for a person to read in a panel. The runtime
# already truncates and sets ``truncated``; this is the backstop for a tool that
# does not, so one runaway result cannot become a multi-megabyte frame.
MAX_RESULT_PREVIEW = 64 * 1024

# How many files one turn's media may put on the wire. A page-weight bound rather
# than a correctness one: a turn that produced more has a reporting problem, not
# a delivery one.
MAX_MEDIA_ITEMS = 32

# The convention the tool registry uses for a failed call: the model-facing text
# of a failure starts with this. Spine's ToolEvent carries no ok flag, so the
# tool_call_update status is read off the preview the same way the loop's own
# retry-hint logic reads it -- a tool that reports a friendly "Error: ..." is
# classified as failed here too, because the convention IS the contract tools
# opt into.
_FAILURE_PREFIX = "error"


def _text_chunk(kind: str, text: str) -> dict[str, Any]:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def file_uri(path: str) -> str:
    """An absolute path as a ``file://`` URI.

    ``as_uri`` rather than a hand-built prefix: it percent-encodes, which is what
    keeps the space in "My Documents" from terminating the URI, and it spells a
    Windows path correctly. The fallback is unreachable from callers that have
    already made the path absolute; it is kept because the property they depend on
    is that this never raises -- a raise here would leave a suspended
    ``session/prompt`` unanswered.
    """
    try:
        return Path(path).as_uri()
    except ValueError:
        return "file://" + quote(path, safe="/")


def resource_link(path: str, *, mime: str | None = None) -> dict[str, Any]:
    """A ``resource_link`` content block for one file.

    A link rather than an ``image`` / ``audio`` block even for a picture: those
    carry base64 ``data``, which would mean reading the file, and a link is what a
    local client wants anyway -- it can open the file in an editor tab instead of
    rendering a copy of it.
    """
    link: dict[str, Any] = {
        "type": "resource_link",
        "uri": file_uri(path),
        # ``name`` is required and is what a client puts in a list. The basename,
        # not the whole path: the path is already in ``uri``, and a deep path
        # renders as one unreadable line.
        "name": PurePath(path).name or path,
    }
    if mime:
        link["mimeType"] = mime
    return link


def usage_update(usage: Any) -> dict[str, Any] | None:
    """How full the context window is, and what the turn cost.

    Emitted only when the window numbers are both real. A ``size`` of zero would
    have a client drawing a full bar or dividing by it, and a turn that reported
    no usage at all (a hook short-circuit, a cached reply) has nothing to say here
    -- an update of zeroes is not the same statement as no update.

    The currency is hardcoded because the figure is: ``cost_usd`` is dollars by
    name, and a configurable currency here would relabel the same number.
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


class AcpTurnRunner(AgentTurnRunner):
    """Runs an ACP turn through the loop's native ``run_turn`` with streaming on.

    Streaming, not the one-shot path ``build_repl`` uses: live delivery is the
    reason to be on this transport rather than on a pipe, and a streamed reply
    reaches the client as ``agent_message_chunk`` frames while the deck is still
    being built.

    The one thing the generic runner does not carry is the usage sink: the
    scheduler's ``TurnEnded.usage`` has three token counts and no window size, so
    the ``usage_update`` frame would have nothing to say. The loop fills the sink
    with ``context_used`` / ``context_max`` / ``cost_usd``, which is exactly the
    shape that frame needs.
    """

    def __init__(self, agent_loop: Any, usages: dict[str, dict[str, Any]]) -> None:
        super().__init__(agent_loop, stream=True, inline_tool_stream=True)
        self._usages = usages

    async def run(self, req: TurnRequest, emit: Emit, drain: Drain) -> TurnOutcome:
        cid = req.conversation or f"{req.source.channel}:{req.source.chat_id}"
        usage_sink: dict[str, Any] = {}
        outcome = await self._loop.run_turn(
            req, emit, drain, stream=True, inline_tool_stream=True, usage_sink=usage_sink
        )
        self._usages[cid] = dict(usage_sink)
        return outcome


class AcpOutlet:
    """The ACP send surface. Maps each spine event to its ``session/update``.

    Streamed reply content rides ``send_stream_chunk`` (-> ``agent_message_chunk``)
    and the discrete deliverables ride ``deliver``: ``Reasoning`` ->
    ``agent_thought_chunk``, ``ToolEvent`` -> ``tool_call`` / ``tool_call_update``,
    a non-streamed ``Text`` -> an ``agent_message_chunk``, ``MediaOut`` -> one
    ``agent_message_chunk`` per file carrying a ``resource_link``.

    ``Notice`` and ``EpisodeStart`` are eaten. ``EpisodeStart`` is a collapsing
    boundary with no ACP counterpart; ``Notice`` reaches the wire in the reference
    only for the ``action_blocked`` kind, which is not one of spine's
    ``NoticeKind`` values here -- so a progress notice has no frame to become and
    is dropped rather than guessed at.

    The turn's own end is not this object's: the sink fires the prompt's
    ``stopReason`` after the render barrier, so it lands after the last chunk.
    """

    def __init__(self, channel: str, emit: Frames, sessions: SessionTable) -> None:
        self.name = channel
        self.capabilities = Capabilities(streaming=True)
        self._emit = emit
        self._sessions = sessions

    def _send(self, session_id: Any, update: dict[str, Any]) -> None:
        """Address one update to its session, dropping it if that session is gone.

        A released session's stream has no subscriber on the far side, and a frame
        naming a ``sessionId`` the client has closed is a protocol violation
        rather than a late delivery.
        """
        session = self._sessions.get(session_id)
        if session is None:
            logger.debug("acp: no session {!r} for a {} update; dropping", session_id, update.get("sessionUpdate"))
            return
        if update.get("sessionUpdate") == "agent_message_chunk":
            content = update.get("content")
            # Collected here rather than at each call site: this is the one point
            # every reply chunk passes through, so what the session records cannot
            # drift from what the client receives.
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                session.reply.append(content["text"])
        self._emit(protocol.session_update(session.session_id, update))

    def _base(self, session_id: Any) -> str | None:
        """The directory a relative path from this session is resolved against.

        The session's root and not its `cwd`: the loop is built with
        `workspace=session.root`, so `read_file(path="deck/build.py")` means a file
        under that root. Handing the client's own directory to `locations()` resolved
        every such path against somewhere the file is not, and the client was shown a
        location that does not exist.
        """
        session = self._sessions.get(session_id)
        return None if session is None else str(session.root)

    async def deliver(self, out: Deliverable) -> None:
        cid = out.conversation_id
        if isinstance(out, Reasoning):
            if out.content:
                self._send(cid, _text_chunk("agent_thought_chunk", out.content))
        elif isinstance(out, ToolEvent):
            self._send(cid, self._tool_update(out, self._base(cid)))
        elif isinstance(out, Text):
            # A non-streamed reply (a clarification, a hook short-circuit, the
            # empty-reply fallback) rides the same chunk kind the streamed reply
            # uses, so a client renders both into one message.
            if out.content:
                self._send(cid, _text_chunk("agent_message_chunk", out.content))
        elif isinstance(out, MediaOut):
            for update in self._media_updates(out, self._base(cid)):
                self._send(cid, update)
        # Notice / EpisodeStart: eaten (no ACP counterpart).

    @staticmethod
    def _tool_update(out: ToolEvent, cwd: str | None) -> dict[str, Any]:
        if out.phase is ToolPhase.START:
            update: dict[str, Any] = {
                "sessionUpdate": "tool_call",
                "toolCallId": out.tool_call_id,
                "title": title_for(out.name or None, out.arguments, out.display),
                "kind": tool_kind(out.name or None),
                # ``in_progress`` and not ``pending``: pending means "not started
                # -- streaming input or awaiting approval", and by the time this
                # event exists the call is running. A pending row that never
                # changes reads as a hang.
                "status": "in_progress",
            }
            # ``rawInput`` is deliberately absent. It would carry the tool's
            # arguments verbatim, and for exec that is the whole command line --
            # published into a transcript the client persists. The title carries
            # what a client needs to draw the row.
            found = locations(out.arguments, cwd)
            if found:
                update["locations"] = found
            return update
        preview = out.result_preview or ""
        update = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": out.tool_call_id,
            "status": "failed" if preview[: len(_FAILURE_PREFIX)].lower() == _FAILURE_PREFIX else "completed",
        }
        if preview:
            text = preview[:MAX_RESULT_PREVIEW]
            if out.truncated or len(preview) > MAX_RESULT_PREVIEW:
                text += "\n[truncated]"
            update["content"] = [{"type": "content", "content": {"type": "text", "text": text}}]
        return update

    @staticmethod
    def _media_updates(out: MediaOut, cwd: str | None) -> list[dict[str, Any]]:
        """One ``agent_message_chunk`` per file the reply carried.

        ``content`` on a chunk is a single ``ContentBlock``, not a list, which is
        why this is a chunk per file rather than one chunk with many blocks.

        A relative path with no session cwd to anchor it is named in text rather
        than sent as a relative URI: the URI would resolve against the client's
        notion of the current directory, so it either fails or opens a different
        file with the same name.
        """
        base = Path(cwd) if cwd else None
        updates: list[dict[str, Any]] = []
        for item in out.media:
            if len(updates) >= MAX_MEDIA_ITEMS:
                break
            raw = (item.path or "").strip()
            if not raw:
                continue
            resolved = absolute_path(raw, base)
            if resolved is None:
                updates.append(_text_chunk("agent_message_chunk", f"[attachment: {raw}]"))
                continue
            updates.append(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": resource_link(resolved, mime=item.mime or None),
                }
            )
        return updates

    async def send_stream_chunk(self, chat_id: str, stream_id: str, delta: str, *, done: bool = False) -> None:
        if done or not delta:
            # There is no stream-done frame in ACP: the turn is finalized by the
            # prompt's own ``stopReason``. ``done=True`` only lets the hub close
            # its stream state.
            return
        # ``stream_id`` is the conversation id, which is the session id.
        self._send(stream_id, _text_chunk("agent_message_chunk", delta))

    def say(self, session_id: str, text: str) -> None:
        """Put one line of agent message on a session's stream.

        Public because two callers outside a turn need it: the method layer's
        explanation for a turn that could not start, and the deck report a
        finished turn is completed with. Both have to be ``agent_message_chunk``
        rather than anything else, because a raven host builds the sub-agent's
        reply out of that update alone.
        """
        self._send(session_id, _text_chunk("agent_message_chunk", text))

    def send_media(self, session_id: str, path: str, *, mime: str | None = None) -> None:
        """Name one produced file on a session's stream, as a typed resource."""
        self._send(session_id, {"sessionUpdate": "agent_message_chunk", "content": resource_link(path, mime=mime)})

    def send_usage(self, session_id: str, usage: Any) -> None:
        update = usage_update(usage)
        if update is not None:
            self._send(session_id, update)


def _make_acp_sink(
    hub: DeliveryHub,
    outlet: AcpOutlet,
    channel: str,
    sessions: SessionTable,
    usages: dict[str, dict[str, Any]],
) -> Callable[[TurnEvent], Awaitable[None]]:
    """Adapt the hub into the scheduler's EventSink for ACP.

    Deliverables route through the hub; a turn's end answers the suspended
    ``session/prompt``. The render barrier is awaited first, so the ``stopReason``
    the client receives is the last word about the turn rather than the first --
    a client that closed its stream on the response would otherwise drop the
    chunks still queued behind it.

    A cancelled turn is settled by ``session/cancel`` and not here: settling twice
    is a no-op by construction, but a *message* explaining the failure would be a
    second explanation for something the client already asked for.
    """

    async def _finish(conversation_id: str) -> None:
        # close_stream clears the hub's per-stream state (so the next turn on this
        # session reopens cleanly); wait_idle then blocks until every queued chunk
        # has been delivered -- a turn that streamed nothing never built a queue,
        # so it returns at once.
        await hub.close_stream(conversation_id)
        await hub.wait_idle(channel)

    async def sink(event: TurnEvent) -> None:
        if isinstance(event, TurnEnded):
            await _finish(event.conversation_id)
            outlet.send_usage(event.conversation_id, usages.pop(event.conversation_id, None))
            session = sessions.get(event.conversation_id)
            if session is not None:
                session.settle("end_turn")
            return
        if isinstance(event, TurnFailed):
            await _finish(event.conversation_id)
            usages.pop(event.conversation_id, None)
            session = sessions.get(event.conversation_id)
            if session is None:
                return
            if not event.cancelled:
                # Said as message content, then ended as ``end_turn``. The
                # alternatives are both worse: erroring the prompt makes some
                # clients tear down the whole turn, and ending silently shows a
                # person a turn that stopped for no stated reason.
                outlet.say(session.session_id, f"The turn failed: {event.error or 'no reason reported'}")
                session.settle("end_turn")
            else:
                session.settle("cancelled")
            return
        if isinstance(event, TurnStarted):
            # The ``session/prompt`` request is itself the record that a turn
            # began, so an update saying so would be a second one.
            return
        await hub.dispatch(event)

    return sink


def build_acp(
    agent_loop: Any,
    emit: Frames,
    sessions: SessionTable,
    *,
    channel: str = "acp",
    user_pool: int = 1,
    system_pool: int = 1,
) -> tuple[Scheduler, DeliveryHub, AcpOutlet, Callable[[], Awaitable[None]]]:
    """Wire the spine pieces one ACP turn flows through.

    A hub carrying this channel's :class:`AcpOutlet`, and a ``Scheduler`` whose
    runner streams the agent loop and whose sink answers the prompt after the
    render barrier. Returns those plus a ``teardown`` the caller awaits on exit --
    stop the scheduler (no more events), then close the hub's outlet workers.

    Must be called from inside the running loop: ``Scheduler`` pins its home loop
    in ``__init__``.
    """
    hub = DeliveryHub()
    outlet = AcpOutlet(channel, emit, sessions)
    hub.register(outlet)
    usages: dict[str, dict[str, Any]] = {}
    scheduler = Scheduler(
        AcpTurnRunner(agent_loop, usages),
        OriginPools(user=user_pool, system=system_pool),
        _make_acp_sink(hub, outlet, channel, sessions, usages),
    )

    async def teardown() -> None:
        await scheduler.shutdown(grace=0.0)
        await hub.aclose()

    return scheduler, hub, outlet, teardown


__all__ = [
    "MAX_MEDIA_ITEMS",
    "MAX_RESULT_PREVIEW",
    "AcpOutlet",
    "AcpTurnRunner",
    "build_acp",
    "file_uri",
    "resource_link",
    "usage_update",
]
