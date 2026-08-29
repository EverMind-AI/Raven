"""Refusing a tool call and ending the turn are two decisions.

One flag used to answer both, and the agent loop did three things with it: it
refused the call, it cancelled the sibling calls the model had already written
in the same response, and it ended the turn without asking the model again. The
first two belong together -- a refused operation must not be reachable through a
sibling the model wrote before it knew the answer -- but the third is a separate
judgement, and no refusal could be expressed without it.

`blocks_call` and `continuation` are those two questions. Every refusal still
asks for `ABORT_TURN`, so nothing here asserts a behaviour change; what these
pin is that the loop reads the second field instead of inferring it from the
first.
"""

from __future__ import annotations

import pytest

from raven.agent.loop import AgentLoop
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.contracts.tool import Continuation, Tool, ToolOutput, ToolResult
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


def test_a_refusal_says_what_should_happen_to_the_turn() -> None:
    """The two questions, asked separately."""
    refused = ToolResult(model_text="Error: blocked", blocks_call=True, continuation=Continuation.ABORT_TURN)

    assert refused.blocks_call is True
    assert refused.continuation is Continuation.ABORT_TURN


def test_an_ordinary_result_asks_for_neither() -> None:
    ordinary = ToolResult(model_text="done")

    assert ordinary.blocks_call is False
    assert ordinary.continuation is Continuation.CONTINUE


def test_a_call_can_be_refused_without_ending_the_turn() -> None:
    """The shape the second cut needs. Nothing produces it yet -- what matters
    here is that the type can express it, which one flag could not."""
    refused = ToolResult(model_text="Error: blocked", blocks_call=True, continuation=Continuation.CONTINUE)

    assert refused.blocks_call is True
    assert refused.continuation is Continuation.CONTINUE


def test_the_registry_carries_both_across_its_boundary() -> None:
    """`ToolOutput` is what the loop actually reads, and it is a `str` subclass
    -- so a field that is not copied across is silently absent rather than an
    error."""
    out = ToolOutput("Error: blocked", blocks_call=True, continuation=Continuation.ABORT_TURN)

    assert out.blocks_call is True
    assert out.continuation is Continuation.ABORT_TURN


@pytest.mark.parametrize("continuation", list(Continuation))
def test_every_continuation_survives_the_boundary(continuation: Continuation) -> None:
    """Parametrised over the enum rather than over two literals: a value added
    later is covered by this test on the day it is added."""
    out = ToolOutput("text", continuation=continuation)

    assert out.continuation is continuation


# -- and the loop that reads them ---------------------------------------------
#
# The tests above pin the type. They cannot fail if the loop ignores the
# continuation entirely, which is exactly the mistake this cut is trying not to
# make, so the decision is exercised here through `_process_message`.


class _Refuser(Tool):
    """A tool that refuses, saying separately what should become of the turn.

    No real tool asks for CONTINUE yet -- that is the second cut. A stub is how
    the loop's half of the contract gets tested before a producer exists.
    """

    def __init__(self, continuation: Continuation) -> None:
        self._continuation = continuation
        self.calls = 0

    @property
    def name(self) -> str:
        return "refuser"

    @property
    def description(self) -> str:
        return "refuses"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        return ToolResult(
            model_text="Error: policy stopped this one.",
            retryable=False,
            blocks_call=True,
            continuation=self._continuation,
        )


class _Provider:
    def __init__(self, *, parallel: bool = False) -> None:
        calls = [ToolCallRequest(id="call-a", name="refuser", arguments={})]
        if parallel:
            calls.append(ToolCallRequest(id="call-b", name="refuser", arguments={}))
        self.responses = [
            LLMResponse(content="", tool_calls=calls, finish_reason="tool_calls"),
            LLMResponse(content="Took another route.", finish_reason="stop"),
        ]

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "fake/model"


async def _run(agent: AgentLoop) -> str:
    result, _media = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
            text="do the thing",
            conversation="session-a",
        ),
        session_key="session-a",
    )
    return result


async def test_the_loop_ends_the_turn_when_the_refusal_asks_for_it(tmp_path) -> None:
    """Today's only behaviour, unchanged by the rename."""
    provider = _Provider()
    agent = AgentLoop(provider=provider, workspace=tmp_path, model="fake/model")
    agent.tools.register(_Refuser(Continuation.ABORT_TURN))

    result = await _run(agent)

    assert "no alternative method will be attempted" in result
    # The second response is still on the shelf: the model was never asked again.
    assert len(provider.responses) == 1


async def test_the_loop_asks_again_when_the_refusal_leaves_the_turn_alive(tmp_path) -> None:
    """The shape the second cut needs, read end to end.

    The call is still refused and its result still reaches the model. What does
    not happen is the runtime answering the user on the model's behalf.
    """
    provider = _Provider()
    agent = AgentLoop(provider=provider, workspace=tmp_path, model="fake/model")
    agent.tools.register(_Refuser(Continuation.CONTINUE))

    result = await _run(agent)

    assert result == "Took another route."
    assert provider.responses == []


async def test_a_blocked_call_cancels_its_siblings_even_when_the_turn_goes_on(tmp_path) -> None:
    """Blocking the call and ending the turn are separate, and the sibling
    cancellation belongs to the first one: a call the model wrote before it knew
    the answer must not run, whatever happens to the turn afterwards."""
    provider = _Provider(parallel=True)
    agent = AgentLoop(provider=provider, workspace=tmp_path, model="fake/model")
    tool = _Refuser(Continuation.CONTINUE)
    agent.tools.register(tool)

    result = await _run(agent)

    assert tool.calls == 1
    assert result == "Took another route."
