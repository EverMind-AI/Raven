"""The wiring that makes a session able to ask a question at all.

One function, tested on its own, because it is the seam where the feature can die
silently: delete the call and every other test in this package still passes while
``ask_user`` answers "not configured" and the turn proceeds on a guess. The unit
tests for the routes build their broker by hand and would not notice.
"""

from typing import Any

import pytest

from raven.acp.engine import bind_ask_user
from raven.acp.outbound import OutboundRequests
from raven.acp.questions import AcpQuestions


class _AskTool:
    def __init__(self) -> None:
        self.broker: Any = None

    def set_broker(self, broker: Any) -> None:
        self.broker = broker


class _Loop:
    def __init__(self, **tools: Any) -> None:
        self.tools = dict(tools)


def _questions() -> AcpQuestions:
    frames: list[dict] = []
    return AcpQuestions(outbound=OutboundRequests(frames.append), sessions=object(), emit=frames.append)


def test_the_session_s_ask_user_is_given_a_broker():
    tool = _AskTool()
    loop = _Loop(ask_user=tool)

    assert bind_ask_user(loop, _questions()) is True
    assert tool.broker is not None
    # The broker's own contract: this is what the tool call blocks on and what the
    # round trip resolves.
    assert hasattr(tool.broker, "await_question") and hasattr(tool.broker, "reply")


def test_each_session_gets_its_own_broker():
    """The broker keys pending questions by conversation id, and a session is one
    conversation. Sharing one across sessions would have a second session's
    question fail-safe the first one's."""
    first, second = _AskTool(), _AskTool()
    questions = _questions()

    bind_ask_user(_Loop(ask_user=first), questions)
    bind_ask_user(_Loop(ask_user=second), questions)

    assert first.broker is not second.broker


def test_no_questions_surface_means_no_broker_rather_than_a_crash():
    """The engine factory is a seam a test can drive without the outbound half."""
    tool = _AskTool()
    assert bind_ask_user(_Loop(ask_user=tool), None) is False
    assert tool.broker is None


def test_a_loop_without_the_tool_is_not_an_error():
    """``ask_user`` can be disabled by config; that is a choice, not a failure."""
    assert bind_ask_user(_Loop(), _questions()) is False
    assert bind_ask_user(_Loop(ask_user=object()), _questions()) is False


@pytest.mark.asyncio
async def test_the_bound_broker_reaches_the_questions_surface():
    """End of the wire: what the broker emits has to arrive at the object that
    knows how to ask the client, or the question goes nowhere."""
    questions = _questions()
    tool = _AskTool()
    bind_ask_user(_Loop(ask_user=tool), questions)

    taken: list[tuple] = []
    questions.handle = lambda broker, method, params: taken.append((method, params)) or True  # type: ignore[method-assign]
    await tool.broker._send_frame({"method": "clarify.request", "params": {"request_id": "q-1"}})

    assert taken and taken[0][0] == "clarify.request"
