"""The ``task_board`` tool: how the host model reads its own Task Board back.

One schema carries both actions on purpose. The post-turn call sends the turn's
own tools array, byte for byte, so a provider that caches by prefix reads the
whole window from cache; an update-only schema sent there alone would miss it
from the first token. During a turn ``update`` is refused -- the board is kept
by the post-turn call, which reads the forced call's arguments itself.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from raven.agent.task_board.board import TaskLink
from raven.agent.task_board.lineage import task_status
from raven.agent.task_board.recall import list_groups, rank, search, thread_text, turn_texts
from raven.agent.task_board.service import TaskBoardService
from raven.contracts.tool import Tool

TOOL_NAME = "task_board"
REFUSED_UPDATE = "The board is updated for you after this turn ends; use action=recall to read it."

# The update's shape -- fields, statuses, handles -- is stated by the post-turn
# instruction, paid once per turn, rather than nested here, where every model call
# of every turn would pay for it.
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["recall", "update"]},
        "query": {"type": "string"},
        "group_id": {"type": "string"},
        "include_turns": {"type": "boolean"},
        "steps": {"type": "array", "items": {"type": "object"}},
        "statuses": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["action"],
}


class TaskBoardTool(Tool):
    """Reads this conversation's Task Board: a listing, one group's thread, or a search."""

    def __init__(self, service: TaskBoardService) -> None:
        self._service = service
        self._session_key: ContextVar[str | None] = ContextVar("task_board_session_key", default=None)

    def set_context(self, channel: str, chat_id: str, session_key: str | None = None) -> None:
        self._session_key.set(session_key or f"{channel}:{chat_id}")

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "This conversation's task board -- one group per matter, one step per turn that moved it -- "
            "kept for you after every turn, so never call action=update. action=recall: alone lists the "
            "groups; group_id gives one group's thread; query finds a matter by its words; include_turns "
            "adds each step's turn."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return PARAMETERS

    async def execute(
        self,
        action: str,
        query: str | None = None,
        group_id: str | None = None,
        include_turns: bool = False,
        **_: Any,
    ) -> str:
        if action == "update":
            return REFUSED_UPDATE
        if action != "recall":
            return f"Error: unknown action {action!r}; use action=recall."
        key = self._session_key.get()
        if not key:
            return "Error: no conversation is bound to this call."
        service = self._service
        board = service.load(key)
        now = service.now_ms()
        if not group_id and not query:
            return list_groups(board, now_ms=now)
        lineage = service.lineage(key, board)
        session_dir = service.session_dir(key)

        def status_of(link: TaskLink) -> str:
            return task_status(session_dir, link)

        if group_id:
            try:
                out = thread_text(board, group_id, status_of=status_of, lineage=lineage)
            except KeyError:
                return f"No group {group_id!r} on the board.\n\n{list_groups(board, now_ms=now)}"
            shown = next(g for g in board.groups if g.id == group_id)
        else:
            out = search(board, query or "", status_of=status_of, lineage=lineage)
            ranked = rank(board, query or "")
            shown = ranked[0] if ranked else None
        if include_turns and shown is not None:
            out += "\n\n" + turn_texts(service.messages(key), [s.turn_at for s in shown.steps])
        return out
