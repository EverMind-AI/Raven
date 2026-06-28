"""Turn-level logging parity.

Previously ``tui.log`` only had ``Tool call:`` start lines (loop/main.py:808); no
``Tool result:`` / iteration boundary, so a multi-minute tool-heavy turn left
no trace of what the agent was doing. These tests drive a real ``_run_agent_loop``
through one tool iteration + one final iteration and assert the new logs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from raven.agent.loop import AgentLoop
from raven.agent.tools.base import Tool
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _ScriptedProvider(LLMProvider):
    """Returns queued LLMResponses from ``chat`` (chat_with_retry wraps it)."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self._responses = responses

    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, reasoning_effort=None, tool_choice=None):
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "stub"


class _FakeTool(Tool):
    def __init__(self, name: str, result: str = "fake-result") -> None:
        self._name = name
        self._result = result
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"fake {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self._result


def _make_agent(workspace: Path, responses: list[LLMResponse], tool: Tool) -> AgentLoop:
    agent = AgentLoop(
        provider=_ScriptedProvider(responses),
        workspace=workspace,
        model="stub",
        max_iterations=5,
        restrict_to_workspace=True,
    )
    agent.tools.register(tool)
    return agent


def _capture(level: str = "DEBUG"):
    captured: list[str] = []
    sink_id = logger.add(lambda m: captured.append(str(m)), level=level, format="{message}")
    return captured, sink_id


# ---------------------------------------------------------------------------
# REQ-1: Tool result log emitted after tool execution
# ---------------------------------------------------------------------------


async def test_tool_result_logged_after_execute(workspace) -> None:
    tool = _FakeTool("exec", result="total 0\nfile.txt")
    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="Done.", finish_reason="stop"),
    ]
    agent = _make_agent(workspace, responses, tool)

    captured, sink_id = _capture("INFO")
    try:
        final, tools_used, _, _ = await agent._run_agent_loop([{"role": "user", "content": "ls"}])
    finally:
        logger.remove(sink_id)

    assert final == "Done."
    assert tools_used == ["exec"]
    assert any("Tool call: exec" in c for c in captured), captured
    result_lines = [c for c in captured if "Tool result:" in c]
    assert len(result_lines) == 1, captured
    assert "exec" in result_lines[0]
    assert "duration=" in result_lines[0]
    assert "file.txt" in result_lines[0], "result preview must be included"


async def test_tool_result_preview_truncated(workspace) -> None:
    tool = _FakeTool("grep", result="X" * 500)
    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="grep", arguments={"q": "x"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="ok", finish_reason="stop"),
    ]
    agent = _make_agent(workspace, responses, tool)
    captured, sink_id = _capture("INFO")
    try:
        await agent._run_agent_loop([{"role": "user", "content": "go"}])
    finally:
        logger.remove(sink_id)
    result_lines = [c for c in captured if "Tool result:" in c]
    assert len(result_lines) == 1
    # Preview truncated to ~200 chars — full 500-char result must not appear.
    assert "X" * 500 not in result_lines[0]


# ---------------------------------------------------------------------------
# REQ-2: iteration boundary log
# ---------------------------------------------------------------------------


async def test_iteration_boundary_logged(workspace) -> None:
    tool = _FakeTool("exec")
    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={})],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="final", finish_reason="stop"),
    ]
    agent = _make_agent(workspace, responses, tool)
    captured, sink_id = _capture("DEBUG")
    try:
        await agent._run_agent_loop([{"role": "user", "content": "x"}])
    finally:
        logger.remove(sink_id)
    iter_lines = [c for c in captured if "Iteration" in c]
    # Two iterations ran (tool dispatch + final).
    assert len(iter_lines) >= 2, captured
    assert any("model=" in c for c in iter_lines)


# ---------------------------------------------------------------------------
# REQ-3: rust-notify DEBUG spam suppression (watchfiles logger)
# ---------------------------------------------------------------------------


def test_suppress_noisy_watchers_raises_watchfiles_level() -> None:
    import logging

    from raven.cli.tui_commands import _suppress_noisy_watchers

    # Simulate watchfiles emitting at DEBUG (the 'rust notify timeout' spam).
    logging.getLogger("watchfiles.main").setLevel(logging.DEBUG)
    _suppress_noisy_watchers()
    for name in ("watchfiles", "watchfiles.main", "watchdog"):
        assert logging.getLogger(name).level >= logging.INFO, name
    # A DEBUG record on watchfiles.main is now below the logger's threshold.
    assert not logging.getLogger("watchfiles.main").isEnabledFor(logging.DEBUG)
