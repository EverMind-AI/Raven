"""Spatial vocabulary: where a message came from and what it carried.

Pure frozen data, zero behaviour. Shared by spine and the channel rewrite.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ChatType(StrEnum):
    """Conversation shape, collapsed to the two forms behaviour branches on."""

    DM = "dm"
    GROUP = "group"


@dataclass(frozen=True)
class Source:
    """Where a message came from and where a reply is delivered.

    ``surface`` names the front end behind the channel when the connection
    declared one at handshake (``system.hello``): the terminal and the served
    page share the ``tui`` channel on purpose, so the channel alone cannot say
    which one a turn came from. ``None`` means undeclared -- tracing falls back
    to the process-wide ``raven.tracing.set_surface`` value, then the channel.
    """

    channel: str
    chat_id: str
    sender_id: str
    chat_type: ChatType
    extras: Mapping[str, Any] = field(default_factory=dict)
    surface: str | None = None


@dataclass(frozen=True)
class Media:
    """A downloaded attachment with a local path; built only after the gate."""

    path: str
    mime: str
    kind: str
