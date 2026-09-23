"""A turn that asks parks its task; a later message answers the waiting turn."""

import asyncio

from a2a.types import TaskState

from raven.a2a.asking import A2aQuestionBroker
from raven.agent.tools.ask_user import AskUserTool


async def test_await_question_parks_and_then_returns_the_answer():
    parked = []
    broker = A2aQuestionBroker(on_park=parked.append)

    waiting = asyncio.create_task(broker.await_question("task-1", prompt="which one?", timeout_s=5.0))
    await asyncio.sleep(0)
    assert parked == ["task-1"]
    assert not waiting.done()

    assert broker.answer("task-1", "the second one") is True
    assert await waiting == "the second one"


async def test_answering_an_unknown_task_reports_that_it_did_nothing():
    assert A2aQuestionBroker(on_park=lambda _: None).answer("nope", "hi") is False


async def test_a_timed_out_question_returns_the_default_and_unparks():
    broker = A2aQuestionBroker(on_park=lambda _: None)
    out = await broker.await_question("task-2", prompt="?", default="fallback", timeout_s=0.01)
    assert out == "fallback"
    assert broker.answer("task-2", "too late") is False


def test_parked_is_the_input_required_state():
    from raven.a2a.lifecycle import task_state_for

    assert task_state_for("question") == TaskState.TASK_STATE_INPUT_REQUIRED


async def test_the_ask_user_tool_asks_through_this_broker_with_every_flag_it_sends():
    """The tool hands the broker every field of the protocol, a multi-select flag
    included, so a responder that stopped short of the protocol failed the call
    before anything was parked."""
    parked = []
    broker = A2aQuestionBroker(on_park=parked.append)
    tool = AskUserTool(broker=broker, conversation_id="task-3", timeout_s=5.0)

    asking = asyncio.create_task(
        tool.execute(questions=[{"question": "Which days?", "options": ["mon", "tue"], "multi_select": True}])
    )
    await asyncio.sleep(0)
    assert parked == ["task-3"]

    assert broker.answer("task-3", "mon, tue") is True
    result = await asking
    assert result.display_text == "answered: mon, tue"
