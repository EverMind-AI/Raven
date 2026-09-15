"""A turn that asks parks its task; a later message answers the waiting turn."""

import asyncio

import pytest
from a2a.types import TaskState

from raven.a2a.asking import A2aQuestionBroker


async def test_await_question_parks_and_then_returns_the_answer():
    parked = []
    broker = A2aQuestionBroker(on_park=parked.append)

    waiting = asyncio.create_task(
        broker.await_question("task-1", prompt="which one?", timeout_s=5.0)
    )
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
