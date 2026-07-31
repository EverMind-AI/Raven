"""Tool-failure loop break: nudge the model off a tool it keeps failing on.

When the same tool fails deterministically N times running (transient errors
excluded), the loop appends a change-approach nudge to the tool result — once
per fresh streak, bounded per turn — so a weak model stops repeating a dead call.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.main import _is_hard_tool_failure
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# --------------------------------------------------------------------------- #
# unit: _is_hard_tool_failure                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "result,expected",
    [
        ("Error: Tool 'x' not found. Available: a, b", True),
        ("Error: file does not exist", True),
        ("No matches found.", False),  # empty search = success, not a failure
        ("No files found", False),  # find empty result
        ("route not found in cache, using local fallback", False),  # success mentioning the phrase
        ("Exit code: 1\nboom", True),
        ("Exit code: 0\nok", False),  # exit 0 = success
        ("ok, wrote 3 files", False),
        ("Error: 429 rate limit, retry later", False),  # transient → not hard
        ("request timed out", False),  # transient → not hard
    ],
)
def test_is_hard_tool_failure(result, expected):
    assert _is_hard_tool_failure(result) is expected


# --------------------------------------------------------------------------- #
# loop level: repeated same-tool failure -> bounded nudges                     #
# --------------------------------------------------------------------------- #


class _AlwaysFailsSameToolProvider(LLMProvider):
    """Keeps calling one (nonexistent) tool that hard-fails every time."""

    def __init__(self):
        super().__init__(api_key="test")
        self.loop_marker_counts: list[int] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.loop_marker_counts.append(sum(1 for m in messages if "[loop]" in str(m.get("content", ""))))
        if tools is None:  # max-iter synthesis call
            return LLMResponse(content="done", finish_reason="stop")
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{len(self.loop_marker_counts)}", name="no_such_tool", arguments={})],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_repeated_tool_failure_nudges_bounded(workspace):
    provider = _AlwaysFailsSameToolProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=6,
        restrict_to_workspace=True,
    )

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )

    # A nudge fired (>=1 [loop] marker seen) but never exceeded the per-turn cap.
    assert max(provider.loop_marker_counts) == AgentLoop._LOOP_BREAK_MAX


# --------------------------------------------------------------------------- #
# loop level: byte-identical failing call -> breaker refuses execution         #
# --------------------------------------------------------------------------- #


class _CountingTool:
    """Registrable stub tool; counts real executions."""

    timeout_seconds = None
    blocking_interaction = False

    def __init__(self, result: str):
        self.calls = 0
        self._result = result

    name = "stub_tool"
    description = "stub"
    parameters = {"type": "object", "properties": {"x": {"type": "string"}}}

    def cast_params(self, params):
        return params

    def validate_params(self, params):
        return []

    def to_schema(self):
        return {
            "type": "function",
            "function": {"name": self.name, "description": "stub", "parameters": self.parameters},
        }

    async def execute(self, **kwargs):
        self.calls += 1
        return self._result


class _RepeatsSameCallProvider(LLMProvider):
    """Calls stub_tool with the given per-iteration arguments forever."""

    def __init__(self, args_seq):
        super().__init__(api_key="test")
        self._args_seq = args_seq
        self.turn = 0
        self.saw_breaker = False

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.saw_breaker = self.saw_breaker or any(
            "was NOT executed again" in str(m.get("content", "")) for m in messages
        )
        if tools is None:
            return LLMResponse(content="done", finish_reason="stop")
        args = self._args_seq[min(self.turn, len(self._args_seq) - 1)]
        self.turn += 1
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{self.turn}", name="stub_tool", arguments=args)],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


async def _run_with(provider, tool, workspace, iters=7):
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=iters,
        restrict_to_workspace=True,
    )
    agent.tools.register(tool)
    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )
    return agent


@pytest.mark.asyncio
async def test_identical_failing_call_is_refused_after_threshold(workspace):
    tool = _CountingTool("Error: deterministic boom")
    provider = _RepeatsSameCallProvider([{"x": "same"}])
    await _run_with(provider, tool, workspace)
    # Executed exactly threshold times; further identical calls were refused.
    assert tool.calls == AgentLoop._SAME_CALL_BREAK_THRESHOLD
    assert provider.saw_breaker


@pytest.mark.asyncio
async def test_varying_arguments_never_trip_breaker(workspace):
    tool = _CountingTool("Error: deterministic boom")
    provider = _RepeatsSameCallProvider([{"x": f"v{i}"} for i in range(10)])
    await _run_with(provider, tool, workspace)
    assert tool.calls > AgentLoop._SAME_CALL_BREAK_THRESHOLD
    assert not provider.saw_breaker


@pytest.mark.asyncio
async def test_identical_successful_polling_never_trips_breaker(workspace):
    """Identical successful repeats are legitimate polling (exec_read on a
    building session) and must never be refused."""
    tool = _CountingTool("(no new output)\n[job job1 still running - poll again for more]")
    provider = _RepeatsSameCallProvider([{"x": "poll"}])
    await _run_with(provider, tool, workspace, iters=8)
    assert tool.calls == 8  # every tool iteration actually executed
    assert not provider.saw_breaker
