"""Spawn tool for creating background subagents."""

from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from raven.agent.tools.base import Tool

if TYPE_CHECKING:
    from raven.agent.subagent import SubagentManager


@dataclass(frozen=True)
class _SpawnOrigin:
    """Per-turn origin for subagent announcements, isolated per asyncio task
    (the tool is shared; a turn runs in its own lane task). Frozen +
    copy-on-write so a child task that inherited the parent's value never
    writes back through the shared reference."""

    channel: str
    chat_id: str
    # The originating turn's conversation key. For channels this equals
    # ``channel:chat_id``, but the TUI mints a per-session key (``tui:<sid>``)
    # that the front-end subscribes on, so it must be carried explicitly:
    # subagent results re-inject on this key, and the reply is emitted to it.
    conversation: str | None = None

    @property
    def session_key(self) -> str:
        return self.conversation or f"{self.channel}:{self.chat_id}"


class SpawnTool(Tool):
    """Tool to spawn a subagent for background task execution."""

    # A subagent runs its own (up to 15-iteration) loop with no internal
    # wall-clock cap, so give it a generous backstop rather than the default.
    timeout_seconds = 900.0

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._default = _SpawnOrigin(channel="cli", chat_id="direct")
        self._origin: ContextVar[_SpawnOrigin] = ContextVar("spawn_origin")

    def _cur(self) -> _SpawnOrigin:
        return self._origin.get(None) or self._default

    def set_context(self, channel: str, chat_id: str, conversation: str | None = None) -> None:
        """Set the origin context for subagent announcements (turn-local)."""
        self._origin.set(replace(self._cur(), channel=channel, chat_id=chat_id, conversation=conversation))

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to complete",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for the task (for display)",
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str, label: str | None = None, **kwargs: Any) -> str:
        """Spawn a subagent to execute the given task."""
        org = self._cur()
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=org.channel,
            origin_chat_id=org.chat_id,
            origin_conversation=org.session_key,
            session_key=org.session_key,
        )
