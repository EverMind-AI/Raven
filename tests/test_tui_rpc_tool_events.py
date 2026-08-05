"""Generalized tool progress events.

Structured tool progress used to exist only for the retired chat-channel
message tool; exec / read_file / grep / ... ran with zero structured tool events,
so the TUI showed nothing during multi-tool turns. ``tool.start`` /
``tool.complete`` now fire for every tool the agent dispatches, with no
per-tool exceptions.

Coverage is N+1 variant (one case per registered tool + plain text), mock-forced
(not real-LLM stochastic).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.tools.base import Tool
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _ScriptedProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self._responses = responses

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
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "stub"


class _FakeTool(Tool):
    def __init__(self, name: str, result: str = "fake-result") -> None:
        self._name = name
        self._result = result

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
        return self._result


def _make_agent(workspace: Path, responses: list[LLMResponse], *tools: Tool) -> AgentLoop:
    agent = AgentLoop(
        provider=_ScriptedProvider(responses),
        workspace=workspace,
        model="stub",
        max_iterations=5,
        restrict_to_workspace=True,
    )
    for t in tools:
        agent.tools.register(t)
    return agent


def _tool_then_final(tool_name: str, result: str = "ok"):
    return [
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c-{tool_name}", name=tool_name, arguments={"a": 1})],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="final", finish_reason="stop"),
    ]


# ---------------------------------------------------------------------------
# REQ-6: tool.start before execute, tool.complete after, real tool_call_id.
# ---------------------------------------------------------------------------


async def test_tool_start_and_complete_emitted(workspace) -> None:
    tool = _FakeTool("exec", result="total 0\nfile.txt")
    agent = _make_agent(workspace, _tool_then_final("exec", "total 0\nfile.txt"), tool)

    events: list[tuple[str, dict]] = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    final, tools_used, _, _ = await agent._run_agent_loop(
        [{"role": "user", "content": "ls"}],
        on_tool_event=on_tool_event,
    )

    assert final == "final"
    phases = [p for p, _ in events]
    assert phases == ["start", "complete"], events
    start_info = events[0][1]
    assert start_info["tool_call_id"] == "c-exec"
    assert start_info["name"] == "exec"
    assert start_info["arguments"] == {"a": 1}
    complete_info = events[1][1]
    assert complete_info["tool_call_id"] == "c-exec"
    assert "file.txt" in complete_info["result_preview"]
    assert complete_info["truncated"] is False


async def test_tool_complete_truncated_flag(workspace) -> None:
    tool = _FakeTool("grep", result="X" * 500)
    agent = _make_agent(workspace, _tool_then_final("grep", "X" * 500), tool)
    events: list[tuple[str, dict]] = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    await agent._run_agent_loop(
        [{"role": "user", "content": "go"}],
        on_tool_event=on_tool_event,
    )
    complete = [i for p, i in events if p == "complete"][0]
    assert complete["truncated"] is True
    assert len(complete["result_preview"]) <= 200


# ---------------------------------------------------------------------------
# REQ-8: N+1 tool variants — each registered tool + plain text.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["exec", "read_file", "grep", "list_dir", "web_search"])
async def test_n_plus_1_each_tool_emits_events(workspace, tool_name) -> None:
    tool = _FakeTool(tool_name)
    agent = _make_agent(workspace, _tool_then_final(tool_name), tool)
    events: list[tuple[str, dict]] = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    await agent._run_agent_loop(
        [{"role": "user", "content": "x"}],
        on_tool_event=on_tool_event,
    )
    assert [p for p, _ in events] == ["start", "complete"]
    assert all(i["tool_call_id"] == f"c-{tool_name}" for _, i in events)


async def test_n_plus_1_plain_text_emits_no_tool_events(workspace) -> None:
    """The +1 case: LLM picks plain text → zero tool events."""
    agent = _make_agent(workspace, [LLMResponse(content="hi", finish_reason="stop")])
    events: list = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    final, tools_used, _, _ = await agent._run_agent_loop(
        [{"role": "user", "content": "hi"}],
        on_tool_event=on_tool_event,
    )
    assert final == "hi"
    assert events == []
    assert tools_used == []
