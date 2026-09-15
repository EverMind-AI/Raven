"""An A2A task becomes one raven turn.

The two methods the SDK asks for. Everything else about the protocol -- the
task store, the event queue, the eleven request-handler methods -- is
``DefaultRequestHandler``'s.

The failure path is a trust boundary: a turn's exception can carry file paths,
prompt fragments, tool output and credentials, and the A2A caller is in another
trust domain. So a raised turn puts a fixed sentence on the wire and the real
cause in this host's log -- never the traceback, never the exception's own
message.

A turn that asks a question does not suspend either: ``self._run_turn(prompt)``
stays a live coroutine awaiting a future inside its task's ``A2aQuestionBroker``,
while the SDK's own producer/consumer split already runs ``execute`` in the
background and lets ``on_message_send`` return as soon as it sees the resulting
``INPUT_REQUIRED`` status. A later ``SendMessage`` against the same task id
resolves that future through ``answer`` instead of starting a second turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus, TaskStatusUpdateEvent
from loguru import logger

from raven.a2a.asking import A2aQuestionBroker
from raven.a2a.lifecycle import task_state_for

TURN_FAILED_MESSAGE = "The agent turn failed. Ask the operator of this agent to check its logs."


class RavenAgentExecutor(AgentExecutor):
    """Runs one inbound A2A task as one raven turn.

    `run_turn` is injected rather than imported, so this module has no
    dependency on how a turn is built, and its tests need no runtime.
    """

    def __init__(self, run_turn: Callable[[str], Awaitable[str]]) -> None:
        self._run_turn = run_turn
        # One broker per in-flight task, so a later `SendMessage` naming that
        # task id can reach the turn that is still parked waiting on it.
        self._brokers: dict[str, A2aQuestionBroker] = {}
        # Holds the on-park status-update task so it survives GC: the loop only
        # keeps a weak reference to a task nothing else points at.
        self._background: set[asyncio.Task[None]] = set()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run one turn for `context`'s task, reporting working then a terminal state."""
        prompt = context.get_user_input()
        # The SDK's consumer rejects a TaskStatusUpdateEvent for a task it has never
        # seen a Task object for, so a brand-new task needs this bare Task first.
        await event_queue.enqueue_event(self._initial_task(context))
        await event_queue.enqueue_event(self._status(context, "running"))

        def on_park(_task_id: str) -> None:
            task = asyncio.create_task(event_queue.enqueue_event(self._status(context, "question")))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

        self._brokers[context.task_id] = A2aQuestionBroker(on_park=on_park)
        try:
            answer = await self._run_turn(prompt)
        except Exception:
            logger.opt(exception=True).error("a2a turn failed for task {}", context.task_id)
            await event_queue.enqueue_event(self._status(context, "failed", TURN_FAILED_MESSAGE))
            return
        finally:
            self._brokers.pop(context.task_id, None)
        await event_queue.enqueue_event(self._status(context, "done", answer))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Report `context`'s task as cancelled; there is no in-flight turn to stop here."""
        logger.info("a2a task {} cancelled by the caller", context.task_id)
        self._brokers.pop(context.task_id, None)
        await event_queue.enqueue_event(self._status(context, "cancelled"))

    def answer(self, task_id: str, text: str) -> bool:
        """Resolve the question `task_id`'s turn is waiting on. False if none is parked."""
        broker = self._brokers.get(task_id)
        if broker is None:
            return False
        return broker.answer(task_id, text)

    def _initial_task(self, context: RequestContext) -> Task:
        """The bare submitted-state Task a new task must exist as before any status update."""
        return Task(
            id=context.task_id,
            context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        )

    def _status(self, context: RequestContext, outcome: str, text: str = "") -> TaskStatusUpdateEvent:
        """One task-status event carrying `outcome`'s A2A state and optional message text."""
        message = Message(role=Role.ROLE_AGENT, parts=[Part(text=text)]) if text else None
        status = TaskStatus(state=task_state_for(outcome), message=message)
        return TaskStatusUpdateEvent(task_id=context.task_id, context_id=context.context_id, status=status)
