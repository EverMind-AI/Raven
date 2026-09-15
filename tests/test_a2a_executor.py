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

    async def run_turn(prompt, *, conversation_id, broker):
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

    async def run_turn(prompt, *, conversation_id, broker):
        raise RuntimeError("boom")

    queue = FakeQueue()
    await RavenAgentExecutor(run_turn).execute(FakeContext(), queue)
    assert queue.events[-1].status.state == TaskState.TASK_STATE_FAILED


async def test_a_raising_turn_does_not_put_the_traceback_on_the_wire():
    async def run_turn(prompt, *, conversation_id, broker):
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


async def test_the_conversation_id_reaches_run_turn_and_differs_from_the_task_id():
    """`execute` mints a raven conversation id per task and passes it to `run_turn`
    as a keyword -- that id, not the A2A task id, is what `AskUserTool` actually
    parks futures under (see `ask_user.py`'s `set_context`).
    """
    seen: dict[str, str] = {}

    async def run_turn(prompt, *, conversation_id, broker):
        seen["conversation_id"] = conversation_id
        return "ok"

    await RavenAgentExecutor(run_turn).execute(FakeContext(), FakeQueue())

    assert seen["conversation_id"], "run_turn must receive a non-empty conversation id"
    assert seen["conversation_id"] != "task-1"


async def test_a_turn_that_asks_parks_then_completes_once_answered():
    from a2a.types import TaskState

    async def run_turn(prompt, *, conversation_id, broker):
        # The broker and conversation id arrive as the same kwargs `execute`
        # injects into the real turn path -- nothing here reaches into the
        # executor's private bookkeeping to find them.
        choice = await broker.await_question(conversation_id, prompt="which one?")
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

    # `answer()` is keyed by the task id -- the id a caller resuming a task
    # actually has -- and translates it to the conversation id the broker
    # parked the future under.
    assert executor.answer("task-1", "the second one") is True
    await turn
    assert queue.events[-1].status.state == TaskState.TASK_STATE_COMPLETED
    assert queue.events[-1].status.message.parts[0].text == "chose: the second one"


async def test_answer_on_an_unknown_task_id_reports_that_it_did_nothing():
    async def run_turn(prompt, *, conversation_id, broker):
        return "unused"

    assert RavenAgentExecutor(run_turn).answer("no-such-task", "hi") is False


async def test_two_parked_tasks_are_tracked_and_answered_independently():
    """One broker serves both, keyed per conversation -- answering one
    concurrently-parked task must not resolve or evict the other one.
    """
    from a2a.types import TaskState

    async def run_turn(prompt, *, conversation_id, broker):
        choice = await broker.await_question(conversation_id, prompt="which one?")
        return f"chose: {choice}"

    executor = RavenAgentExecutor(run_turn)
    ctx_a, ctx_b = FakeContext("first"), FakeContext("second")
    ctx_a.task_id, ctx_b.task_id = "task-1", "task-2"
    queue_a, queue_b = FakeQueue(), FakeQueue()
    turn_a = asyncio.create_task(executor.execute(ctx_a, queue_a))
    turn_b = asyncio.create_task(executor.execute(ctx_b, queue_b))
    await _drain_until(lambda: len(queue_a.events) >= 3 and len(queue_b.events) >= 3)

    assert set(executor._conversation_ids) == {"task-1", "task-2"}

    assert executor.answer("task-1", "first answer") is True
    await turn_a
    assert queue_a.events[-1].status.state == TaskState.TASK_STATE_COMPLETED
    assert queue_a.events[-1].status.message.parts[0].text == "chose: first answer"
    # task-2 is untouched: still parked, still reachable, not swept away by
    # task-1's completion popping its own entry out of the shared dict.
    assert set(executor._conversation_ids) == {"task-2"}
    assert not turn_b.done()

    assert executor.answer("task-2", "second answer") is True
    await turn_b
    assert queue_b.events[-1].status.state == TaskState.TASK_STATE_COMPLETED
    assert queue_b.events[-1].status.message.parts[0].text == "chose: second answer"
    assert executor._conversation_ids == {}
    assert executor._turns == {}


async def test_every_task_is_handed_the_same_broker():
    """One broker instance for the whole executor, not one per task.

    `AskUserTool`'s broker is a per-process slot that the turn path installs
    into, so a per-task broker is overwritten by the next task to start, and the
    earlier task's question then parks in an object `answer` no longer looks in:
    its resume does nothing and the turn falls through to the ask's default.
    Measured before this invariant held -- injecting a single suspension between
    the install and the park made two concurrent tasks both answer "(no answer)".
    """
    seen = []

    async def run_turn(prompt, *, conversation_id, broker):
        seen.append(broker)
        return "ok"

    executor = RavenAgentExecutor(run_turn)
    second = FakeContext()
    second.task_id = "task-2"
    await executor.execute(FakeContext(), FakeQueue())
    await executor.execute(second, FakeQueue())

    assert len(seen) == 2
    assert seen[0] is seen[1], "a per-task broker loses the shared ask_user slot race"
