"""Per-tool timeout wrapping in ``ToolRegistry.execute``.

The registry wraps every non-blocking tool in ``asyncio.wait_for`` so a tool
without its own timeout can't wedge the agent loop. Covers:
- a hanging tool is killed at the ceiling and returns an error (no hang)
- a fast tool returns its result normally
- ``timeout_seconds`` overrides the registry default
- ``blocking_interaction`` tools are NOT wrapped (run past the ceiling)
- ``is_blocking`` reports that flag (the fact source the web channel mirrors)
- a CancelledError (e.g. /stop) is not swallowed as a tool error
"""

from __future__ import annotations

import asyncio

import pytest

from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import Tool


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


def test_is_blocking_mirrors_the_tool_flag():
    assert _registry(_SleepTool(0.0, blocking=True)).is_blocking("sleeper") is True
    assert _registry(_SleepTool(0.0)).is_blocking("sleeper") is False


def test_is_blocking_unknown_tool_is_false():
    assert ToolRegistry().is_blocking("no-such-tool") is False


def test_every_subagent_invoking_tool_is_blocking():
    """A tool that runs a sub-agent must declare ``blocking_interaction``.

    The flag is the single fact source the web channel mirrors onto
    ``tool.start`` to suspend its turn-stream idle clock. A sub-agent run stays
    silent for far longer than that clock, so a tool missing the flag ends the
    turn in the web UI while the run is still going.
    """
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.spawn_tool import SpawnTool
    from raven.agent.tools.deep_research import DeepResearchOfferTool, DeepResearchTool

    for cls in (SpawnTool, SubAgentDagTool, DeepResearchTool, DeepResearchOfferTool):
        assert cls.blocking_interaction is True, cls.__name__


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
    from raven.agent.subagent.spawn_tool import SpawnTool
    from raven.agent.tools.media_gen import VideoGenerateTool
    from raven.agent.tools.shell import ExecTool

    assert ExecTool.timeout_seconds >= 600
    assert VideoGenerateTool.timeout_seconds >= 600
    assert SpawnTool.timeout_seconds >= 600
    # Default-class tools inherit None -> registry default applies.
    assert Tool.timeout_seconds is None
    assert Tool.blocking_interaction is False


class _HeartbeatLog:
    """Capture the registry's own INFO lines for the duration of a call."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._sink: int | None = None

    def __enter__(self) -> "_HeartbeatLog":
        from loguru import logger

        self._sink = logger.add(lambda m: self.lines.append(m.record["message"]), level="INFO")
        return self

    def __exit__(self, *exc: object) -> None:
        from loguru import logger

        if self._sink is not None:
            logger.remove(self._sink)

    @property
    def beats(self) -> list[str]:
        return [line for line in self.lines if "still running" in line]


@pytest.mark.asyncio
async def test_a_tool_that_outlives_the_first_beat_says_so(monkeypatch):
    """Between its start line and its result line a tool said nothing, so a
    running call and a stopped process left the same record. Measured on a real
    run: ``ppt_prepare`` held the turn 600s twice and the whole window holds
    three lines -- the call, the timeout, and the next iteration."""
    from raven.agent.tools import registry as registry_module

    monkeypatch.setattr(registry_module, "HEARTBEAT_FIRST_S", 0.02)
    monkeypatch.setattr(registry_module, "HEARTBEAT_EVERY_S", 0.02)
    reg = _registry(_SleepTool(0.12), default_timeout=5.0)

    with _HeartbeatLog() as log:
        assert await reg.execute("sleeper", {}) == "done"

    assert log.beats, "a tool running six beats' worth of time logged nothing"
    assert "sleeper" in log.beats[0]
    # The ceiling is in the line because "still running" alone does not say
    # whether the call is near being killed or nowhere near it.
    assert "5s ceiling" in log.beats[0]


@pytest.mark.asyncio
async def test_an_ordinary_call_is_silent(monkeypatch):
    """The first beat is late on purpose. A line per tool call would bury the
    slow one it exists to point at."""
    from raven.agent.tools import registry as registry_module

    monkeypatch.setattr(registry_module, "HEARTBEAT_FIRST_S", 5.0)
    reg = _registry(_SleepTool(0.0), default_timeout=5.0)

    with _HeartbeatLog() as log:
        assert await reg.execute("sleeper", {}) == "done"

    assert log.beats == []


@pytest.mark.asyncio
async def test_the_beat_stops_when_the_call_does(monkeypatch):
    """Otherwise the registry would go on reporting a tool that had returned --
    a line saying "still running" about a finished call is worse than none."""
    from raven.agent.tools import registry as registry_module

    monkeypatch.setattr(registry_module, "HEARTBEAT_FIRST_S", 0.02)
    monkeypatch.setattr(registry_module, "HEARTBEAT_EVERY_S", 0.02)
    reg = _registry(_SleepTool(0.06), default_timeout=5.0)

    with _HeartbeatLog() as log:
        await reg.execute("sleeper", {})
        settled = len(log.beats)
        await asyncio.sleep(0.15)

        assert len(log.beats) == settled


@pytest.mark.asyncio
async def test_a_timed_out_call_still_reports_the_timeout(monkeypatch):
    """The heartbeat is a bystander: it must not hold the result or move the
    deadline. This is the case that would notice either."""
    from raven.agent.tools import registry as registry_module

    monkeypatch.setattr(registry_module, "HEARTBEAT_FIRST_S", 0.01)
    monkeypatch.setattr(registry_module, "HEARTBEAT_EVERY_S", 0.01)
    reg = _registry(_SleepTool(5.0), default_timeout=0.05)

    with _HeartbeatLog() as log:
        result = await reg.execute("sleeper", {})

    assert "timed out after" in result
    assert log.beats


@pytest.mark.asyncio
async def test_the_first_beat_is_late_and_the_rest_are_not(monkeypatch):
    """Two different intervals, and the difference is the whole design: the
    first is late so ordinary calls stay silent, and the ones after it are not,
    so a reader tailing a slow call sees it advancing rather than waiting the
    same long gap again. A single interval can only have one of those."""
    from raven.agent.tools import registry as registry_module

    monkeypatch.setattr(registry_module, "HEARTBEAT_FIRST_S", 0.30)
    monkeypatch.setattr(registry_module, "HEARTBEAT_EVERY_S", 0.02)
    reg = _registry(_SleepTool(0.60), default_timeout=5.0)

    with _HeartbeatLog() as log:
        await reg.execute("sleeper", {})

    # ~0.30s of silence, then ~15 beats. Held to a fraction of that so a loaded
    # box cannot fail it, while one beat -- what a single interval would give --
    # still cannot pass.
    assert len(log.beats) >= 5, log.beats
