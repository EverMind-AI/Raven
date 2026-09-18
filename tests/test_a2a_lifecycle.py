"""A raven turn's outcome, in A2A task-state terms."""

import pytest
from a2a.types import TaskState

from raven.a2a.lifecycle import task_state_for


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("running", TaskState.TASK_STATE_WORKING),
        ("done", TaskState.TASK_STATE_COMPLETED),
        ("failed", TaskState.TASK_STATE_FAILED),
        ("cancelled", TaskState.TASK_STATE_CANCELED),
        ("question", TaskState.TASK_STATE_INPUT_REQUIRED),
    ],
)
def test_each_outcome_maps_to_its_state(outcome, expected):
    assert task_state_for(outcome) == expected


def test_a_question_is_not_a_terminal_state():
    terminal = {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
    assert task_state_for("question") not in terminal


def test_an_unknown_outcome_raises_rather_than_guessing():
    with pytest.raises(ValueError, match="unknown turn outcome"):
        task_state_for("sideways")
