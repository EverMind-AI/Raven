"""Generalized tool progress events.

Previously a synthetic ``tool.complete`` was only emitted for the MessageTool
path; exec / read_file / grep / ... ran with zero structured tool events, so the
TUI showed nothing during multi-tool turns. This generalizes ``tool.start`` /
``tool.complete`` to every tool the agent dispatches.

Coverage is N+1 variant (one case per registered tool + plain text), mock-forced
(not real-LLM stochastic). MessageTool must not be double-emitted (its synthetic
tool.complete in turn.py stays the source).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.tools.message import MessageTool
from raven.contracts.tool import Tool
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


class _MetadataTool(_FakeTool):
    def take_metadata(self) -> dict[str, Any] | None:
        return {"raven_delivery": {"files": [{"name": "report.pdf"}]}}


def _make_agent(workspace: Path, responses: list[LLMResponse], *tools: Tool) -> AgentLoop:
    agent = AgentLoop(
        provider=_ScriptedProvider(responses),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=5),
        tools=ToolWiring(restrict_to_workspace=True),
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


async def test_tool_metadata_survives_when_no_live_event_consumer_is_attached(workspace) -> None:
    agent = _make_agent(workspace, _tool_then_final("deliver_files"), _MetadataTool("deliver_files"))

    _, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "deliver"}])

    tool_result = next(message for message in messages if message.get("role") == "tool")
    assert tool_result["_metadata"] == {"raven_delivery": {"files": [{"name": "report.pdf"}]}}


async def test_tool_start_carries_blocking_for_a_blocking_tool(workspace) -> None:
    """``tool.start`` must report the registry's blocking verdict.

    A consumer that streams a turn (the web channel) has no tool table of its
    own, so the flag has to ride the event or it would have to hard-code tool
    names — a second fact source that drifts the moment a tool is added.
    """
    tool = _FakeTool("run_subagent_dag")
    tool.blocking_interaction = True
    agent = _make_agent(workspace, _tool_then_final("run_subagent_dag"), tool)
    events: list[tuple[str, dict]] = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    await agent._run_agent_loop([{"role": "user", "content": "x"}], on_tool_event=on_tool_event)
    assert events[0][1]["blocking"] is True


async def test_tool_start_carries_blocking_false_for_a_plain_tool(workspace) -> None:
    agent = _make_agent(workspace, _tool_then_final("grep"), _FakeTool("grep"))
    events: list[tuple[str, dict]] = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    await agent._run_agent_loop([{"role": "user", "content": "x"}], on_tool_event=on_tool_event)
    assert events[0][1]["blocking"] is False


async def test_tool_complete_truncated_flag(workspace) -> None:
    from raven.agent.loop._shared import _TOOL_PREVIEW_MAX_CHARS

    over = "X" * (_TOOL_PREVIEW_MAX_CHARS + 100)
    tool = _FakeTool("grep", result=over)
    agent = _make_agent(workspace, _tool_then_final("grep", over), tool)
    events: list[tuple[str, dict]] = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    await agent._run_agent_loop(
        [{"role": "user", "content": "go"}],
        on_tool_event=on_tool_event,
    )
    complete = [i for p, i in events if p == "complete"][0]
    assert complete["truncated"] is True
    assert len(complete["result_preview"]) <= _TOOL_PREVIEW_MAX_CHARS


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


# ---------------------------------------------------------------------------
# MessageTool not double-emitted — its synthetic tool.complete
# in turn.py stays the single source; the general loop path skips it.
# ---------------------------------------------------------------------------


async def test_message_tool_skipped_by_general_path(workspace) -> None:
    async def _send(out) -> None:
        pass

    msg_tool = MessageTool(send_callback=_send)
    agent = _make_agent(
        workspace,
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="m1", name="message", arguments={"content": "hi"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ],
        msg_tool,
    )
    events: list = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    _, tools_used, _, _ = await agent._run_agent_loop(
        [{"role": "user", "content": "hi"}],
        on_tool_event=on_tool_event,
    )
    assert "message" in tools_used
    # turn.py owns the message tool's tool.complete; the general path skips it
    # to avoid a double-emit.
    assert events == [], f"message tool must not emit general tool events; got {events}"


async def test_tool_start_carries_blocking_through_a_tool_call_passthrough(workspace) -> None:
    """A blocking tool reached via the ``tool_call`` meta-tool must still be
    flagged, or a compacted catalog reintroduces the clocked-turn bug."""
    from raven.agent.tools.tool_search import ToolCallTool, ToolSearchController

    target = _FakeTool("run_subagent_dag")
    target.blocking_interaction = True
    agent = _make_agent(
        workspace,
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="tool_call",
                        arguments={"name": "run_subagent_dag", "arguments": {}},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="final", finish_reason="stop"),
        ],
        target,
    )
    agent.tools.register(ToolCallTool(ToolSearchController(agent.tools, always_visible=set())))
    events: list[tuple[str, dict]] = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    await agent._run_agent_loop([{"role": "user", "content": "x"}], on_tool_event=on_tool_event)

    assert events[0][1]["name"] == "tool_call"
    assert events[0][1]["blocking"] is True


async def test_tool_complete_wire_payload_carries_metadata() -> None:
    """The manifest must survive the one tool.complete serialization site, which
    the TUI and the web channel share."""
    from raven.spine.events import ToolEvent, ToolPhase

    emitted: list[dict] = []

    class _Emitter:
        async def emit(self, cid: str, frame: dict) -> None:
            emitted.append(frame)

    from raven.rpc.spine import RpcOutlet

    outlet = RpcOutlet("tui", _Emitter())
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="t1",
            result_preview="done",
            metadata={"raven_delivery": {"files": [], "invalid": []}},
        )
    )

    complete = [f for f in emitted if f["type"] == "tool.complete"]
    assert complete, f"expected a tool.complete frame, got {emitted}"
    assert complete[0]["payload"]["metadata"] == {"raven_delivery": {"files": [], "invalid": []}}


# ---------------------------------------------------------------------------
# The diff a write reports, and the one hop it has to make by hand.
# ---------------------------------------------------------------------------


async def test_tool_complete_carries_the_diff_a_write_produced(workspace) -> None:
    """The hop that was missing: the registry attaches the diff to the result,
    and only this event reaches a UI.

    Every other layer of this was built -- the tool produced a diff, the event
    declared a field for it, the outlet forwarded that field, the wire schema
    named it -- and nothing copied the one to the other, so the panel's Diff tab
    was empty for every whole-file write.
    """
    from raven.agent.tools.filesystem import WriteFileTool

    target = workspace / "letter.md"
    target.write_text("before\n", encoding="utf-8")
    agent = _make_agent(
        workspace,
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c-write",
                        name="write_file",
                        arguments={"path": str(target), "content": "after\n"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="final", finish_reason="stop"),
        ],
        WriteFileTool(workspace=workspace),
    )
    events: list[tuple[str, dict]] = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    await agent._run_agent_loop([{"role": "user", "content": "x"}], on_tool_event=on_tool_event)

    complete = [info for phase, info in events if phase == "complete"][0]
    assert complete["diff"] is not None, "the diff never left the registry"
    assert "-before" in complete["diff"] and "+after" in complete["diff"], complete["diff"]


async def test_tool_complete_diff_is_absent_for_a_tool_that_changes_nothing(workspace) -> None:
    """A field present on every event is a field a client stops checking."""
    agent = _make_agent(workspace, _tool_then_final("grep"), _FakeTool("grep"))
    events: list[tuple[str, dict]] = []

    async def on_tool_event(phase: str, info: dict) -> None:
        events.append((phase, info))

    await agent._run_agent_loop([{"role": "user", "content": "x"}], on_tool_event=on_tool_event)

    assert [info for phase, info in events if phase == "complete"][0]["diff"] is None
