"""The turn's route to a human, for code with no tool registry to look in.

`AskUserTool` is registered per `AgentLoop`, and an ACP backend is built from
config with no loop reference, so it cannot resolve the tool the way
`AgentLoop._confirm_graph` does. A turn-scoped ContextVar is how `ExecTool`
already solves the same problem for shell approvals, and it inherits into the
background task a sub-agent run happens on.
"""

from __future__ import annotations

import asyncio
import weakref
from contextvars import ContextVar
from typing import Any, Protocol


class Asker(Protocol):
    async def ask(self, prompt: str, choices: list[str] | None, conversation_id: str) -> str | None: ...


_TURN: ContextVar[tuple[Any, str]] = ContextVar("acp_ask_turn", default=(None, ""))


def start_ask_turn(asker: Any, *, conversation_id: str) -> None:
    """Bind this turn's asker. `None` means no human is reachable."""
    _TURN.set((asker, conversation_id))


def current_ask() -> tuple[Any, str]:
    return _TURN.get()


_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()


def question_lock(conversation_id: str) -> asyncio.Lock:
    """One lock per conversation and loop, for whatever is putting questions to it.

    Per conversation because that is the broker's key: it allows one pending
    question per conversation and fail-safes an overlapping one to its default,
    which to whoever is waiting reads as "the user skipped" and silently loses a
    question nobody ever saw. Shared across both routes an ACP sub-agent's
    question can take, so an elicitation form and an ``ask_user`` round trip
    cannot overlap each other either.

    Per loop because an `asyncio.Lock` binds itself to the first loop that
    contends it and never unbinds: keyed by conversation alone, a lock outliving
    its loop makes every later acquire raise, which the callers can only answer
    with a decline. Weak keys so an entry goes away with its loop.
    """
    locks = _LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[conversation_id] = lock
    return lock


def attribute(agent: str, instance: str, message: str) -> str:
    """Name the sub-agent a question came from, in front of the question.

    A bare separator, not a phrase: there is no backend i18n for user-facing
    strings and both frontends are bilingual, so any wording here would hardcode
    one language into them. Shared by both routes an ACP sub-agent's question can
    take, so the two read identically to whoever answers them.
    """
    who = f"{agent}({instance})" if instance else agent
    return f"{who}: {message}"


__all__ = ["Asker", "attribute", "current_ask", "question_lock", "start_ask_turn"]
