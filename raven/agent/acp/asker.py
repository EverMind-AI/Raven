"""The turn's route to a human, for code with no tool registry to look in.

`AskUserTool` is registered per `AgentLoop`, and an ACP backend is built from
config with no loop reference, so it cannot resolve the tool the way
`AgentLoop._confirm_graph` does. A turn-scoped ContextVar is how `ExecTool`
already solves the same problem for shell approvals, and it inherits into the
background task a sub-agent run happens on.
"""

from __future__ import annotations

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


__all__ = ["Asker", "current_ask", "start_ask_turn"]
