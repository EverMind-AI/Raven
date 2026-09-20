"""One raven turn, in A2A task-state terms.

The load-bearing row is ``question``. A turn can stop and ask (``AskUserTool``),
and over ACP that goes out as ``session/request_permission`` on the caller's own
wire. A2A models it natively: the task parks in ``INPUT_REQUIRED`` and the caller
resumes it with another message against the same task id. So an inbound question
parks the task instead of holding a request thread open against a caller that was
never asked to answer one.
"""

from __future__ import annotations

from a2a.types import TaskState

TURN_TO_TASK_STATE: dict[str, int] = {
    "running": TaskState.TASK_STATE_WORKING,
    "done": TaskState.TASK_STATE_COMPLETED,
    "failed": TaskState.TASK_STATE_FAILED,
    "cancelled": TaskState.TASK_STATE_CANCELED,
    "question": TaskState.TASK_STATE_INPUT_REQUIRED,
}


def task_state_for(outcome: str) -> int:
    """The A2A task state for a raven turn `outcome`."""
    try:
        return TURN_TO_TASK_STATE[outcome]
    except KeyError:
        raise ValueError(f"unknown turn outcome: {outcome!r}") from None
