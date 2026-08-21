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
from raven.agent.loop.failure_streak import failure_class, is_hard_tool_failure
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# --------------------------------------------------------------------------- #
# unit: is_hard_tool_failure                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "result,expected",
    [
        ("Error: Tool 'x' is not available. It may have been unloaded, or the name may be wrong.", True),
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
    assert is_hard_tool_failure(result) is expected


@pytest.mark.parametrize(
    "result,expected",
    [
        # The registry's own wording for a name that did not resolve. It says
        # "is not available" rather than "not found" because it cannot tell a
        # hallucinated name from a tool unloaded mid-turn -- but it is the same
        # failure, and the streak must not file it under the catch-all.
        ("Error: Tool 'x' is not available. It may have been unloaded, or the name may be wrong.", "not_found"),
        ("Error: tool 'x' is not available. It may have been unloaded, or the name may be wrong.", "not_found"),
        ("Error: Invalid parameters for tool 'x': missing 'path'", "schema"),
        ("Error: Tool 'x' timed out after 300s.", "timeout"),
        ("Error: something else entirely", "other"),
    ],
)
def test_failure_class(result, expected):
    assert failure_class(result) == expected


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


class _NudgeTextProvider(_AlwaysFailsSameToolProvider):
    """Keeps the nudge text itself, not just a count of markers."""

    def __init__(self):
        super().__init__()
        self.tool_results: list[str] = []

    async def chat(self, messages, **kwargs):
        self.tool_results += [
            str(m.get("content", ""))
            for m in messages
            if m.get("role") == "tool" and "[loop]" in str(m.get("content", ""))
        ]
        return await super().chat(messages, **kwargs)


def _find_skill_stub():
    from raven.agent.tools.base import Tool

    class _FindSkill(Tool):
        @property
        def name(self) -> str:
            return "find_skill"

        @property
        def description(self) -> str:
            return "stub"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs) -> str:
            return "ran"

    return _FindSkill()


async def _nudges_with_find_skill_switched(workspace, off: bool) -> list[str]:
    """Drive a real failing streak and hand back the nudges it produced.

    Switched off through the config file rather than through the registry's
    source directly, so the path under test is the one the settings page uses.
    """
    import json
    from pathlib import Path

    from raven.config.loader import get_config_path

    cfg = get_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"tools": {"disabledTools": ["find_skill"] if off else []}}), encoding="utf-8")

    provider = _NudgeTextProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=6,
        restrict_to_workspace=True,
    )
    agent.tools.register(_find_skill_stub())
    assert isinstance(cfg, Path)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )
    return provider.tool_results


@pytest.mark.asyncio
async def test_the_nudge_does_not_name_a_switched_off_find_skill(workspace):
    """The nudge is model-visible text, so a registration check advertised a tool
    the operator had switched off -- and `execute` then refuses it as absent.

    Registration was a fair proxy for availability until the off switch stopped
    unregistering; a withheld tool stays in the registry, which is what makes the
    switch reversible.
    """
    nudges = await _nudges_with_find_skill_switched(workspace, off=True)

    assert nudges, "no nudge fired, so the assertion below would pass vacuously"
    assert not any("find_skill" in n for n in nudges)


@pytest.mark.asyncio
async def test_and_does_name_it_when_it_is_on(workspace):
    """The pairing case, so the assertion above cannot pass by never mentioning
    the tool at all."""
    nudges = await _nudges_with_find_skill_switched(workspace, off=False)

    assert nudges
    assert any("find_skill" in n for n in nudges)


class _AlwaysTruncatedWriteProvider(LLMProvider):
    """Every turn is cut off inside the same `write_file` call."""

    def __init__(self):
        super().__init__(api_key="test")
        self.nudges: list[str] = []

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.nudges.extend(str(m.get("content", "")) for m in messages if "[loop]" in str(m.get("content", "")))
        if tools is None:  # max-iter synthesis call
            return LLMResponse(content="done", finish_reason="stop")
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{len(self.nudges)}", name="write_file", arguments={"path": "snake.py"})],
            finish_reason="length",
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_truncation_streak_is_nudged_toward_a_smaller_payload(workspace):
    """The class reaches the nudge from the loop, not only from a direct call.

    The streak key is already `(tool, class)`; only `[0]` was being read, so a
    per-class text is inert until the call site passes the rest of what it has.
    """
    provider = _AlwaysTruncatedWriteProvider()
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
            text="write a long file",
        ),
        session_key="s1",
    )

    assert provider.nudges, "two truncations running should have fired the loop break"
    assert all("different tool" not in n for n in provider.nudges)
    assert all("smaller" in n for n in provider.nudges)
