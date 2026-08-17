"""The single output vocabulary: everything a turn can emit."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from raven.spine.message import Media, Source


@dataclass(frozen=True)
class Usage:
    """Token accounting for one turn."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class NoticeKind(StrEnum):
    """Out-of-band signals a turn surfaces to the user."""

    PROGRESS = "progress"
    TOOL_HINT = "tool_hint"
    INJECTED = "injected"
    DELIVERY_FAILED = "delivery_failed"


class ToolPhase(StrEnum):
    """When a tool event fires; outlets render the two phases differently."""

    START = "start"
    COMPLETE = "complete"


# Lifecycle events — emitted by the worker, never by a runner.


@dataclass(frozen=True)
class TurnStarted:
    """Marker that a turn began."""

    conversation_id: str | None = None


@dataclass(frozen=True)
class TurnFailed:
    error: str
    cancelled: bool
    conversation_id: str | None = None


@dataclass(frozen=True)
class TurnEnded:
    usage: Usage
    latency_ms: float
    explicit_reply: bool
    conversation_id: str | None = None


# Deliverable events — emitted by the runner, routed to outlets.


@dataclass(frozen=True)
class ToolEvent:
    phase: ToolPhase
    tool_call_id: str
    name: str = ""
    arguments: dict[str, Any] | None = None
    result_preview: str = ""
    truncated: bool = False
    source: Source | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class Text:
    content: str
    source: Source | None = None
    reply_to: str | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class MediaOut:
    media: tuple[Media, ...]
    source: Source | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class StreamDelta:
    delta: str
    stream_id: str | None = None
    source: Source | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class Reasoning:
    content: str
    source: Source | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class Notice:
    kind: NoticeKind
    source: Source | None = None
    detail: str | None = None
    conversation_id: str | None = None


RunnerEvent = ToolEvent | Text | MediaOut | StreamDelta | Reasoning | Notice
# Same union, named for its delivery role: what the hub routes and an Outlet renders.
Deliverable = RunnerEvent
TurnEvent = TurnStarted | TurnFailed | TurnEnded | RunnerEvent
