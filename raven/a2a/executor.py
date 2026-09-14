"""An A2A task becomes one raven turn.

The two methods the SDK asks for. Everything else about the protocol -- the
task store, the event queue, the eleven request-handler methods -- is
``DefaultRequestHandler``'s.

The failure path is a trust boundary: a turn's exception can carry file paths,
prompt fragments, tool output and credentials, and the A2A caller is in another
trust domain. So a raised turn puts a fixed sentence on the wire and the real
cause in this host's log -- never the traceback, never the exception's own
message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus, TaskStatusUpdateEvent
from loguru import logger

from raven.a2a.lifecycle import task_state_for

TURN_FAILED_MESSAGE = "The agent turn failed. Ask the operator of this agent to check its logs."


class RavenAgentExecutor(AgentExecutor):
    """Runs one inbound A2A task as one raven turn.

    `run_turn` is injected rather than imported, so this module has no
    dependency on how a turn is built, and its tests need no runtime.
    """

    def __init__(self, run_turn: Callable[[str], Awaitable[str]]) -> None:
        self._run_turn = run_turn

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run one turn for `context`'s task, reporting working then a terminal state."""
        prompt = context.get_user_input()
        # The SDK's consumer rejects a TaskStatusUpdateEvent for a task it has never
        # seen a Task object for, so a brand-new task needs this bare Task first.
        await event_queue.enqueue_event(self._initial_task(context))
        await event_queue.enqueue_event(self._status(context, "running"))
        try:
            answer = await self._run_turn(prompt)
        except Exception:
            logger.opt(exception=True).error("a2a turn failed for task {}", context.task_id)
            await event_queue.enqueue_event(self._status(context, "failed", TURN_FAILED_MESSAGE))
            return
        await event_queue.enqueue_event(self._status(context, "done", answer))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Report `context`'s task as cancelled; there is no in-flight turn to stop here."""
        logger.info("a2a task {} cancelled by the caller", context.task_id)
        await event_queue.enqueue_event(self._status(context, "cancelled"))

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
