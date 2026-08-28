"""A tool may declare that calling it ends the turn.

Measured 2026-08-06 on a real on-call round: the turn ran
``ops_tune_status`` -> ``ops_observe`` -> ``ops_check_later`` round and round,
about every twenty seconds, until it hit ``max_iterations`` (41 calls against a
cap of 40). Every call succeeded, so the failure-loop breaker never fired, and
"Scheduled a wake at ..." reads as a step that completed rather than one that
closes. The wake-dedup fix kept the cron store at one pending wake but did not
touch the spinning itself -- two defects that had looked like one.

The contract tested here: after a tool whose ``ends_turn`` is true, the next
model call is made with no tools, so the model can still write its closing
message but cannot start another step.
"""

from __future__ import annotations

import pytest

from raven.agent.tools.base import Tool


class _Waiter(Tool):
    @property
    def name(self) -> str:
        return "waiter"

    @property
    def description(self) -> str:
        return "schedules a wake and ends the turn"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def ends_turn(self) -> bool:
        return True

    async def execute(self, **kwargs) -> str:
        return "Scheduled a wake at ~later."


class _Plain(Tool):
    @property
    def name(self) -> str:
        return "plain"

    @property
    def description(self) -> str:
        return "does a thing"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "done"


def test_a_tool_does_not_end_the_turn_by_default():
    """Only tools that say so end the turn; everything else is unaffected."""
    assert _Plain().ends_turn is False


def test_a_waiting_tool_declares_that_it_ends_the_turn():
    assert _Waiter().ends_turn is True


def test_ops_check_later_is_such_a_tool():
    """The tool that means 'and now we wait' is the one that spun."""
    from raven.agent.tools.ops import OpsCheckLaterTool

    assert OpsCheckLaterTool(cron_service=None).ends_turn is True


def test_the_tools_that_keep_working_do_not():
    """A guard against a blanket application: submitting or observing leaves the
    turn open, because there is more to decide right after."""
    from raven.agent.tools.ops import OpsSubmitTool, OpsTuneStatusTool

    assert OpsSubmitTool(cron_service=None).ends_turn is False
    assert OpsTuneStatusTool().ends_turn is False


@pytest.mark.parametrize("declared,expected_second_call_tools", [(True, None), (False, "present")])
def test_the_loop_withholds_tools_after_such_a_call(declared, expected_second_call_tools):
    """The behaviour, read off the loop source rather than a live model: the flag
    is initialised, set from the executed tool, and consumed where tools are
    assembled for the next call. Asserted structurally because standing up a real
    AgentLoop needs a provider, a workspace and a session manager, and the thing
    worth protecting is that these three lines stay wired to each other.
    """
    import inspect

    from raven.agent.loop import main as loop_main

    src = inspect.getsource(loop_main.AgentLoop._run_agent_loop)
    assert "turn_closed_by_tool = False" in src
    assert 'getattr(self.tools.get(tool_call.name), "ends_turn", False)' in src
    assert "None if turn_closed_by_tool else self.tools.get_definitions()" in src


@pytest.mark.asyncio
async def test_one_call_may_say_it_did_not_wait_after_all(tmp_path):
    """The class default says "and now we wait"; a refused call did not wait.
    Closing the turn there leaves an ops campaign with nothing pending and nothing
    reported, so the call's own answer wins over the tool's default."""
    from raven.agent.tools.base import ToolResult
    from raven.agent.tools.registry import ToolRegistry

    class _RefusingWaiter(_Waiter):
        async def execute(self, **kwargs):
            return ToolResult("REFUSED: nothing was scheduled.", ends_turn=False)

    registry = ToolRegistry()
    registry.register(_RefusingWaiter())

    out = await registry.execute("waiter", {})

    assert out.ends_turn is False
    assert _RefusingWaiter().ends_turn is True


@pytest.mark.asyncio
async def test_the_second_call_is_made_with_no_tools(tmp_path):
    """The behaviour itself, driven through the real loop with a recording
    provider: first call asks for the waiting tool, and the call after it must
    arrive with no tools at all -- that is what stops another step."""
    from raven.agent.loop import AgentLoop
    from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    seen_tools: list[object] = []

    class _Recorder(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.calls = 0

        def get_default_model(self) -> str:
            return "stub"

        async def chat(self, messages, tools=None, model=None, **kw):
            seen_tools.append(tools)
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=[ToolCallRequest(id="1", name="waiter", arguments={})],
                )
            return LLMResponse(content="waiting for the wake", finish_reason="stop")

        async def chat_with_retry(self, messages, tools=None, model=None, **kw):
            return await self.chat(messages, tools=tools, model=model, **kw)

    loop = AgentLoop(provider=_Recorder(), workspace=tmp_path, model="stub", max_iterations=6)
    loop.tools.register(_Waiter())

    content, used, _msgs, _outcome = await loop._run_agent_loop(
        [{"role": "user", "content": "watch it"}]
    )

    assert used == ["waiter"], "the tool must actually have run"
    assert len(seen_tools) == 2, f"expected exactly two model calls, got {len(seen_tools)}"
    assert seen_tools[0], "the first call must offer tools"
    assert seen_tools[1] is None, "the call after a turn-ending tool must offer none"
    assert content == "waiting for the wake"
