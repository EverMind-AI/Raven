"""Message tool for sending messages to users."""

from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from raven.agent.tools.base import Tool


@dataclass(frozen=True)
class _MsgTurn:
    """Per-turn send routing + target, isolated per asyncio task.

    The tool is a single shared instance, but a turn runs in its own lane
    task; storing this in a ContextVar makes set_context / set_send_callback /
    the sent flag turn-local, so a USER turn and a concurrent proactive turn
    cannot clobber each other's reply routing. Frozen + copy-on-write: every
    mutator rebinds the ContextVar to a fresh value rather than mutating in
    place, so a child task that inherited the parent's value never writes back
    through the shared reference.
    """

    channel: str
    chat_id: str
    message_id: str | None
    send_callback: Callable[[str, list[str]], Awaitable[None]] | None
    sent: bool = False


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[str, list[str]], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        # Constructor values are the fallback baseline; each turn task copies
        # them into its own ContextVar slot on first access.
        self._default = _MsgTurn(
            channel=default_channel,
            chat_id=default_chat_id,
            message_id=default_message_id,
            send_callback=send_callback,
        )
        self._turn: ContextVar[_MsgTurn] = ContextVar("message_tool_turn")

    def _cur(self) -> _MsgTurn:
        return self._turn.get(None) or self._default

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context (turn-local)."""
        self._turn.set(
            replace(self._cur(), channel=channel, chat_id=chat_id, message_id=message_id)
        )

    def set_send_callback(
        self,
        callback: Callable[[str, list[str]], Awaitable[None]] | None,
    ) -> None:
        """Set the send callback (turn-local). ``None`` is a valid value and
        matches the constructor default — ``execute`` then returns the
        ``not configured`` error string rather than calling through.
        """
        self._turn.set(replace(self._cur(), send_callback=callback))

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._turn.set(replace(self._cur(), sent=False))

    @property
    def sent_in_turn(self) -> bool:
        """Whether a reply was sent to the turn's own target this turn."""
        return self._cur().sent

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents)"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any
    ) -> str:
        st = self._cur()
        channel = channel or st.channel
        chat_id = chat_id or st.chat_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not st.send_callback:
            return "Error: Message sending not configured"

        try:
            await st.send_callback(content, media or [])
            if channel == st.channel and chat_id == st.chat_id:
                self._turn.set(replace(st, sent=True))
            media_info = f" with {len(media)} attachments" if media else ""
            return f"Message sent to {channel}:{chat_id}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
