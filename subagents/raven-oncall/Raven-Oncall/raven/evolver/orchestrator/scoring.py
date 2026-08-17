"""Bench-neutral scoring contract — the one currency the funnel scores in.

Every consumer in the seven-step funnel (screen, paired gate, sealed ledger)
reads only :attr:`TaskEval.pass_rate`, so a *scorer's* sole job is to turn one
evaluation of a candidate into ``{task_id: TaskEval}``. That makes the whole
benchmark surface collapse to a single value — :class:`EvalBackend` — bundling
the four bench-specific things the orchestrator needs: how to score a task set
(``eval``), the vanilla cold-start baseline over train (``cold_start``), the
screen anchor drawn from train (``anchor``), and the trajectories that feed
diagnosis (``trajectories``). SWE-bench, AppWorld, and the no-benchmark LLM
judge are each one :class:`EvalBackend` instance; nothing above this module
imports a concrete bench.

Train vs. sealed test is first-class here (SOP's iron law, as a mechanism not a
rule): ``EvalBackend`` carries both ``train_task_ids`` (the evolvable pool —
diagnosis, screen, confirm, gate, parent selection all live here) and
``test_task_ids`` (held-out; blind-scored only, never consulted for any
decision). ``eval`` takes an explicit ``split`` so one scorer serves both: the
funnel passes ``split="train"``, the sealed runner ``split="test"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Protocol

from raven.evolver.analysis.stability_bucket import TaskStability
from raven.evolver.scheduler.anchor_selection import AnchorSelection

if TYPE_CHECKING:
    from raven.evolver.tree.node import HarnessNode


@dataclass(frozen=True)
class TaskEval:
    """Per-task K-trial outcome — the universal currency the funnel scores in.

    ``infra_attempts`` is a subset of ``attempts`` (infra trials still count as
    non-passes here); Gate-f uses it to exclude infra-contaminated tasks. Benches
    that do not track infra leave it 0, so Gate-f is a no-op for them.
    """

    task_id: str
    passes: int
    attempts: int
    infra_attempts: int = 0

    @property
    def pass_rate(self) -> float:
        """Fraction of trials that passed; 0.0 when no trial was observed."""
        if self.attempts == 0:
            return 0.0
        return self.passes / self.attempts


class EvalFn(Protocol):
    """Score ``node`` on ``task_ids`` at K trials; return per-task outcomes.

    ``split`` selects the dataset partition — ``"train"`` for the funnel's
    screen/confirm, ``"test"`` for the sealed held-out runner. A task that never
    launched is simply absent from the returned mapping (the caller decides
    whether that is an infra failure per Gate-f).
    """

    def __call__(
        self,
        node: "HarnessNode",
        task_ids: list[str],
        k: int,
        job_name: str,
        *,
        split: str = "train",
    ) -> dict[str, TaskEval]: ...


# round-scoped trajectory feed for diagnosis:
# (round_index, parent) -> [(trajectory_id, task_description, trajectory_text), ...]
TrajectorySource = Callable[[int, "HarnessNode"], list[tuple[str, str, str]]]

# Pre-scoring environment health precheck (SOP §0 Gate0, before any scoring). Raises when
# the environment is dirty (sandbox down / network unroutable / verifier can't
# emit results) so a run does not score against a broken box. Bench-specific —
# each bench validates what it needs; left None when a bench has none wired.
PrecheckFn = Callable[[], None]


def eval_with_infra_rerun(
    eval_fn: "EvalFn",
    node: "HarnessNode",
    task_ids: list[str],
    k: int,
    job_name: str,
    *,
    split: str = "train",
    max_reruns: int = 2,
) -> dict[str, TaskEval]:
    """Wrap an ``eval_fn`` with the SOP §0 infra rerun ladder (detect -> rerun).

    After the first eval, any task that still carries an infra trial — or that
    was requested but came back with NO measurement at all (never launched /
    result never written: infra by definition) — is re-scored (fresh, at K) up
    to ``max_reruns`` times — re-run once the environment is healthy, topping the task back up to K attempts — and the measurement
    with the fewest infra trials is kept. This salvages recoverable (transient)
    infra so the task is measured for real; genuinely persistent infra survives
    all reruns and is left to score 0 in the denominator (never dropped — that
    is the caller's fixed-denominator discipline, SOP §0). A bench with no
    infra signal that returns every requested task triggers no rerun, so this
    is a transparent no-op there.
    """
    evals = dict(eval_fn(node, task_ids, k, job_name, split=split))
    for i in range(1, max_reruns + 1):
        infra_tasks = [t for t in task_ids if (ev := evals.get(t)) is None or ev.infra_attempts > 0]
        if not infra_tasks:
            break
        redo = eval_fn(node, infra_tasks, k, f"{job_name}_infra_rerun{i}", split=split)
        for t in infra_tasks:
            new = redo.get(t)
            old = evals.get(t)
            if new is not None and (old is None or new.infra_attempts < old.infra_attempts):
                evals[t] = new
    return evals


def with_infra_rerun(inner: "EvalFn", max_reruns: int) -> "EvalFn":
    """Wrap an ``EvalFn`` with the SOP §0 infra rerun ladder.

    A no-op when ``max_reruns <= 0`` or the bench emits no infra signal (every
    requested task comes back with ``infra_attempts == 0``), so a bench with no
    infra concept is unaffected. Benches compose this around their raw scorer
    when building an :class:`EvalBackend`.
    """
    if max_reruns <= 0:
        return inner

    def wrapped(node, task_ids, k, job_name, *, split="train"):
        return eval_with_infra_rerun(inner, node, task_ids, k, job_name, split=split, max_reruns=max_reruns)

    return wrapped


def flip_summary(
    candidate_evals: dict[str, TaskEval],
    control_evals: dict[str, TaskEval],
    task_ids: list[str],
    *,
    max_ids: int = 12,
) -> dict:
    """Per-task flip table between a candidate and its control (SOP §2 ①).

    ``rescued`` = tasks whose pass rate ROSE under the candidate (including
    partial, e.g. 1/3 -> 2/3), ``regressed`` = tasks whose rate fell,
    ``still_failing`` = tasks below a perfect rate under the candidate. The
    id lists are capped at ``max_ids`` (counts are always exact) — this feeds
    prompts and ledgers, not statistics; the paired gate owns the stats. A
    task absent from an arm scores 0.0 for that arm, same as the gate.
    """
    rescued: list[str] = []
    regressed: list[str] = []
    still_failing: list[str] = []
    for t in task_ids:
        c_ev, v_ev = candidate_evals.get(t), control_evals.get(t)
        c = c_ev.pass_rate if c_ev is not None else 0.0
        v = v_ev.pass_rate if v_ev is not None else 0.0
        if c > v:
            rescued.append(t)
        elif c < v:
            regressed.append(t)
        if c < 1.0:
            still_failing.append(t)
    return {
        "n_rescued": len(rescued),
        "n_regressed": len(regressed),
        "n_still_failing": len(still_failing),
        "rescued": rescued[:max_ids],
        "regressed": regressed[:max_ids],
        "still_failing": still_failing[:max_ids],
    }


def anchor_mean_pass_rate(evals: dict[str, TaskEval], anchor_task_ids: list[str]) -> float:
    """Mean per-task pass rate over the anchor subset.

    The quantity the screen compares against vanilla's anchor mean. Anchor tasks
    with no observed trial contribute 0.0 (a candidate that failed to even launch
    on an anchor task is not rewarded for the gap).
    """
    if not anchor_task_ids:
        raise ValueError("anchor_mean_pass_rate requires a non-empty anchor list")
    total = 0.0
    for task_id in anchor_task_ids:
        ev = evals.get(task_id)
        total += ev.pass_rate if ev is not None else 0.0
    return total / len(anchor_task_ids)


@dataclass(frozen=True)
class EvalBackend:
    """Everything bench-specific the funnel needs, in one value.

    One instance per benchmark (SWE-bench, AppWorld) or per no-benchmark LLM
    judge. The FSM stays bench-agnostic: it screens/confirms via ``eval`` on
    ``train_task_ids``, seeds the baseline from ``cold_start``, draws the screen
    anchor from ``anchor``, and feeds ``trajectories`` to diagnosis.
    ``test_task_ids`` is scored blind only, by the sealed runner.
    """

    train_task_ids: list[str]
    test_task_ids: list[str]
    eval: EvalFn
    cold_start: Callable[[], dict[str, TaskStability]]
    anchor: Callable[..., AnchorSelection]
    trajectories: Optional[TrajectorySource] = None
    precheck: Optional[PrecheckFn] = None


__all__ = [
    "TaskEval",
    "EvalFn",
    "TrajectorySource",
    "PrecheckFn",
    "EvalBackend",
    "anchor_mean_pass_rate",
    "eval_with_infra_rerun",
    "flip_summary",
    "with_infra_rerun",
]
