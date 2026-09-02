"""Connection-scoped client identity, carried by contextvars.

One dispatcher now serves several clients at once (the gateway's WebSocket
fan-in), and a handler receives only ``params: dict`` -- nothing names the
connection a frame arrived on. Rather than threading a connection object
through every handler signature, each transport binds a small mutable state
dict into a ContextVar for the lifetime of one connection. Every dispatch task
it spawns snapshots that context (``asyncio.create_task`` copies contextvars),
so all frames from one socket share the dict and frames from different sockets
never see each other's.

Two facts are stored today:

* the surface the client declared in ``system.hello`` (``"tui"`` / ``"page"`` /
  ``"shell"``): ``turn.send`` reads it back and stamps it onto the turn's
  ``Source``, which is how a trace can tell the GUI shell from the browser page
  from a relayed terminal on one gateway;
* how to reach this one connection (:func:`set_frame_sink`), plus the
  conversations it has sent turns for (:func:`claim_conversation`). A frame that
  interrupts a specific conversation -- an ask_user question -- then goes to the
  surface that started that turn instead of to every socket on the gateway; see
  :func:`conversation_scoped`.

A transport that never binds (or a client that never declares) leaves every
read returning ``None``, and both fall back to what they did before: tracing to
the process-wide ``raven.tracing.set_surface`` value, and an interrupting frame
to the transport's broadcast.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from typing import Any

from loguru import logger

_state: ContextVar[dict[str, Any] | None] = ContextVar("rpc_connection_state", default=None)

# ids of the connection states whose sockets are still bound. Kept as ids
# because a state dict is unhashable, and an id is stable for the dict's
# lifetime; the holder of a state keeps the dict (and so the id) alive, so
# id reuse cannot collide here.
_live: set[int] = set()

# conversation key -> the connection state of the surface that last sent a turn
# for it. Process-wide because the reader is not on the owning connection: a
# question is emitted from the engine's own task, which has no connection bound.
# Entries are dropped when that connection unbinds, so a stale key can never
# outlive the socket it names.
_owners: dict[str, dict[str, Any]] = {}

SendFrame = Callable[[dict[str, Any]], Awaitable[None]]

# What a declared surface may look like: a short lowercase token ("tui",
# "page", "shell", "webui"). Bounded so an arbitrary client cannot write
# paragraphs into every span of every turn it sends.
SURFACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def bind_connection() -> Token:
    """Give the current context a fresh connection state; returns a reset token.

    Called by a transport once per accepted connection, before it starts
    dispatching that connection's frames. Pair with :func:`unbind_connection`.
    """
    state: dict[str, Any] = {}
    token = _state.set(state)
    _live.add(id(state))
    return token


def unbind_connection(token: Token) -> None:
    state = _state.get()
    if state is not None:
        _live.discard(id(state))
        for key, owner in list(_owners.items()):
            if owner is state:
                del _owners[key]
    _state.reset(token)


def current_state() -> dict[str, Any] | None:
    """The connection state bound in this context, or None when unbound."""
    return _state.get()


def is_bound(state: dict[str, Any] | None) -> bool:
    """True while the connection that owns ``state`` is still bound."""
    return state is not None and id(state) in _live


def set_frame_sink(sink: SendFrame) -> bool:
    """Record how to send one frame to this connection alone; False if unbound.

    The transports own a broadcast sink (every socket) because that is what a
    subscription stream wants: clients filter by ``subscription_id``. A frame
    that interrupts one conversation has no such handle, so it needs the
    narrower one.
    """
    state = _state.get()
    if state is None:
        return False
    state["send_frame"] = sink
    return True


def current_frame_sink() -> SendFrame | None:
    """The sink of the connection this code is running on, or None.

    Unlike :func:`frame_sink_for` (which resolves through the persistent
    per-conversation owner), this is the connection's OWN sink -- the caller
    on the other end of the socket, whoever owns the conversation. A
    request-local action (a slash's confirm, for example) wants THIS one: it
    must reach the surface that asked, without claiming or disturbing the
    conversation's owner, which still routes the engine's in-flight questions.
    """
    state = _state.get()
    if state is None:
        return None
    sink = state.get("send_frame")
    return sink if callable(sink) else None


def claim_conversation(conversation_id: str) -> bool:
    """Record this connection as the surface a conversation speaks through.

    Called by ``turn.send``: whoever sent the turn is who a mid-turn question
    belongs to. Last sender wins, which is the right answer when two surfaces
    share a session -- the question follows whoever spoke most recently.
    """
    state = _state.get()
    if state is None or not conversation_id:
        return False
    _owners[conversation_id] = state
    return True


def frame_sink_for(conversation_id: str | None) -> SendFrame | None:
    """The owning connection's sink, or ``None`` when nobody owns it."""
    if not conversation_id:
        return None
    state = _owners.get(conversation_id)
    if state is None:
        return None
    sink = state.get("send_frame")
    return sink if callable(sink) else None


def conversation_scoped(broadcast: SendFrame) -> SendFrame:
    """Wrap a broadcast sink so a frame naming a conversation reaches only the
    surface that owns it.

    Falls back to ``broadcast`` whenever the narrow send is not available or
    fails -- an unclaimed conversation (a cron or IM turn, a transport that
    binds nothing), a socket that has gone away since it claimed. The fail-safe
    direction is deliberate: a question that reaches too many surfaces is
    untidy, one that reaches none stalls a turn until it times out.
    """

    async def send(frame: dict[str, Any]) -> None:
        params = frame.get("params") if isinstance(frame, dict) else None
        conversation_id = params.get("conversation_id") if isinstance(params, dict) else None
        sink = frame_sink_for(conversation_id if isinstance(conversation_id, str) else None)
        if sink is not None:
            try:
                await sink(frame)
                return
            except Exception as exc:  # noqa: BLE001 -- the owner is gone; everyone is better than nobody
                logger.warning("rpc: scoped send for conversation {} failed ({}); broadcasting", conversation_id, exc)
        await broadcast(frame)

    return send


def declare_surface(surface: str | None) -> bool:
    """Record the surface the client declared; False when no connection is bound.

    A transport that never called :func:`bind_connection` (the in-process test
    dispatcher, a bare pipe) has nowhere to keep per-connection facts, and that
    is fine -- the caller falls back to the process-wide declaration.
    """
    state = _state.get()
    if state is None:
        return False
    state["surface"] = surface or None
    return True


def declared_surface() -> str | None:
    """The surface the current connection declared at handshake, or ``None``."""
    state = _state.get()
    return state.get("surface") if state is not None else None


__all__ = [
    "SURFACE_RE",
    "bind_connection",
    "claim_conversation",
    "conversation_scoped",
    "current_state",
    "declare_surface",
    "declared_surface",
    "frame_sink_for",
    "is_bound",
    "set_frame_sink",
    "unbind_connection",
]
