"""Per-tool timeout wrapping in ``ToolRegistry.execute``.

The registry wraps every non-blocking tool in ``asyncio.wait_for`` so a tool
without its own timeout can't wedge the agent loop. Covers:
- a hanging tool is killed at the ceiling and returns an error (no hang)
- a fast tool returns its result normally
- ``timeout_seconds`` overrides the registry default
- ``blocking_interaction`` tools are NOT wrapped (run past the ceiling)
- a CancelledError (e.g. /stop) is not swallowed as a tool error
"""

from __future__ import annotations

import asyncio

import pytest

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry


class _SleepTool(Tool):
    """Sleeps for ``delay`` then returns 'done'. Configurable timeout/blocking."""

    def __init__(self, delay: float, *, timeout_seconds=None, blocking=False):
        self._delay = delay
        self.timeout_seconds = timeout_seconds
        self.blocking_interaction = blocking

    @property
    def name(self) -> str:
        return "sleeper"

    @property
    def description(self) -> str:
        return "sleeps"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        await asyncio.sleep(self._delay)
        return "done"


def _registry(tool: Tool, default_timeout: float | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    if default_timeout is not None:
        reg.DEFAULT_TOOL_TIMEOUT_S = default_timeout
    reg.register(tool)
    return reg


@pytest.mark.asyncio
async def test_fast_tool_returns_result():
    reg = _registry(_SleepTool(0.0), default_timeout=1.0)
    assert await reg.execute("sleeper", {}) == "done"


@pytest.mark.asyncio
async def test_hanging_tool_times_out_at_default_ceiling():
    reg = _registry(_SleepTool(5.0), default_timeout=0.05)
    result = await reg.execute("sleeper", {})
    assert "timed out after" in result
    assert "try a different approach" in result


@pytest.mark.asyncio
async def test_per_tool_timeout_seconds_overrides_default():
    # default would allow it, but the tool's own tighter ceiling fires first
    reg = _registry(_SleepTool(5.0, timeout_seconds=0.05), default_timeout=100.0)
    result = await reg.execute("sleeper", {})
    assert "timed out after 0s" in result


@pytest.mark.asyncio
async def test_blocking_interaction_tool_is_not_wrapped():
    # Sleeps well past the ceiling, but blocking tools are never timer-killed.
    reg = _registry(_SleepTool(0.2, blocking=True), default_timeout=0.05)
    assert await reg.execute("sleeper", {}) == "done"


@pytest.mark.asyncio
async def test_cancelled_error_propagates_not_swallowed():
    class _CancelTool(_SleepTool):
        async def execute(self, **kwargs) -> str:
            raise asyncio.CancelledError()

    reg = _registry(_CancelTool(0.0), default_timeout=1.0)
    with pytest.raises(asyncio.CancelledError):
        await reg.execute("sleeper", {})


@pytest.mark.asyncio
async def test_long_running_tools_keep_generous_ceilings():
    # Guard against regressing the overrides on the genuinely-slow tools.
    from raven.agent.tools.shell import ExecTool
    from raven.agent.tools.media_gen import VideoGenerateTool
    from raven.agent.tools.spawn import SpawnTool

    assert ExecTool.timeout_seconds >= 600
    assert VideoGenerateTool.timeout_seconds >= 600
    assert SpawnTool.timeout_seconds >= 600
    # Default-class tools inherit None -> registry default applies.
    assert Tool.timeout_seconds is None
    assert Tool.blocking_interaction is False
