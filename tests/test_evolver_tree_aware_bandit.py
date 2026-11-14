"""Pin the tree-aware bandit before the orchestrator starts calling it.

``evolver/scheduler/tree_aware_bandit.py`` ships as Option F.1 of the SOP, but
the loop does not call it yet (``docs/specs/self-evolution-loop-raven-mapping.md``
§6 ②), so nothing exercised its maths. These tests pin the contract the wiring
will rely on rather than a behaviour the loop observes today.
"""

from __future__ import annotations

import math

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
    assert scheduler(explore=0.0).exploration_bonus("t1") > 0.0


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


def test_choose_from_pool_returns_the_whole_small_pool() -> None:
    s = scheduler()
    s.add_node("root", parent_id=None)
    picked = s.choose_from_pool("root", ["t1", "t3"], K=9)
    assert sorted(picked) == ["t1", "t3"]
