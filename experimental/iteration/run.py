"""Run rounds of trial, evaluation, analysis and curation on one worker task."""

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

from ..analyst.feedback import Feedback
from ..analyst.role import Analyst
from ..analyst.run import Limits as AnalystLimits
from ..curator.generation.run import GenerationInterruptedError
from ..curator.generation.run import Limits as CuratorLimits
from ..curator.raven_adapter.materialize import _write
from ..curator.raven_adapter.observe import plain
from ..curator.workflow import PENDING, improve
from .protocols import Sessions, Signal


@dataclass(frozen=True)
class Limits:
    max_rounds: int = 3
    analyst: AnalystLimits = field(default_factory=AnalystLimits)
    curator: CuratorLimits = field(default_factory=CuratorLimits)

    def __post_init__(self):
        if not isinstance(self.max_rounds, int) or self.max_rounds < 1:
            raise ValueError("the round budget must be a positive integer")


@dataclass(frozen=True)
class Round:
    """One round, with the analysis and curation record files it produced under the worker root; `feedback` is None when the analysis failed."""

    sessions: Sessions
    signals: tuple[Signal, ...]
    feedback: Feedback | None
    curated: bool
    analysis: tuple[str, ...] = ()
    curation: tuple[str, ...] = ()


class _Records:
    """Name the record files a step adds to a directory, so rounds can be joined to them later."""

    def __init__(self, directory: Path, exclude=()):
        self.directory, self.exclude = directory, frozenset(exclude)
        self.seen = self._names()

    def _names(self):
        if not self.directory.is_dir():
            return set()
        return {path.name for path in self.directory.glob("*.json")} - self.exclude

    def added(self) -> tuple[str, ...]:
        current = self._names()
        new = tuple(sorted(current - self.seen))
        self.seen = current
        return new


def history_entry(number, signals, feedback, plan, activity=()) -> dict:
    """One round as later rounds see it: per-item results with the session that failed each, the requirements raised
    with how they would be accepted, what the mechanisms did, and the revision that followed with its expectations."""
    return {
        "round": number,
        "results": {
            signal.source: {item.id: item.result for item in signal.items} for signal in signals if signal.items
        },
        "failed_in": {
            signal.source: {item.id: item.session for item in signal.items if item.result == "fail" and item.session}
            for signal in signals
            if signal.items
        },
        "requirements": [
            {
                "behavior": requirement.behavior,
                "strength": requirement.strength,
                "acceptance": requirement.acceptance,
            }
            for requirement in (feedback.requirements if feedback else ())
        ],
        "mechanisms_acted": [row for row in activity if row.get("acted")],
        "revision": [
            {
                "target": change.target,
                "reason": change.reason,
                "expected": change.expected,
                "verification": change.verification,
            }
            for change in plan.changes
        ]
        if plan
        else None,
    }


def answerless(signals) -> tuple[Signal, ...]:
    """The signals as the Curator may see them: each item's verdict, session and the worker's own answer, without the
    reference an evaluator holds (`expected`) or its note on what should have been said. A reference that reaches
    the Curator is copied into the Harness and scores the same cases next round without the worker improving."""
    return tuple(
        replace(signal, items=tuple(replace(item, expected="", note="") for item in signal.items)) for signal in signals
    )


def satisfied(signals) -> bool:
    return bool(signals) and all(signal.satisfied is True for signal in signals)


async def trial_sessions(worker, trials) -> Sessions:
    sessions = {}
    for trial in trials:
        produced = await trial.run(worker)
        if sessions.keys() & produced.keys():
            raise ValueError(f"trials produced the same session key: {sorted(sessions.keys() & produced.keys())}")
        sessions.update(produced)
    return sessions


async def run(
    worker,
    provider,
    trials,
    analyst,
    *,
    curator=None,
    model=None,
    curator_model=None,
    limits=Limits(),
    probe=None,
    opening=(),
):
    """Curate once from the task, then iterate until every evaluator is satisfied, the analyst stops, or rounds run out.

    `opening` signals, such as an owner handing over its materials, reach that first curation as its feedback.

    The worker must be bound to its task. `analyst` reviews each round (an `experimental.analyst.role.Analyst`, or a
    sequence of evaluators for the base Analyst); a `curate` decision, or material handed over with a review, reaches
    the curator (workflow.improve unless another arm is supplied) with the signals stripped of their references (see
    `answerless`; or what the Analyst relays in their place) and what the mechanisms did (`mechanism_activity`).
    The last round's review is recorded but never curated: no later round would test that revision. The run record is
    rewritten after every step, so a reader can follow a run while it is going; a round is recorded as soon as it is
    analysed, before its curation. `status` ends as finished, paused (the Curator's call
    budget or model transport was interrupted and can be resumed) or error, and `stop` says why the run ended.
    """
    if worker.baseline.task is None:
        raise ValueError("iteration requires a worker bound to a current task")
    curator = improve if curator is None else curator
    if not hasattr(analyst, "review"):
        analyst = Analyst(tuple(analyst), provider, model=model, limits=limits.analyst)
    rounds, previous_signals, previous_feedback, history = [], (), None, []
    analyses, curations = (
        _Records(worker.root / "analysis"),
        _Records(worker.root / "curation", exclude={PENDING, "composition.json"}),
    )
    record = {
        "task_id": worker.baseline.task.id,
        "task": worker.baseline.task.text,
        "status": "running",
        "curator": curator.__name__,
        "opening": plain(tuple(opening)),
        "rounds": rounds,
    }
    path = worker.root / "iteration" / f"{uuid4().hex}.json"

    def save():
        _write(path, json.dumps(plain(record), ensure_ascii=False).encode())

    async def curate(feedback=None) -> bool:
        try:
            await curator(worker, provider, feedback=feedback, model=curator_model, limits=limits.curator, probe=probe)
        except GenerationInterruptedError as exc:
            record["status"], record["stop"] = "paused", str(exc)
            return False
        return True

    save()
    try:
        try:
            resumed = await curate({"signals": plain(tuple(opening))} if opening else None)
        finally:
            record["initial_curation"] = curations.added()
            save()
        if not resumed:
            return rounds
        if record["initial_curation"] and worker.last_plan is not None:
            history.append(history_entry(0, (), None, worker.last_plan))
        for _ in range(limits.max_rounds):
            sessions = await trial_sessions(worker, trials)
            signals = ()
            try:
                review = await analyst.review(
                    worker,
                    sessions,
                    previous_signals=previous_signals,
                    previous_feedback=previous_feedback,
                    history=tuple(history),
                )
                signals, feedback, acted = review.signals, review.feedback, review.activity
            except Exception as exc:
                signals = getattr(exc, "signals", signals)
                rounds.append(Round(sessions, signals, None, False, analyses.added()))
                raise
            wanted = feedback.decision == "curate" or bool(review.handover)
            curated = wanted and len(rounds) + 1 < limits.max_rounds
            rounds.append(Round(sessions, signals, feedback, curated, analyses.added()))
            previous_signals, previous_feedback = signals, feedback
            save()
            if feedback.decision == "stop":
                record["stop"] = "the analyst stopped the run"
                break
            if not curated:
                history.append(history_entry(len(rounds), signals, feedback, None, acted))
                if not signals:
                    record["stop"] = "no evaluator produced a signal"
                    break
                if satisfied(signals):
                    record["stop"] = "every evaluator is satisfied"
                    break
                continue
            try:
                resumed = await curate(
                    {
                        "signals": plain(answerless(signals) if review.relayed is None else review.relayed),
                        "history": list(history),
                        "mechanism_activity": list(acted),
                        **feedback.model_dump(mode="json"),
                    }
                )
            finally:
                rounds[-1] = replace(rounds[-1], curation=curations.added())
                save()
            history.append(
                history_entry(
                    len(rounds),
                    signals,
                    feedback,
                    worker.last_plan if rounds[-1].curation else None,
                    acted,
                )
            )
            save()
            if not resumed:
                return rounds
        else:
            record["stop"] = "rounds exhausted" + (
                "; the last review was not curated, no round would test it" if wanted else ""
            )
        record["status"] = "finished"
        return rounds
    except Exception as exc:
        record["status"], record["error"] = "error", str(exc)
        raise
    finally:
        save()
