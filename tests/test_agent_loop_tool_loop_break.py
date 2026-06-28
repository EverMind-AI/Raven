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
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# --------------------------------------------------------------------------- #
# unit: _is_hard_tool_failure                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("result,expected", [
    ("Error: Tool 'x' not found. Available: a, b", True),
    ("Error: file does not exist", True),
    ("No matches found.", False),         # empty search = success, not a failure
    ("No files found", False),            # find empty result
    ("route not found in cache, using local fallback", False),  # success mentioning the phrase
    ("Exit code: 1\nboom", True),
    ("Exit code: 0\nok", False),          # exit 0 = success
    ("ok, wrote 3 files", False),
    ("Error: 429 rate limit, retry later", False),   # transient → not hard
    ("request timed out", False),                    # transient → not hard
])
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

    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, reasoning_effort=None, tool_choice=None):
        self.loop_marker_counts.append(
            sum(1 for m in messages if "[loop]" in str(m.get("content", "")))
        )
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
        provider=provider, workspace=workspace,
        model="stub", max_iterations=6, restrict_to_workspace=True,
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
