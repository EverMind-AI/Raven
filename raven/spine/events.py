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
    # The runtime ended the turn on a safety decision. Unlike the kinds above,
    # this one replaces the answer rather than accompanying it, so an outlet
    # that renders nothing else should still render this.
    ACTION_BLOCKED = "action_blocked"


class ToolPhase(StrEnum):
    """When a tool event fires; outlets render the two phases differently."""

    START = "start"
    COMPLETE = "complete"


# Lifecycle events — emitted by the worker, never by a runner.

# ``turn_id`` is the second correlation axis alongside ``conversation_id``: the
# lane is WHERE a turn ran, this is WHICH turn ran. A consumer keyed only on the
# lane stamps a turn's end with whatever a per-lane slot last held, which is a
# different turn whenever the runtime submits one of its own onto a busy lane.


@dataclass(frozen=True)
class TurnStarted:
    """Marker that a turn began."""

    conversation_id: str | None = None
    turn_id: str = ""


@dataclass(frozen=True)
class TurnFailed:
    error: str
    cancelled: bool
    conversation_id: str | None = None
    turn_id: str = ""


@dataclass(frozen=True)
class TurnEnded:
    usage: Usage
    latency_ms: float
    explicit_reply: bool
    conversation_id: str | None = None
    turn_id: str = ""


# Deliverable events — emitted by the runner, routed to outlets.


@dataclass(frozen=True)
class ToolEvent:
    phase: ToolPhase
    tool_call_id: str
    name: str = ""
    arguments: dict[str, Any] | None = None
    # Tool-authored call label for the start phase; None -> UI derives one.
    display: str | None = None
    result_preview: str = ""
    truncated: bool = False
    source: Source | None = None
    conversation_id: str | None = None
    # START only: the tool is a blocking interaction, so it has no automatic
    # deadline and may emit nothing for as long as it runs. An outlet whose
    # client clocks the stream must suspend that clock while it is in flight.
    blocking: bool = False
    # COMPLETE only: opt-in structured payload from ToolResult.metadata (e.g. a
    # file manifest). Outlets that do not understand a key ignore it.
    metadata: dict[str, Any] | None = None
    # COMPLETE only: unified diff of what the call changed on disk, when the
    # tool could produce one (see ToolResult.diff). For an outlet that renders
    # the change; never shown to the model.
    diff: str | None = None


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


@dataclass(frozen=True)
class EpisodeStart:
    """Boundary marker: a new model call (episode) begins. ``index`` is the
    0-based step within the turn. Outlets that group a turn into per-call
    episodes use it to start a fresh bucket; others ignore it."""

    index: int
    source: Source | None = None
    conversation_id: str | None = None


RunnerEvent = ToolEvent | Text | MediaOut | StreamDelta | Reasoning | Notice | EpisodeStart
# Same union, named for its delivery role: what the hub routes and an Outlet renders.
Deliverable = RunnerEvent
TurnEvent = TurnStarted | TurnFailed | TurnEnded | RunnerEvent
