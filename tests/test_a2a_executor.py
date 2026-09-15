"""The executor turns an A2A task into one raven turn, and leaks nothing when it raises."""

import asyncio

from raven.a2a.executor import RavenAgentExecutor


class FakeQueue:
    """Stands in for EventQueue. `enqueue_event` is a coroutine on the real one."""

    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class FakeContext:
    """Stands in for RequestContext, which exposes get_user_input(), not an attribute."""

    def __init__(self, text="do the thing"):
        self._text = text
        self.task_id = "task-1"
        self.context_id = "ctx-1"

    def get_user_input(self, delimiter="\n"):
        return self._text


async def test_a_completed_turn_enqueues_its_answer():
    from a2a.types import Task, TaskState

    async def run_turn(prompt):
        assert prompt == "do the thing"
        return "the answer"

    queue = FakeQueue()
    await RavenAgentExecutor(run_turn).execute(FakeContext(), queue)
    # The SDK's task consumer rejects a status update for a task it has never seen a
    # Task object for, so the first event must be a bare submitted-state Task.
    assert isinstance(queue.events[0], Task)
    states = [e.status.state for e in queue.events]
    assert states == [
        TaskState.TASK_STATE_SUBMITTED,
        TaskState.TASK_STATE_WORKING,
        TaskState.TASK_STATE_COMPLETED,
    ]
    assert queue.events[-1].status.message.parts[0].text == "the answer"


async def test_a_failed_turn_reports_the_failed_state():
    from a2a.types import TaskState

    async def run_turn(prompt):
        raise RuntimeError("boom")

    queue = FakeQueue()
    await RavenAgentExecutor(run_turn).execute(FakeContext(), queue)
    assert queue.events[-1].status.state == TaskState.TASK_STATE_FAILED


async def test_a_raising_turn_does_not_put_the_traceback_on_the_wire():
    async def run_turn(prompt):
        raise RuntimeError("/srv/secret/path.py exploded with API_KEY=abc123")

    queue = FakeQueue()
    await RavenAgentExecutor(run_turn).execute(FakeContext(), queue)
    wire = " ".join(str(e) for e in queue.events)
    assert "/srv/secret/path.py" not in wire
    assert "abc123" not in wire
    assert "Traceback" not in wire


async def _drain_until(condition, attempts=50):
    """Yield to the loop until `condition()` holds, or fail after `attempts` ticks.

    The "question" status is enqueued from a task scheduled inside `on_park`
    (see executor.py), not awaited directly by `execute`, so a test cannot
    assume it has landed after a fixed number of bare `asyncio.sleep(0)` calls.
    """
    for _ in range(attempts):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def test_a_turn_that_asks_parks_then_completes_once_answered():
    from a2a.types import TaskState

    async def run_turn(prompt):
        # No real ask_user wiring exists yet for A2A (see executor.py's module
        # docstring); this stand-in reaches the broker the way a future tool
        # integration would, keyed by the same task id FakeContext always uses.
        broker = executor._brokers["task-1"]
        choice = await broker.await_question("task-1", prompt="which one?")
        return f"chose: {choice}"

    executor = RavenAgentExecutor(run_turn)
    queue = FakeQueue()
    turn = asyncio.create_task(executor.execute(FakeContext(), queue))
    await _drain_until(lambda: len(queue.events) >= 3)

    states = [e.status.state for e in queue.events]
    assert states == [
        TaskState.TASK_STATE_SUBMITTED,
        TaskState.TASK_STATE_WORKING,
        TaskState.TASK_STATE_INPUT_REQUIRED,
    ]
    assert not turn.done()

    assert executor.answer("task-1", "the second one") is True
    await turn
    assert queue.events[-1].status.state == TaskState.TASK_STATE_COMPLETED
    assert queue.events[-1].status.message.parts[0].text == "chose: the second one"


async def test_answer_on_an_unknown_task_id_reports_that_it_did_nothing():
    async def run_turn(prompt):
        return "unused"

    assert RavenAgentExecutor(run_turn).answer("no-such-task", "hi") is False
