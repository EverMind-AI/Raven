"""The Analyst role: read a round and answer with a `Review`, whose feedback decides what reaches the Curator.

The base class runs every evaluator over the round's sessions, then `experimental.analyst.run.analyse` reads their
signals against the sessions and the execution records and writes the feedback: a decision and requirements stated
in observable terms. A subclass may review a round another way, as long as it answers with a `Review`.
"""

import json
from dataclasses import dataclass, field
from uuid import uuid4

from ..curator.raven_adapter.materialize import _write
from ..curator.raven_adapter.observe import plain
from .activity import activity
from .feedback import Feedback
from .run import Limits, analyse


@dataclass(frozen=True)
class Review:
    """What the Analyst made of a round: the signals it measured, its feedback, and what the mechanisms did.

    `relayed` is what the Curator hears in place of the signals when the Analyst is itself the source (None: the
    signals, verbatim); `handover` names material handed over with this review, which reaches the Curator even when
    nothing needs revising.
    """

    signals: tuple
    feedback: Feedback
    activity: tuple[dict, ...] = field(default=())
    relayed: tuple | None = None
    handover: tuple[str, ...] = ()


class Analyst:
    """Reads a round and answers with a `Review`; only a `curate` decision, or material handed over with the review,
    reaches the Curator.

    `activity` (see `experimental.analyst.activity`) is attached to every review, counted from the records, so the
    Curator can check its own revision against what its mechanisms actually did.
    """

    def __init__(self, evaluators=(), provider=None, *, model=None, limits=Limits()):
        self.evaluators, self.provider, self.model, self.limits = tuple(evaluators), provider, model, limits

    async def review(self, worker, sessions, *, previous_signals=(), previous_feedback=None, history=()) -> Review:
        signals = []
        for evaluator in self.evaluators:
            signal = await evaluator.evaluate(sessions)
            if signal is not None:
                signals.append(signal)
        signals = tuple(signals)
        try:
            feedback = await analyse(
                worker,
                self.provider,
                signals,
                sessions,
                previous_signals=previous_signals,
                previous_feedback=previous_feedback,
                history=history,
                model=self.model,
                limits=self.limits,
            )
        except Exception as exc:
            # The iteration record keeps a failed round's measurements.
            exc.signals = signals
            raise
        return Review(signals, feedback, tuple(activity(sessions)))

    @staticmethod
    def record(worker, entry: dict) -> None:
        """Keep one review beside the worker's other records, in the form `analyse` writes."""
        _write(worker.root / "analysis" / f"{uuid4().hex}.json", json.dumps(plain(entry), ensure_ascii=False).encode())
