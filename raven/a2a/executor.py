"""An A2A task becomes one raven turn.

The two methods the SDK asks for. Everything else about the protocol -- the
task store, the event queue, the eleven request-handler methods -- is
``DefaultRequestHandler``'s.

The failure path is a trust boundary: a turn's exception can carry file paths,
prompt fragments, tool output and credentials, and the A2A caller is in another
trust domain. So a raised turn puts a fixed sentence on the wire and the real
cause in this host's log -- never the traceback, never the exception's own
message.

A turn that asks a question does not suspend either: ``self._turn_runner(...)``
stays a live coroutine awaiting a future inside its task's ``A2aQuestionBroker``,
while the SDK's own producer/consumer split already runs ``execute`` in the
background and lets ``on_message_send`` return as soon as it sees the resulting
``INPUT_REQUIRED`` status. A later ``SendMessage`` against the same task id
resolves that future through ``answer`` instead of starting a second turn.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus, TaskStatusUpdateEvent
from loguru import logger

from raven.a2a.asking import A2aQuestionBroker
from raven.a2a.lifecycle import task_state_for
from raven.contracts.asking import QuestionResponder

TURN_FAILED_MESSAGE = "The agent turn failed. Ask the operator of this agent to check its logs."


class RunTurn(Protocol):
    """One inbound A2A prompt run as a raven turn.

    A plain `Callable[[str], Awaitable[str]]` alias cannot express the
    keyword-only `conversation_id`/`broker` parameters below, so this is a
    `Protocol` (same idiom as `raven/playbook/agent_profiles.py`'s
    `AgentProfileSource`) instead of a type alias.
    """

    async def __call__(self, prompt: str, *, conversation_id: str, broker: QuestionResponder | None) -> str: ...


class RavenAgentExecutor(AgentExecutor):
    """Runs one inbound A2A task as one raven turn.

    `run_turn` is injected rather than imported, so this module has no
    dependency on how a turn is built, and its tests need no runtime.
    """

    def __init__(self, run_turn: RunTurn) -> None:
        self._turn_runner = run_turn
        # ONE broker for every task, not one per task. `AskUserTool`'s broker is a
        # per-process slot (raven/agent/tools/ask_user.py) that the turn path
        # installs into, so a per-task broker is overwritten by the next task to
        # start -- and the earlier task's question then parks in an object
        # `answer` no longer looks in, so its resume silently does nothing and the
        # turn falls through to the ask's default. The broker keys its own waiters
        # by conversation id, so one instance serves concurrent tasks safely and
        # installing it repeatedly is a no-op.
        self._broker = A2aQuestionBroker(on_park=self._on_park)
        # conversation id -> the turn to report a park against. Keyed that way
        # because `on_park` is told the conversation id, being what the tool
        # parks under; `execute` owns the entry for its own turn's lifetime.
        self._turns: dict[str, tuple[RequestContext, EventQueue]] = {}
        # A2A's task id (caller-visible, used to resume) and raven's conversation
        # id (server-minted, what AskUserTool actually parks futures under) are
        # different strings; this maps the former to the latter for `answer`.
        self._conversation_ids: dict[str, str] = {}
        # Holds the on-park status-update task so it survives GC: the loop only
        # keeps a weak reference to a task nothing else points at.
        self._background: set[asyncio.Task[None]] = set()

    def _on_park(self, conversation_id: str) -> None:
        """Report the turn waiting on `conversation_id` as needing input."""
        turn = self._turns.get(conversation_id)
        if turn is None:
            return
        context, event_queue = turn
        task = asyncio.create_task(event_queue.enqueue_event(self._status(context, "question")))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run one turn for `context`'s task, reporting working then a terminal state."""
        prompt = context.get_user_input()
        # The SDK's consumer rejects a TaskStatusUpdateEvent for a task it has never
        # seen a Task object for, so a brand-new task needs this bare Task first.
        await event_queue.enqueue_event(self._initial_task(context))
        await event_queue.enqueue_event(self._status(context, "running"))

        # Minted here, not by run_turn: the executor owns the task-id ->
        # conversation-id mapping, so `answer` needs the value before the turn
        # (which never hands it back out) has even started.
        conversation_id = f"a2a:{uuid.uuid4()}"
        self._conversation_ids[context.task_id] = conversation_id
        self._turns[conversation_id] = (context, event_queue)
        try:
            answer = await self._turn_runner(prompt, conversation_id=conversation_id, broker=self._broker)
        except Exception:
            logger.opt(exception=True).error("a2a turn failed for task {}", context.task_id)
            await event_queue.enqueue_event(self._status(context, "failed", TURN_FAILED_MESSAGE))
            return
        finally:
            self._conversation_ids.pop(context.task_id, None)
            self._turns.pop(conversation_id, None)
        await event_queue.enqueue_event(self._status(context, "done", answer))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Report `context`'s task as cancelled; there is no in-flight turn to stop here."""
        logger.info("a2a task {} cancelled by the caller", context.task_id)
        if (conversation_id := self._conversation_ids.pop(context.task_id, None)) is not None:
            self._turns.pop(conversation_id, None)
        await event_queue.enqueue_event(self._status(context, "cancelled"))

    def answer(self, task_id: str, text: str) -> bool:
        """Resolve the question `task_id`'s turn is waiting on. False if none is parked."""
        conversation_id = self._conversation_ids.get(task_id)
        if conversation_id is None:
            return False
        # AskUserTool parks its future under the turn's conversation id, not the
        # caller-visible task id -- translate before resolving.
        return self._broker.answer(conversation_id, text)

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
