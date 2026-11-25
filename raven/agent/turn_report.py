"""Carry an ACP prompt's finish control through local turn submission."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from raven.spine.turn import TurnReport

REPORT_INSTRUCTION = (
    "Write your final answer now using the information already collected. "
    "State what you completed, what artifacts are available, and what remains unfinished."
)

_SUBMITTING: ContextVar[TurnReport | None] = ContextVar("raven_report_submission", default=None)


@contextmanager
def submission(report: TurnReport | None) -> Iterator[None]:
    """Give the in-process RPC call the control object for its own TurnRequest."""
    token = _SUBMITTING.set(report)
    try:
        yield
    finally:
        _SUBMITTING.reset(token)


def submitting() -> TurnReport | None:
    """The finish control attached to this local submission."""
    return _SUBMITTING.get()
