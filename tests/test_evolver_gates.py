"""Unit tests for the gate arithmetic (raven.evolver.orchestrator.gates).

These protect the promotion decision itself: paired z statistics, the Fisher
exact test, the three-shield pipeline's narrowing rules, and the two concrete
gate policies. Wrong math here does not crash — it silently promotes or prunes
the wrong candidate — so the expected values below are hand-computed.
"""

from __future__ import annotations

import math

import pytest

from raven.evolver.orchestrator.gates.fisher import (
    fisher_one_sided,
    focused_counts,
    train_mean,
)
from raven.evolver.orchestrator.gates.paired import paired_lift
from raven.evolver.orchestrator.gates.pipeline import run_gates
from raven.evolver.orchestrator.gates.policy import Baseline, DecisionContext
from raven.evolver.orchestrator.gates.strategies import (
    FocusedFisherGate,
    PairedTwoSigmaGate,
    confirm_job_name,
)
from raven.evolver.orchestrator.scoring import TaskEval
from raven.evolver.scheduler.anchor_selection import AnchorSelection
from raven.evolver.tree.node import HarnessNode, NodeStatus


def _te(tid: str, passes: int, attempts: int, infra: int = 0) -> TaskEval:
    return TaskEval(task_id=tid, passes=passes, attempts=attempts, infra_attempts=infra)


def _evals(spec: dict[str, tuple[int, int]]) -> dict[str, TaskEval]:
    return {tid: _te(tid, p, a) for tid, (p, a) in spec.items()}


class TestPairedLift:
    def test_deterministic_win_is_inf_z(self):
        ids = ["t1", "t2", "t3", "t4"]
        r = paired_lift(
            candidate_evals=_evals({t: (3, 3) for t in ids}),
            control_evals=_evals({t: (0, 3) for t in ids}),
            task_ids=ids,
        )
        assert r.mean_lift == 1.0
        assert r.se == 0.0
        assert r.z == math.inf
        assert r.promoted and r.credited_2sigma

    def test_hand_computed_z(self):
        # diffs = [1, 1, 0, 0]: mean 0.5, stdev sqrt(1/3), se sqrt(1/3)/2,
        # z = 0.5 / (sqrt(1/3)/2) = sqrt(3) ~ 1.732 -> promoted, NOT credited.
        cand = _evals({"t1": (3, 3), "t2": (3, 3), "t3": (0, 3), "t4": (0, 3)})
        ctrl = _evals({t: (0, 3) for t in ("t1", "t2", "t3", "t4")})
        r = paired_lift(candidate_evals=cand, control_evals=ctrl, task_ids=["t1", "t2", "t3", "t4"])
        assert r.mean_lift == pytest.approx(0.5)
        assert r.z == pytest.approx(math.sqrt(3))
        assert r.promoted
        assert not r.credited_2sigma

    def test_zero_lift_is_not_promoted(self):
        ids = ["t1", "t2"]
        same = _evals({t: (1, 3) for t in ids})
        r = paired_lift(candidate_evals=same, control_evals=dict(same), task_ids=ids)
        assert r.z == 0.0
        assert not r.promoted and not r.credited_2sigma

    def test_deterministic_regression_is_minus_inf(self):
        ids = ["t1", "t2"]
        r = paired_lift(
            candidate_evals=_evals({t: (0, 3) for t in ids}),
            control_evals=_evals({t: (3, 3) for t in ids}),
            task_ids=ids,
        )
        assert r.z == -math.inf
        assert not r.promoted and not r.credited_2sigma

    def test_missing_task_scores_zero_for_that_arm(self):
        # Candidate never launched t2: it must score 0.0, not be dropped.
        cand = _evals({"t1": (3, 3)})
        ctrl = _evals({"t1": (0, 3), "t2": (3, 3)})
        r = paired_lift(candidate_evals=cand, control_evals=ctrl, task_ids=["t1", "t2"])
        assert r.candidate_mean == pytest.approx(0.5)
        assert r.control_mean == pytest.approx(0.5)
        assert not r.promoted  # tie, not a win

    def test_single_task_has_zero_se(self):
        r = paired_lift(
            candidate_evals=_evals({"t1": (3, 3)}),
            control_evals=_evals({"t1": (0, 3)}),
            task_ids=["t1"],
        )
        assert r.se == 0.0 and r.z == math.inf

    def test_empty_task_list_refused(self):
        with pytest.raises(ValueError, match="non-empty"):
            paired_lift(candidate_evals={}, control_evals={}, task_ids=[])


class TestFisher:
    def test_hand_computed_extreme(self):
        # [[3,0],[0,3]]: P(a=3) = C(3,3)*C(3,0)/C(6,3) = 1/20 = 0.05.
        assert fisher_one_sided(3, 0, 0, 3) == pytest.approx(0.05)

    def test_hand_computed_moderate(self):
        # [[8,2],[2,8]]: sum_{a=8..10} C(10,a)*C(10,10-a)/C(20,10)
        # = (45*45 + 10*10 + 1) / 184756 = 2126/184756.
        assert fisher_one_sided(8, 2, 2, 8) == pytest.approx(2126 / 184756)

    def test_candidate_worst_is_one(self):
        assert fisher_one_sided(0, 3, 3, 0) == pytest.approx(1.0)

    def test_degenerate_margins_return_one(self):
        assert fisher_one_sided(0, 0, 2, 1) == 1.0  # empty candidate row
        assert fisher_one_sided(2, 1, 0, 0) == 1.0  # empty control row
        assert fisher_one_sided(0, 3, 0, 3) == 1.0  # no passes anywhere
        assert fisher_one_sided(3, 0, 3, 0) == 1.0  # all passes everywhere

    def test_focused_counts_keeps_infra_as_fails(self):
        evals = {"t1": _te("t1", 1, 3, infra=2), "t2": _te("t2", 3, 3)}
        # t3 never launched: skipped here (train_mean owns the denominator).
        assert focused_counts(evals, ["t1", "t2", "t3"]) == (4, 2)

    def test_train_mean_fixed_denominator(self):
        evals = _evals({"t1": (3, 3)})
        # Missing t2 contributes 0.0 but stays in the denominator.
        assert train_mean(evals, ["t1", "t2"]) == pytest.approx(0.5)
        assert train_mean(evals, []) == 0.0


class TestRunGates:
    def test_infra_reported_but_not_dropped(self):
        ids = ["t1", "t2"]
        cand = {"t1": _te("t1", 3, 3, infra=1), "t2": _te("t2", 3, 3)}
        ctrl = _evals({t: (0, 3) for t in ids})
        g = run_gates(candidate_evals=cand, control_evals=ctrl, task_ids=ids)
        assert g.infra_contaminated == ["t1"]
        assert g.eligible_tasks == ids  # SOP 0: kept in the denominator
        assert g.promoted

    def test_gate_b_none_fails_open(self):
        ids = ["t1"]
        g = run_gates(
            candidate_evals=_evals({"t1": (3, 3)}),
            control_evals=_evals({"t1": (0, 3)}),
            task_ids=ids,
            fired_tasks=None,
        )
        assert g.unfired_excluded == [] and g.eligible_tasks == ids

    def test_gate_b_empty_set_leaves_nothing_and_refuses(self):
        ids = ["t1", "t2"]
        g = run_gates(
            candidate_evals=_evals({t: (3, 3) for t in ids}),
            control_evals=_evals({t: (0, 3) for t in ids}),
            task_ids=ids,
            fired_tasks=set(),
        )
        assert not g.promoted
        assert g.paired is None
        assert g.unfired_excluded == ids

    def test_gate_b_narrows_paired_to_fired_subset(self):
        cand = _evals({"t1": (3, 3), "t2": (0, 3)})
        ctrl = _evals({"t1": (0, 3), "t2": (0, 3)})
        g = run_gates(candidate_evals=cand, control_evals=ctrl, task_ids=["t1", "t2"], fired_tasks={"t1"})
        assert g.eligible_tasks == ["t1"]
        assert g.unfired_excluded == ["t2"]
        assert g.paired.n_tasks == 1 and g.promoted


class _FakeEval:
    """EvalFn returning canned evals keyed by job-name suffix; records calls."""

    def __init__(self, by_suffix: dict[str, dict[str, TaskEval]]):
        self.by_suffix = by_suffix
        self.calls: list[tuple[list[str], int, str]] = []

    def __call__(self, node, task_ids, k, job_name, *, split="train"):
        self.calls.append((list(task_ids), k, job_name))
        for suffix, evals in self.by_suffix.items():
            if job_name.endswith(suffix):
                return {t: ev for t, ev in evals.items() if t in task_ids}
        raise AssertionError(f"unexpected eval job {job_name!r}")


def _node(nid: str = "cand") -> HarnessNode:
    return HarnessNode(
        node_id=nid,
        parent_id="C0",
        git_commit_sha="0" * 40,
        git_branch="",
        created_at=HarnessNode.utc_now(),
        created_at_iter=1,
    )


def _ctx(eval_fn, baseline_evals, train_ids, **kw) -> DecisionContext:
    return DecisionContext(
        node=_node(),
        parent_id="C0",
        round_index=1,
        eval=eval_fn,
        baseline=Baseline(baseline_evals, train_mean(baseline_evals, train_ids), "vanilla"),
        train_task_ids=train_ids,
        **kw,
    )


class TestFocusedFisherGate:
    def test_promotes_on_full_train_lift(self):
        train = ["f1", "t2", "t3"]
        base = _evals({"f1": (0, 3), "t2": (3, 3), "t3": (0, 3)})  # mean 1/3
        fake = _FakeEval(
            {
                "_focused": _evals({"f1": (2, 3)}),
                "_confirm": _evals({"f1": (2, 3), "t2": (3, 3), "t3": (0, 3)}),  # mean 5/9
            }
        )
        out = FocusedFisherGate(k=3).decide(_ctx(fake, base, train, focused_task_ids=["f1"]))
        assert out.status == NodeStatus.promoted_to_baseline
        assert out.score == pytest.approx(5 / 9)
        assert out.stats["full_lift"] == pytest.approx(5 / 9 - 1 / 3)
        probe_ids, _, probe_job = fake.calls[0]
        assert probe_ids == ["f1"] and probe_job == "cand_focused"
        confirm_ids, _, confirm_job = fake.calls[1]
        assert confirm_ids == train and confirm_job == confirm_job_name("cand")

    def test_min_confirm_lift_prunes(self):
        train = ["f1", "t2", "t3"]
        base = _evals({"f1": (0, 3), "t2": (3, 3), "t3": (0, 3)})
        fake = _FakeEval(
            {
                "_focused": _evals({"f1": (2, 3)}),
                "_confirm": _evals({"f1": (2, 3), "t2": (3, 3), "t3": (0, 3)}),
            }
        )
        out = FocusedFisherGate(k=3, min_confirm_lift=0.5).decide(_ctx(fake, base, train, focused_task_ids=["f1"]))
        assert out.status == NodeStatus.pruned_at_confirm  # lift 2/9 < 0.5

    def test_stable_sentinel_regression_prunes_at_screen(self):
        train = ["s1", "s2", "t3"]
        base = _evals({"s1": (3, 3), "s2": (3, 3), "t3": (0, 3)})
        fake = _FakeEval({"_focused": _evals({"s1": (1, 3), "s2": (3, 3)})})
        # st_c = mean(1/3, 1) = 2/3 < 1.0 - guard(1.5/(2*3)=0.25) = 0.75 -> prune.
        out = FocusedFisherGate(k=3).decide(_ctx(fake, base, train, sentinel_task_ids=["s1", "s2"]))
        assert out.status == NodeStatus.pruned_at_screen
        assert out.stats["sentinel_regression"] is True
        assert len(fake.calls) == 1  # no confirm was paid for

    def test_fragile_sentinel_noise_is_tolerated(self):
        # A borderline (1/3) sentinel dipping to 0/3 is not significant
        # (fisher p = 0.5): wide-pass advances to confirm instead of pruning.
        train = ["s1", "t2"]
        base = _evals({"s1": (1, 3), "t2": (0, 3)})
        fake = _FakeEval(
            {
                "_focused": _evals({"s1": (0, 3)}),
                "_confirm": _evals({"s1": (1, 3), "t2": (2, 3)}),
            }
        )
        out = FocusedFisherGate(k=3).decide(_ctx(fake, base, train, sentinel_task_ids=["s1"]))
        assert out.status == NodeStatus.promoted_to_baseline
        assert out.stats["sent_fragile_p_worse"] == pytest.approx(0.5)

    def test_significantly_worse_probe_prunes_without_confirm(self):
        train = ["f1", "f2", "f3"]
        base = _evals({t: (3, 3) for t in train})
        fake = _FakeEval({"_focused": _evals({t: (0, 3) for t in train})})
        out = FocusedFisherGate(k=3).decide(_ctx(fake, base, train, focused_task_ids=train))
        assert out.status == NodeStatus.pruned_at_screen
        assert out.stats["pruned_significantly_worse"] is True
        # fisher_p_worse = P for [[9,0],[0,9]] = 1/C(18,9) = 1/48620.
        assert out.stats["fisher_p_worse"] == pytest.approx(1 / 48620)
        assert len(fake.calls) == 1


class TestPairedTwoSigmaGate:
    def _anchor(self, ids, cull=0.1):
        return AnchorSelection(task_ids=ids, sigma_screen=cull, cull_threshold=cull, tasks=[], shortfalls={})

    def test_requires_anchor(self):
        fake = _FakeEval({})
        with pytest.raises(ValueError, match="anchor"):
            PairedTwoSigmaGate().decide(_ctx(fake, {}, ["t1"]))

    def test_clear_screen_loss_prunes_before_confirm(self):
        train = ["a1", "a2", "t3"]
        base = _evals({"a1": (3, 3), "a2": (3, 3), "t3": (0, 3)})
        fake = _FakeEval({"_screen": _evals({"a1": (0, 1), "a2": (0, 1)})})
        out = PairedTwoSigmaGate().decide(_ctx(fake, base, train, anchor=self._anchor(["a1", "a2"])))
        assert out.status == NodeStatus.pruned_at_screen
        assert out.screen.bucket == "cull"
        assert len(fake.calls) == 1  # confirm never ran

    def test_fired_subset_cannot_promote_a_full_train_regression(self):
        # Gate-b narrows the paired stats to {t1} where the candidate wins,
        # but the full-train mean regresses (1/3 vs 2/3): the dual condition
        # must refuse promotion and report the FULL mean as the score.
        train = ["t1", "t2", "t3"]
        base = _evals({"t1": (0, 3), "t2": (3, 3), "t3": (3, 3)})  # mean 2/3
        fake = _FakeEval(
            {
                "_screen": _evals({"t2": (1, 1)}),  # within band -> advance
                "_confirm": _evals({"t1": (3, 3), "t2": (0, 3), "t3": (0, 3)}),  # mean 1/3
            }
        )
        out = PairedTwoSigmaGate().decide(
            _ctx(
                fake,
                base,
                train,
                anchor=self._anchor(["t2"]),
                fired_source=lambda node, ids: {"t1"},
            )
        )
        assert out.gate.paired.promoted  # the fired subset alone looks like a win
        assert out.status == NodeStatus.pruned_at_confirm
        assert out.score == pytest.approx(1 / 3)  # full-train mean, never the subset mean
        assert out.gate.unfired_excluded == ["t2", "t3"]
