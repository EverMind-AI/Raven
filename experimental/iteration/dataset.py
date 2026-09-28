"""A trial and evaluator in one: run every case through the worker, then score each answer.

The same cases run every round, so a later round's score measures training on them, not a general improvement: that
takes a frozen Harness on cases whose answers never reached the loop. Expected answers stay with the evaluator and
the Analyst; the Curator never receives them (see `experimental.iteration.run.answerless`).
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from .protocols import Exchange, Item, Sessions, Signal


@dataclass(frozen=True)
class Case:
    id: str
    input: str
    expected: str


def exact(actual: str, expected: str) -> bool:
    return actual.strip() == expected.strip()


def contains(actual: str, expected: str) -> bool:
    return expected.strip() in actual


class Dataset:
    """`score` returns a bool or a 0..1 number per case; `threshold` is the pass rate that satisfies this evaluator."""

    def __init__(
        self, cases, *, score: Callable[[str, str], bool | float] = exact, threshold: float = 1.0, name="dataset"
    ):
        self.cases = tuple(cases)
        if not self.cases or len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("a dataset needs at least one case and distinct case ids")
        if not 0 <= threshold <= 1:
            raise ValueError("threshold is a pass rate between 0 and 1")
        self.score, self.threshold, self.name = score, threshold, name

    async def run(self, worker) -> Sessions:
        """Each run starts fresh worker sessions, so a round measures the current Harness alone."""
        sessions, run_id = {}, uuid4().hex
        for case in self.cases:
            execution = await worker.run(case.input, session_key=f"case:{case.id}:{run_id}")
            sessions[f"case:{case.id}"] = [Exchange(case.input, execution)]
        return sessions

    async def evaluate(self, sessions: Sessions) -> Signal | None:
        items = []
        for case in self.cases:
            exchanges = sessions.get(f"case:{case.id}")
            if not exchanges:
                items.append(Item(case.id, "unknown", f"case:{case.id}", case.expected, "", "no session for this case"))
                continue
            actual = exchanges[-1].assistant
            value = self.score(actual, case.expected)
            result = ("pass" if value else "fail") if isinstance(value, bool) else float(value)
            items.append(Item(case.id, result, f"case:{case.id}", case.expected, actual))
        scores = [
            1.0 if item.result == "pass" else 0.0 if item.result in ("fail", "unknown") else item.result
            for item in items
        ]
        rate = sum(scores) / len(scores)
        return Signal(self.name, items=tuple(items), metrics={"pass_rate": rate}, satisfied=rate >= self.threshold)
