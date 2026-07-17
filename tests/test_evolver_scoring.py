"""Unit tests for the scoring currency and the SOP 0 infra-rerun ladder.

The ladder decides which measurements are salvaged and which score 0 in a
fixed denominator; a bug here hands every candidate a free lift (or silently
throws away good trials), so the rerun/keep rules are pinned down exactly.
"""

from __future__ import annotations

import pytest

from raven.evolver.orchestrator.scoring import (
    TaskEval,
    anchor_mean_pass_rate,
    eval_with_infra_rerun,
    flip_summary,
    with_infra_rerun,
)


def _te(tid, passes, attempts, infra=0):
    return TaskEval(task_id=tid, passes=passes, attempts=attempts, infra_attempts=infra)


class _ScriptedEval:
    """EvalFn replaying a scripted list of result maps; records every call."""

    def __init__(self, results: list[dict[str, TaskEval]]):
        self.results = list(results)
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, node, task_ids, k, job_name, *, split="train"):
        self.calls.append((list(task_ids), job_name))
        return self.results.pop(0)


class TestTaskEval:
    def test_pass_rate(self):
        assert _te("t", 2, 3).pass_rate == pytest.approx(2 / 3)
        assert _te("t", 0, 0).pass_rate == 0.0

    def test_anchor_mean_missing_scores_zero(self):
        assert anchor_mean_pass_rate({"t1": _te("t1", 3, 3)}, ["t1", "t2"]) == 0.5
        with pytest.raises(ValueError, match="non-empty"):
            anchor_mean_pass_rate({}, [])


class TestInfraRerunLadder:
    def test_clean_eval_triggers_no_rerun(self):
        fake = _ScriptedEval([{"t1": _te("t1", 1, 3), "t2": _te("t2", 3, 3)}])
        out = eval_with_infra_rerun(fake, None, ["t1", "t2"], 3, "job")
        assert len(fake.calls) == 1
        assert out["t1"].passes == 1

    def test_infra_task_rerun_and_salvaged(self):
        fake = _ScriptedEval([
            {"t1": _te("t1", 0, 3, infra=2), "t2": _te("t2", 3, 3)},
            {"t1": _te("t1", 2, 3, infra=0)},
        ])
        out = eval_with_infra_rerun(fake, None, ["t1", "t2"], 3, "job")
        assert out["t1"].passes == 2 and out["t1"].infra_attempts == 0
        # Only the contaminated task is re-scored, under the ladder job name.
        assert fake.calls[1] == (["t1"], "job_infra_rerun1")

    def test_missing_task_is_infra_by_definition(self):
        fake = _ScriptedEval([
            {"t1": _te("t1", 3, 3)},                    # t2 never came back
            {"t2": _te("t2", 1, 3)},
        ])
        out = eval_with_infra_rerun(fake, None, ["t1", "t2"], 3, "job")
        assert fake.calls[1][0] == ["t2"]
        assert out["t2"].passes == 1

    def test_keeps_measurement_with_fewest_infra(self):
        # The rerun came back just as contaminated: keep the original (strictly
        # fewer infra trials required to replace).
        first = _te("t1", 1, 3, infra=1)
        fake = _ScriptedEval([
            {"t1": first},
            {"t1": _te("t1", 0, 3, infra=1)},
            {"t1": _te("t1", 0, 3, infra=2)},
        ])
        out = eval_with_infra_rerun(fake, None, ["t1"], 3, "job")
        assert out["t1"] is first
        assert len(fake.calls) == 3  # base + 2 reruns, then the ladder ends

    def test_persistent_infra_survives_and_scores_low(self):
        results = [{"t1": _te("t1", 0, 3, infra=3)} for _ in range(3)]
        fake = _ScriptedEval(results)
        out = eval_with_infra_rerun(fake, None, ["t1"], 3, "job", max_reruns=2)
        assert len(fake.calls) == 3
        assert out["t1"].infra_attempts == 3  # left to score 0, never dropped

    def test_wrapper_is_identity_at_zero_reruns(self):
        inner = _ScriptedEval([])
        assert with_infra_rerun(inner, 0) is inner

    def test_wrapper_applies_ladder(self):
        fake = _ScriptedEval([
            {"t1": _te("t1", 0, 3, infra=1)},
            {"t1": _te("t1", 2, 3)},
        ])
        wrapped = with_infra_rerun(fake, 1)
        out = wrapped(None, ["t1"], 3, "job")
        assert out["t1"].passes == 2 and len(fake.calls) == 2


class TestFlipSummary:
    def test_partial_rescue_and_regression_accounting(self):
        cand = {"t1": _te("t1", 2, 3), "t2": _te("t2", 0, 3), "t3": _te("t3", 3, 3)}
        ctrl = {"t1": _te("t1", 1, 3), "t2": _te("t2", 1, 3), "t3": _te("t3", 3, 3)}
        s = flip_summary(cand, ctrl, ["t1", "t2", "t3"])
        assert s["rescued"] == ["t1"]          # 1/3 -> 2/3 counts as rescued
        assert s["regressed"] == ["t2"]
        assert s["still_failing"] == ["t1", "t2"]  # anything below 1.0

    def test_missing_arm_scores_zero(self):
        cand = {"t1": _te("t1", 2, 3)}
        s = flip_summary(cand, {}, ["t1", "t2"])
        assert s["rescued"] == ["t1"]  # 0.0 -> 2/3
        assert s["n_regressed"] == 0

    def test_id_lists_capped_but_counts_exact(self):
        ids = [f"t{i}" for i in range(15)]
        cand = {t: _te(t, 3, 3) for t in ids}
        s = flip_summary(cand, {}, ids, max_ids=12)
        assert s["n_rescued"] == 15
        assert len(s["rescued"]) == 12
