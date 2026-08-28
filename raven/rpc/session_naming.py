"""Naming a session alongside the turn that opens it.

Fired from ``turn.send`` rather than from the agent loop, and never awaited by
it: the turn is what the user is waiting for, and a name is worth nothing if it
costs the answer a second. Every guard here fails toward silence -- the
mechanical title ``SessionManager.save`` derives from the first user message is
already on its way to disk, so a naming call that is skipped, times out, or
answers badly leaves a session named exactly as it is named today.

Fired here means gateway sessions only (the GUI and the TUI both send turns
through ``turn.send``). A session opened from the CLI or an IM channel keeps its
mechanical title; the generator itself (``raven.session.title``) holds no
reference to this layer, so wiring it into the loop later reaches those paths
without moving any of the logic below.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.session.title import generate_title
from raven.utils.text import display_width

if TYPE_CHECKING:
    from raven.rpc.subscriptions import SubscriptionEmitter
    from raven.session.manager import SessionManager

# Session keys with a naming call in flight. An in-flight guard, not a ledger:
# a key is added before the task starts and dropped when it ends, so this holds
# nothing between turns. It exists because two sends can land before the first
# user message is recorded, and both would otherwise see a message-less session
# and pay for the same title twice.
_in_flight: set[str] = set()

# Same reason ``RpcServer`` keeps its dispatch tasks: a bare create_task is only
# weakly referenced by the loop and may be collected mid-await.
_tasks: set[asyncio.Task[None]] = set()


def _needs_naming(mgr: "SessionManager", session_key: str) -> bool:
    """True when this session has no name of its own and no history yet.

    Read before the turn records anything, so "no user message on disk" is what
    identifies the opening turn. A session already carrying a human title is
    left alone even when it is empty -- ``session new --title`` names one up
    front, and that name is not a placeholder waiting to be improved.
    """
    session = mgr.peek(session_key)
    if session is None:
        return True
    metadata = session.metadata or {}
    if metadata.get("title") and not metadata.get("title_auto"):
        return False
    return not any(m.get("role") == "user" for m in session.messages)


async def _name_session(
    *,
    session_key: str,
    text: str,
    mgr: "SessionManager",
    provider: Any,
    emitter: "SubscriptionEmitter | None",
    model: str | None,
    budget: int,
    timeout_seconds: float,
) -> None:
    async def _ended(reason: str) -> None:
        """Say that no title is coming, so nobody waits out a grace period for it.

        Every quiet exit below routes through here. They used to just return: the
        turn was unharmed and the mechanical title stood, which is correct as far
        as the session goes, but a client holding a placeholder had no way to
        learn any of it and could only wait. The model answering without calling
        the naming tool is the common one and takes about a second.
        """
        if emitter is None:
            return
        await emitter.emit(
            session_key,
            {"type": "session.naming_ended", "payload": {"session_id": session_key, "reason": reason}},
        )

    try:
        title = await asyncio.wait_for(
            generate_title(provider, text, model=model, budget=budget),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.debug("session naming: {} timed out after {}s; keeping fallback", session_key, timeout_seconds)
        await _ended("timeout")
        return
    except Exception as exc:
        logger.debug("session naming: {} failed ({}); keeping fallback", session_key, exc)
        await _ended("error")
        return
    if not title:
        await _ended("no_title")
        return

    # Re-read rather than reuse anything captured before the call: the turn has
    # been running throughout, so the session on disk now has this turn's
    # messages, and the person may have typed their own name for it meanwhile.
    # ``set_generated_title`` is what refuses that second case.
    session = mgr.get_or_create(session_key)
    if not session.set_generated_title(title):
        # It refuses for two unrelated reasons, and the client acts on the
        # difference: 'renamed' tells it the row already carries a better name
        # and must not be settled onto the opening line. The other refusal is
        # the title being empty after collapsing or longer than storage allows,
        # which nobody typed -- unreachable while `budget` stays under
        # TITLE_STORAGE_MAX, since clean_model_title clamps to it, but a config
        # raising `budget` past 200 would otherwise report every generated title
        # as a rename that never happened.
        metadata = session.metadata or {}
        by_hand = bool(metadata.get("title")) and not metadata.get("title_auto")
        logger.debug(
            "session naming: {} dropped {!r} ({})",
            session_key,
            title,
            "named by hand while the call ran" if by_hand else "title not storable",
        )
        await _ended("renamed" if by_hand else "no_title")
        return
    stored = session.metadata.get("title", title)
    if mgr.exists(session_key):
        try:
            mgr.save(session)
        except Exception:
            logger.warning("session naming: failed to persist the title for {}", session_key)
    # Emitted even when the save was skipped or failed: the title is live in the
    # cached session either way, and the front end is holding a placeholder for
    # it. A lazy session lands the same title on its first save, exactly as a
    # title set through ``session.title`` does.
    if emitter is not None:
        await emitter.emit(
            session_key, {"type": "session.titled", "payload": {"session_id": session_key, "title": stored}}
        )


def name_session_alongside_turn(
    *,
    session_key: str,
    text: str,
    mgr: "SessionManager",
    provider: Any,
    emitter: "SubscriptionEmitter | None",
    enabled: bool,
    model: str | None,
    budget: int,
    min_input_width: int,
    timeout_seconds: float,
) -> asyncio.Task[None] | None:
    """Start the naming call, or decide there is nothing to name. Never raises.

    Returns the task so a caller that needs to wait on it can (the tests do).
    ``turn.send`` deliberately does not: the turn is what the client is waiting
    for, and this is the errand running beside it.
    """
    if not enabled or provider is None:
        return None
    stripped = text.strip()
    if display_width(stripped) < min_input_width:
        return None
    if session_key in _in_flight:
        return None
    try:
        if not _needs_naming(mgr, session_key):
            return None
    except Exception as exc:
        logger.debug("session naming: could not read {} ({}); skipping", session_key, exc)
        return None

    _in_flight.add(session_key)

    async def _run() -> None:
        try:
            await _name_session(
                session_key=session_key,
                text=stripped,
                mgr=mgr,
                provider=provider,
                emitter=emitter,
                model=model,
                budget=budget,
                timeout_seconds=timeout_seconds,
            )
        finally:
            _in_flight.discard(session_key)

    task = asyncio.create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
