"""Contracts between the iteration loop and whatever runs the worker or measures the result."""

from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..curator.raven_adapter.worker import Execution


@dataclass(frozen=True)
class Exchange:
    user: str
    execution: Execution

    @property
    def assistant(self) -> str:
        return self.execution.text


@dataclass(frozen=True)
class Item:
    """One measured item: a criterion, a dataset case or a session, with a verdict or a score."""

    id: str
    result: Literal["pass", "fail", "unknown"] | float
    session: str | None = None
    expected: str = ""
    actual: str = ""
    note: str = ""


@dataclass(frozen=True)
class Signal:
    """What one evaluator measured this round. `satisfied` is its own threshold verdict; None means it does not judge.

    `attachments` are files handed over with the text, as paths relative to the worker's agent home.
    """

    source: str
    text: str = ""
    items: tuple[Item, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)
    satisfied: bool | None = None
    attachments: tuple[str, ...] = ()


Sessions = dict[str, list[Exchange]]


class Trial(Protocol):
    """Whatever puts the worker through its paces: a conversation, a dataset, a simulation."""

    async def run(self, worker) -> Sessions: ...


class Evaluator(Protocol):
    """Whatever measures a round's sessions; None means nothing to say."""

    async def evaluate(self, sessions: Sessions) -> Signal | None: ...
