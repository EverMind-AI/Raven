"""The executor turns an A2A task into one raven turn, and leaks nothing when it raises."""

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
