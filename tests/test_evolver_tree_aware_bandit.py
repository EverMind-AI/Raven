"""Pin the tree-aware bandit before the orchestrator starts calling it.

``evolver/scheduler/tree_aware_bandit.py`` ships as Option F.1 of the SOP, but
the loop does not call it yet (``docs/specs/self-evolution-loop-raven-mapping.md``
§6 ②), so nothing exercised its maths. These tests pin the contract the wiring
will rely on rather than a behaviour the loop observes today.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evolver.scheduler.tree_aware_bandit import TreeAwareTaskScheduler

TASKS = ["t1", "t2", "t3"]


def scheduler(*, lam: float = 0.7, explore: float = 1.0, seed: int | None = 42) -> TreeAwareTaskScheduler:
    return TreeAwareTaskScheduler(TASKS, ancestry_lambda=lam, exploration_weight=explore, rng_seed=seed)


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_an_empty_task_pool_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one task id"):
        TreeAwareTaskScheduler([])


@pytest.mark.parametrize("lam", [0.0, -0.1])
def test_a_non_positive_decay_is_rejected(lam: float) -> None:
    with pytest.raises(ValueError, match="ancestry_lambda must be positive"):
        TreeAwareTaskScheduler(TASKS, ancestry_lambda=lam)


def test_a_negative_exploration_weight_is_rejected() -> None:
    with pytest.raises(ValueError, match="exploration_weight must be non-negative"):
        TreeAwareTaskScheduler(TASKS, exploration_weight=-0.5)


def test_zero_exploration_weight_is_allowed() -> None:
    s = scheduler(explore=0.0)
    s.add_node("root", parent_id=None)
    # The bonus does not read the weight; the weight only scales it inside
    # the score, so a zero weight leaves the score at the variance alone.
    assert s.exploration_bonus("t1") == pytest.approx(1.0)
    assert s.score("t1", "root") == pytest.approx(0.25, abs=1e-6)


# ---------------------------------------------------------------------------
# topology
# ---------------------------------------------------------------------------


def test_a_node_can_only_be_registered_once() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    with pytest.raises(ValueError, match="already registered"):
        s.add_node("root", parent_id=None)


def test_a_parent_must_be_registered_before_its_child() -> None:
    s = scheduler()
    with pytest.raises(ValueError, match="not registered yet"):
        s.add_node("child", parent_id="missing-parent")


def test_a_second_root_is_allowed_and_keeps_the_forest_acyclic() -> None:
    s = scheduler()
    s.add_node("root-a", parent_id=None)
    s.add_node("root-b", parent_id=None)
    assert s.ancestry_distance("root-b", "root-a") is None
    assert s.n_nodes() == 2


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


def test_an_outcome_for_an_unregistered_node_is_rejected() -> None:
    s = scheduler()
    with pytest.raises(ValueError, match="not registered"):
        s.add_outcome("ghost", "t1", True)


def test_an_outcome_for_an_unregistered_task_is_rejected() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    with pytest.raises(ValueError, match="not in the registered task pool"):
        s.add_outcome("root", "not-a-task", True)


def test_repeated_outcomes_accumulate_as_multiple_attempts() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    for passed in (True, False, True):
        s.add_outcome("root", "t1", passed)
    assert s.n_observations() == 3
    p_hat, effective_n = s.weighted_posterior("t1", "root")
    # The posterior sums over attempts, so one node with three attempts
    # weighs 3 * w(0) -- see the module docstring.
    assert p_hat == pytest.approx(2 / 3)
    assert effective_n == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# ancestry
# ---------------------------------------------------------------------------


def test_ancestry_distance_counts_edges_including_zero_for_self() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    s.add_node("a", parent_id="root")
    s.add_node("b", parent_id="a")
    assert s.ancestry_distance("b", "b") == 0
    assert s.ancestry_distance("b", "a") == 1
    assert s.ancestry_distance("b", "root") == 2


def test_ancestry_distance_is_none_off_the_chain() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    s.add_node("a", parent_id="root")
    s.add_node("b", parent_id="root")
    assert s.ancestry_distance("a", "b") is None


def test_ancestors_are_listed_nearest_first() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    s.add_node("a", parent_id="root")
    s.add_node("b", parent_id="a")
    assert s.ancestors_with_distance("b") == [("b", 0), ("a", 1), ("root", 2)]
    assert s.ancestors_with_distance("b", include_self=False) == [("a", 1), ("root", 2)]


def test_ancestry_distance_rejects_an_unregistered_node() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    with pytest.raises(ValueError, match="not registered"):
        s.ancestry_distance("ghost", "root")
    with pytest.raises(ValueError, match="not registered"):
        s.ancestry_distance("root", "ghost")


def test_ancestors_with_distance_rejects_an_unregistered_node() -> None:
    s = scheduler()
    with pytest.raises(ValueError, match="not registered"):
        s.ancestors_with_distance("ghost")


# ---------------------------------------------------------------------------
# posterior
# ---------------------------------------------------------------------------


def test_a_cold_start_posterior_is_uninformative() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    s.add_node("a", parent_id="root")
    assert s.weighted_posterior("t1", "a") == (0.5, 0.0)


def test_a_cold_start_variance_is_the_bernoulli_maximum() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    assert s.variance_estimate("t1", "root") == pytest.approx(0.25)


def test_the_posterior_weights_nearer_ancestors_more() -> None:
    s = scheduler(lam=0.7)
    s.add_node("root", parent_id=None)
    s.add_node("a", parent_id="root")
    # root passed three times at distance 1 from "a"; "a" itself failed once.
    # Both nodes enter the sum once per attempt, so root weighs 3 * w(1).
    for passed in (True, True, True):
        s.add_outcome("root", "t1", passed)
    s.add_outcome("a", "t1", False)

    w_near = math.exp(-0.7 * 0)
    w_far = math.exp(-0.7 * 1)
    p_hat, effective_n = s.weighted_posterior("t1", "a")
    assert p_hat == pytest.approx((w_near * 0.0 + w_far * 3.0) / (w_near + 3 * w_far))
    assert effective_n == pytest.approx(w_near + 3 * w_far)


def test_variance_falls_once_the_posterior_polarises() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    assert s.variance_estimate("t1", "root") == pytest.approx(0.25)
    for _ in range(4):
        s.add_outcome("root", "t1", True)
    assert s.variance_estimate("t1", "root") == pytest.approx(0.0, abs=1e-12)


def test_a_sharper_decay_lets_the_nearest_ancestor_dominate() -> None:
    def posterior_with(lam: float) -> float:
        s = scheduler(lam=lam)
        s.add_node("root", parent_id=None)
        s.add_node("mid", parent_id="root")
        s.add_node("leaf", parent_id="mid")
        s.add_outcome("root", "t1", True)  # two edges from leaf
        s.add_outcome("mid", "t1", False)  # one edge from leaf
        return s.weighted_posterior("t1", "leaf")[0]

    # p_hat = w(2) / (w(1) + w(2)) = 1 / (1 + exp(lambda)), so a sharper kernel
    # discounts the distant pass and leaves the nearby fail in charge.
    assert posterior_with(0.1) == pytest.approx(1 / (1 + math.exp(0.1)))
    assert posterior_with(2.0) == pytest.approx(1 / (1 + math.exp(2.0)))
    assert posterior_with(0.1) > posterior_with(2.0)


def test_the_posterior_rejects_an_unregistered_task_or_node() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    with pytest.raises(ValueError, match="not in the registered task pool"):
        s.weighted_posterior("not-a-task", "root")
    with pytest.raises(ValueError, match="not registered"):
        s.weighted_posterior("t1", "ghost")


# ---------------------------------------------------------------------------
# exploration bonus and score
# ---------------------------------------------------------------------------


def test_the_exploration_bonus_falls_as_observations_accumulate() -> None:
    s = scheduler(explore=1.0)
    s.add_node("root", parent_id=None)
    assert s.exploration_bonus("t1") == pytest.approx(1.0)
    s.add_outcome("root", "t1", True)
    assert s.exploration_bonus("t1") == pytest.approx(1 / math.sqrt(2))
    s.add_outcome("root", "t1", True)
    assert s.exploration_bonus("t1") == pytest.approx(1 / math.sqrt(3))


def test_the_exploration_weight_scales_the_bonus_without_touching_variance() -> None:
    plain, eager = scheduler(explore=0.0), scheduler(explore=2.0)
    for s in (plain, eager):
        s.add_node("root", parent_id=None)
    assert plain.score("t1", "root") == pytest.approx(0.25, abs=1e-6)
    assert eager.score("t1", "root") == pytest.approx(0.25 + 2.0, abs=1e-6)


def test_an_unobserved_task_outranks_a_settled_one() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    for _ in range(5):
        s.add_outcome("root", "t1", True)
    assert s.score("t2", "root") > s.score("t1", "root")


def test_the_score_carries_a_bounded_seeded_jitter() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    base = s.variance_estimate("t1", "root") + 1.0 * s.exploration_bonus("t1")
    first, second = s.score("t1", "root"), s.score("t1", "root")
    assert 0.0 <= first - base < 1e-9
    assert 0.0 <= second - base < 1e-9
    # The RNG advances on every score, so the jitter is not frozen.
    assert first != second


def test_the_exploration_bonus_counts_observations_outside_the_ancestry_chain() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    s.add_node("a", parent_id="root")
    s.add_node("sibling", parent_id="root")
    s.add_outcome("sibling", "t1", True)
    # "sibling" is not an ancestor of "a", yet its observation still counts:
    # the bonus tracks the task, not the chain.
    assert s.exploration_bonus("t1") == pytest.approx(1 / math.sqrt(2))


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def test_choose_returns_at_most_k_tasks_from_the_pool() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    picked = s.choose("root", K=2)
    assert len(picked) == 2
    assert set(picked) <= set(TASKS)
    assert len(set(picked)) == len(picked)


def test_choose_with_a_non_positive_k_returns_nothing() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    assert s.choose("root", K=0) == []
    assert s.choose("root", K=-3) == []


def test_choose_rejects_an_unregistered_candidate() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    with pytest.raises(ValueError, match="not registered"):
        s.choose("ghost", K=1)


def test_choose_from_pool_rejects_an_unregistered_candidate() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    with pytest.raises(ValueError, match="not registered"):
        s.choose_from_pool("ghost", ["t1"], K=1)


def test_choose_honours_the_exclusion_set() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    picked = s.choose("root", K=2, exclude_tasks=["t1", "t2"])
    assert picked == ["t3"]


def test_choose_is_reproducible_from_the_same_seed() -> None:
    def run() -> list[str]:
        s = scheduler(seed=7)
        s.add_node("root", parent_id=None)
        s.add_node("a", parent_id="root")
        s.add_outcome("root", "t1", True)
        s.add_outcome("root", "t2", False)
        return [s.choose("a", K=3), s.choose("a", K=3)]

    assert run() == run()


def test_choose_from_pool_scores_in_registered_order() -> None:
    # Cold-start tasks tie exactly, so the seeded jitter decides the order.
    # With the full pool the two entry points must therefore agree:
    # choose_from_pool has to score tasks in registered order, not set order.
    pooled, chosen = scheduler(seed=42), scheduler(seed=42)
    for s in (pooled, chosen):
        s.add_node("root", parent_id=None)
    assert pooled.choose_from_pool("root", TASKS, K=3) == chosen.choose("root", K=3)


@pytest.mark.slow
def test_choose_from_pool_is_reproducible_under_different_hash_seeds() -> None:
    """A seeded scheduler rebuilds the same pool selection in a fresh process.

    The pool arrives as an iterable and is intersected into a set. If the
    score loop iterated that set, the jitter draw each task receives would
    follow string-hash order, which PYTHONHASHSEED changes per process: the
    same seed could pick a different task after a resume. Both calls below
    run in clean interpreters with different hash seeds.
    """
    script = """
from evolver.scheduler.tree_aware_bandit import TreeAwareTaskScheduler

TASKS = ["t1", "t2", "t3"]

def fresh():
    s = TreeAwareTaskScheduler(TASKS, rng_seed=42)
    s.add_node("root", parent_id=None)
    return s

print(",".join(fresh().choose("root", K=3)))
print(",".join(fresh().choose_from_pool("root", TASKS, K=3)))
"""
    repo_root = Path(__file__).resolve().parents[1]
    inherited = os.environ.get("PYTHONPATH")
    env = {
        **os.environ,
        "PYTHONPATH": str(repo_root) + (os.pathsep + inherited if inherited else ""),
    }
    runs: list[tuple[str, str]] = []
    for hash_seed in ("0", "1"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=repo_root,
            env={**env, "PYTHONHASHSEED": hash_seed},
        )
        assert result.returncode == 0, result.stderr
        chosen, pooled = result.stdout.splitlines()
        assert pooled == chosen, f"hash seed {hash_seed}: {result.stdout!r}"
        runs.append((chosen, pooled))
    assert runs[0] == runs[1]


def test_choose_from_pool_skips_tasks_the_scheduler_does_not_know() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    picked = s.choose_from_pool("root", ["t2", "unknown-task"], K=5)
    assert picked == ["t2"]


def test_choose_from_pool_returns_nothing_for_an_empty_or_unknown_pool() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    assert s.choose_from_pool("root", [], K=5) == []
    assert s.choose_from_pool("root", ["unknown-task"], K=5) == []


def test_choose_from_pool_with_a_non_positive_k_returns_nothing() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    assert s.choose_from_pool("root", ["t1"], K=0) == []
    assert s.choose_from_pool("root", ["t1"], K=-2) == []


def test_choose_from_pool_returns_the_whole_small_pool() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    picked = s.choose_from_pool("root", ["t1", "t3"], K=9)
    assert sorted(picked) == ["t1", "t3"]


# ---------------------------------------------------------------------------
# introspection
# ---------------------------------------------------------------------------


def test_the_repr_reports_the_scheduler_size() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    text = repr(s)
    assert "TreeAwareTaskScheduler" in text
    assert "nodes=1" in text
