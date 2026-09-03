"""Tests for the optional structured-metadata channel from a tool to the turn
stream: Tool.take_metadata -> ToolEvent.metadata -> the tool.complete wire event."""

from __future__ import annotations

from typing import Any

from raven.contracts.tool import Tool
from raven.spine.events import ToolEvent, ToolPhase


class _Bare(Tool):
    @property
    def name(self) -> str:
        return "bare"

    @property
    def description(self) -> str:
        return "no metadata"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def test_take_metadata_defaults_to_none() -> None:
    """Existing tools must keep working untouched, so the hook is opt-in."""
    assert _Bare().take_metadata() is None


def test_tool_event_metadata_defaults_to_none() -> None:
    """Additive field: every existing ToolEvent construction site keeps working."""
    event = ToolEvent(phase=ToolPhase.COMPLETE, tool_call_id="t1")
    assert event.metadata is None


def test_tool_event_carries_metadata() -> None:
    event = ToolEvent(phase=ToolPhase.COMPLETE, tool_call_id="t1", metadata={"raven_delivery": {"files": []}})
    assert event.metadata == {"raven_delivery": {"files": []}}
