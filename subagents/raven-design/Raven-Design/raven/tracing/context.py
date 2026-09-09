"""Per-turn trace context, propagated via contextvars.

contextvars survive ``await`` and are snapshotted when ``asyncio.create_task``
forks a child task — so a subagent spawned mid-turn (P1) inherits the turn's
span as parent automatically, and nested LLM/tool calls hang off the right node.
"""

from __future__ import annotations

import contextlib
import contextvars
import secrets
import time
from dataclasses import dataclass, replace
from typing import Iterator


@dataclass(frozen=True)
class TraceCtx:
    trace_id: str
    session_key: str | None = None
    channel: str | None = None
    chat_id: str | None = None
    parent_span_id: str | None = None
    # Name of the nearest enclosing non-model span — the purpose a model call is
    # made on behalf of (turn / memory.extract / skill.gate / ...). Model-kind
    # spans inherit it rather than becoming a source themselves, so a nested
    # ``llm.call`` can self-label without walking the tree. Generic: no adopter
    # names are hard-coded here.
    source: str | None = None
    # Stable trajectory address for this span tree. Defaults to the trace id
    # (every turn is a single-turn attempt); an explicit attempt opened via
    # begin_attempt() groups multiple turns of one session under one id.
    attempt_id: str | None = None


_CTX: contextvars.ContextVar[TraceCtx | None] = contextvars.ContextVar("raven_tracing_ctx", default=None)

# session_key -> open attempt id. Plain module state (not a contextvar): an
# attempt outlives any single turn's task, so it must be visible to the fresh
# context each new turn starts in. In-memory only — a process restart closes
# all attempts (spans after restart fall back to single-turn attempt ids).
_ATTEMPTS: dict[str, str] = {}


def current() -> TraceCtx | None:
    return _CTX.get()


def new_trace_id() -> str:
    return f"trace-{int(time.time() * 1000):x}-{secrets.token_hex(4)}"


def new_span_id() -> str:
    return f"span-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"


def new_attempt_id() -> str:
    return f"att-{int(time.time() * 1000):x}-{secrets.token_hex(4)}"


def begin_attempt(session_key: str, attempt_id: str | None = None) -> str:
    """Open an attempt for ``session_key``; subsequent turns carry its id.

    Re-entrant by replacement: beginning while one is open replaces it (the
    old attempt is implicitly ended). Returns the attempt id in effect.
    """
    aid = attempt_id or new_attempt_id()
    _ATTEMPTS[session_key] = aid
    return aid


def end_attempt(session_key: str) -> str | None:
    """Close the open attempt for ``session_key`` (returns its id, or None)."""
    return _ATTEMPTS.pop(session_key, None)


def current_attempt(session_key: str | None) -> str | None:
    if session_key is None:
        return None
    return _ATTEMPTS.get(session_key)


@contextlib.contextmanager
def turn_scope(
    *,
    session_key: str | None,
    channel: str | None,
    chat_id: str | None,
    root_span_id: str,
) -> Iterator[TraceCtx]:
    """Open a fresh trace for one turn; child spans parent onto ``root_span_id``."""
    trace_id = new_trace_id()
    ctx = TraceCtx(
        trace_id=trace_id,
        session_key=session_key,
        channel=channel,
        chat_id=chat_id,
        parent_span_id=root_span_id,
        attempt_id=current_attempt(session_key) or trace_id,
    )
    token = _CTX.set(ctx)
    try:
        yield ctx
    finally:
        _CTX.reset(token)


def push(
    *,
    trace_id: str,
    span_id: str,
    name: str | None = None,
    kind: str | None = None,
    session_key: str | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
    attempt_id: str | None = None,
):
    """Set the active ctx so descendants parent onto ``span_id``; returns a reset token.

    Used by the ``trace.span`` facade for manual instrumentation — it controls
    enter/exit explicitly rather than via a ``with`` block. Pair with :func:`reset`.

    ``name``/``kind`` propagate the enclosing ``source`` (see :class:`TraceCtx`):
    a non-model span becomes the source for its descendants; a model span inherits
    its parent's source (so it is never its own invocation source).
    """
    cur = _CTX.get()
    parent_source = cur.source if cur else None
    source = parent_source if (kind == "model" or not name) else name
    return _CTX.set(
        TraceCtx(
            trace_id=trace_id,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            parent_span_id=span_id,
            source=source,
            attempt_id=attempt_id,
        )
    )


def reset(token) -> None:
    _CTX.reset(token)


@contextlib.contextmanager
def child_scope(span_id: str) -> Iterator[TraceCtx]:
    """Re-parent descendants onto ``span_id`` (used by the subagent probe, P1)."""
    cur = _CTX.get() or TraceCtx(trace_id=new_trace_id())
    token = _CTX.set(replace(cur, parent_span_id=span_id))
    try:
        yield _CTX.get()  # type: ignore[misc]
    finally:
        _CTX.reset(token)
