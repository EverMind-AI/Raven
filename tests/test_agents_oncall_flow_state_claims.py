"""Tests for the state-claim check: claims about harness-held state.

Two properties matter more than coverage here. A contradiction must be refused,
and everything else must pass -- an uncertain claim, a claim with no reading to
check it against, and a claim the patterns cannot resolve all pass, and are
counted so a pass can be read for what it is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.state_claims import StateFacts, check, collect_facts, read_facts, write_facts  # noqa: E402


def _facts(**kwargs) -> StateFacts:
    return StateFacts(**kwargs)


# --- the round-1 failure ---


def test_round1_claim_is_refused_when_the_listing_contradicts_the_count():
    """The claim that motivated this: keep_recent_checkpoints=3 read as "only the
    three most recent exist, the best is probably gone", asserted without
    looking. Its factual half is a count, and the listing has five."""
    narrative = (
        "keep_recent_checkpoints=3 means only the three most recent checkpoints are kept, "
        "so the peak may already have been discarded."
    )
    facts = _facts(checkpoints=("step-200", "step-1200", "step-2400", "step-3600", "step-4800"))

    out = check(narrative, facts)

    assert len(out.contradicted) == 1
    assert "3 checkpoint(s) are kept" in out.contradicted[0]
    # Names the claim, not the true count. Printing "has 5" told the loop something
    # no ops tool exposes, and a refused report costs nothing to retry.
    assert "5" not in out.contradicted[0]
    assert "step-4800" not in out.contradicted[0]


def test_hedged_claim_about_an_unnamed_peak_is_not_refused():
    """ "the best one may have been discarded" with no step id cannot be resolved
    without deciding which is best, which this module must not do. It is counted
    and passes."""
    facts = _facts(checkpoints=("step-200", "step-1200"))

    out = check("The best checkpoint may have been discarded by pruning.", facts)

    assert out.contradicted == []
    assert len(out.unresolved) == 1


def test_named_checkpoint_claimed_gone_but_present_is_refused():
    facts = _facts(checkpoints=("step-1200", "step-2400"))

    out = check("step-1200 was pruned, so the run cannot be recovered from it.", facts)

    assert len(out.contradicted) == 1
    assert "step-1200" in out.contradicted[0]
    # The refusal names the claim it disagrees with and stops. It used to print the
    # whole listing, which handed over state no ops tool exposes -- and a refused
    # report is free to retry, because the state check runs before dedupe
    # registration, so that made the refusal a reliable read channel.
    assert "step-2400" not in out.contradicted[0]


def test_named_checkpoint_claimed_present_but_absent_is_refused():
    facts = _facts(checkpoints=("step-2400",))

    out = check("step-1200 is still there on disk.", facts)

    assert len(out.contradicted) == 1
    assert "described as kept" in out.contradicted[0]


def test_a_true_claim_passes():
    facts = _facts(checkpoints=("step-2400", "step-3600"), remaining_minutes=41.0, job_statuses=("running",))

    out = check(
        "step-1200 was discarded; step-2400 is still there. The job is running with 41 minutes remaining.",
        facts,
    )

    assert out.contradicted == []


def test_quoting_the_config_value_is_not_a_claim():
    """ "keep_recent_checkpoints=3" on its own states a setting, not what exists.
    Refusing it would refuse a correct report for citing its own configuration."""
    facts = _facts(checkpoints=("step-200", "step-1200", "step-2400", "step-3600"))

    out = check("Submitted with keep_recent_checkpoints=3 and eval_every=200.", facts)

    assert out.contradicted == []
    assert out.unresolved == []


# --- no readings means no refusals ---


def test_nothing_is_refused_when_the_harness_holds_no_facts():
    out = check(
        "step-1200 was discarded, only three checkpoints are kept, 5 minutes remaining, the job has crashed.",
        StateFacts(),
    )

    assert out.contradicted == []
    assert len(out.unverifiable) == 3


def test_empty_listing_is_not_the_same_as_no_listing():
    """An empty tuple is a reading: nothing is on disk. A claim that a named
    checkpoint is present is then contradicted."""
    out = check("step-1200 is saved.", _facts(checkpoints=()))

    assert len(out.contradicted) == 1


# --- budget ---


@pytest.mark.parametrize(
    "narrative,expected",
    [
        ("about 41 minutes remaining", 0),
        ("41.5 min left in the budget", 0),
        ("the remaining budget is 40 minutes", 0),
        ("12 minutes remaining", 1),
        ("the budget is 90 minutes remaining", 1),
    ],
)
def test_budget_claim_is_compared_with_tolerance(narrative, expected):
    out = check(narrative, _facts(remaining_minutes=41.0))
    assert len(out.contradicted) == expected


def test_budget_without_a_number_is_counted_not_refused():
    out = check("The budget is nearly exhausted.", _facts(remaining_minutes=41.0))

    assert out.contradicted == []
    assert len(out.unresolved) == 1


# --- job status ---


def test_status_claim_contradicting_the_poll_is_refused():
    out = check("The training job has crashed.", _facts(job_statuses=("running",)))

    assert len(out.contradicted) == 1
    assert "has crashed" in out.contradicted[0] and "running" in out.contradicted[0]


def test_status_claim_matching_the_poll_passes():
    out = check("The job is still running.", _facts(job_statuses=("running",)))

    assert out.contradicted == []


def test_status_value_outside_the_table_is_reported_not_assumed():
    """A polled value with no row cannot be compared. Silence here would read as
    agreement."""
    out = check("The job is still running.", _facts(job_statuses=("weird_new_state",)))

    assert out.contradicted == []
    assert len(out.unresolved) == 1
    assert "weird_new_state" in out.unresolved[0]


def test_counts_expose_numerator_and_denominator():
    out = check(
        "step-1200 was pruned. The budget is nearly exhausted.",
        _facts(checkpoints=("step-1200",), remaining_minutes=41.0),
    )

    counts = out.counts()
    assert counts == {"contradicted": 1, "unresolved": 1, "unverifiable": 0, "claims_seen": 2}


# --- the facts file ---


def test_facts_round_trip(tmp_path):
    facts = StateFacts(
        checkpoints=("step-200",),
        remaining_minutes=12.5,
        job_statuses=("running",),
        observed_at_ms=1723,
        sources={"checkpoints": "probe"},
    )
    write_facts(tmp_path, facts)

    assert read_facts(tmp_path) == facts


def test_unreadable_facts_file_reads_as_all_unknown(tmp_path):
    """A checker that raised on its own bookkeeping would block reports."""
    (tmp_path / "state_facts.json").write_text("{not json", encoding="utf-8")

    assert read_facts(tmp_path) == StateFacts()


def test_missing_facts_file_reads_as_all_unknown(tmp_path):
    assert read_facts(tmp_path) == StateFacts()


# --- collect_facts ---


class _Rec:
    def __init__(self, idem_key, status_value, handle="h"):
        self.idem_key = idem_key
        self.status = type("S", (), {"value": status_value})()
        self.handle = handle


class _Backend:
    def __init__(self, remaining=None, artifacts=None):
        self._remaining = remaining
        self._artifacts = artifacts

    async def remaining_minutes(self):
        return self._remaining

    async def list_artifacts(self, handle, pattern="step-*"):
        return self._artifacts


class _BareBackend:
    """No budget, no artifact listing. Must yield None for both."""


@pytest.mark.asyncio
async def test_collect_facts_reads_all_three_classes():
    facts = await collect_facts(
        _Backend(remaining=23.5, artifacts=("step-200", "step-400")),
        [_Rec("t1", "running")],
        now_ms=99,
    )

    assert facts.remaining_minutes == 23.5
    assert facts.checkpoints == ("step-200", "step-400")
    assert facts.job_statuses == ("running",)
    assert facts.observed_at_ms == 99


@pytest.mark.asyncio
async def test_collect_facts_drops_an_ambiguous_listing():
    """Two trials both listing checkpoints: a bare step id cannot be attributed,
    so no listing is kept and the reason is recorded."""
    facts = await collect_facts(
        _Backend(artifacts=("step-200",)),
        [_Rec("t1", "running"), _Rec("t2", "running")],
        now_ms=1,
    )

    assert facts.checkpoints is None
    assert "ambiguous" in facts.sources["checkpoints"]


@pytest.mark.asyncio
async def test_collect_facts_on_a_backend_without_the_capabilities():
    facts = await collect_facts(_BareBackend(), [_Rec("t1", "succeeded")], now_ms=1)

    assert facts.remaining_minutes is None
    assert facts.checkpoints is None
    assert facts.job_statuses == ("succeeded",)


@pytest.mark.asyncio
async def test_a_failing_probe_is_not_a_fact():
    class _Broken:
        async def remaining_minutes(self):
            raise RuntimeError("ssh down")

        async def list_artifacts(self, handle, pattern="step-*"):
            raise RuntimeError("ssh down")

    facts = await collect_facts(_Broken(), [_Rec("t1", "running")], now_ms=1)

    assert facts.remaining_minutes is None
    assert facts.checkpoints is None


def test_written_facts_are_json_a_human_can_read(tmp_path):
    write_facts(tmp_path, StateFacts(checkpoints=("step-200",), remaining_minutes=1.0))
    payload = json.loads((tmp_path / "state_facts.json").read_text(encoding="utf-8"))

    assert payload["checkpoints"] == ["step-200"]


def test_basis_may_cite_any_number_the_tools_showed_not_only_metric_readings() -> None:
    """The gate used to accept only numbers from ``metric_readings``, which put the
    friction on one side of the decision.

    "Keep waiting" is naturally justified by an evaluation score, so it passed.
    "Switch the configuration" is naturally justified by the parameter that is
    wrong -- a learning rate, a viscosity -- so it was refused with "the number in
    basis is not among the readings recorded". Measured 2026-08-06: a correct,
    complete judgement ("nu=1e-3 is 1000x water") was refused, while every
    "still below baseline, checking again later" was accepted.

    That is the asymmetry the basis field exists to avoid. Not acting is already
    the outcome that needs no successful tool call; a gate that also refuses the
    reasons for acting points the gradient the same way twice.

    Fabricated numbers are still refused -- the accepted set is what the tools put
    in front of the loop this turn, nothing more.
    """
    import dataclasses

    from oncall_flow.state_claims import StateFacts, basis_problems

    facts = StateFacts(metric_readings={"ndcg": (0.2937,)}, probe_seq=1)
    facts = dataclasses.replace(facts, shown_values=(2e-05, 5224.0, 140.0))

    # the reason for acting: cites the hyperparameter the tools printed
    assert (
        basis_problems(
            "lr=2e-05 is 10x too high for this model; killing and resubmitting at 2e-06",
            facts,
            last_seq=0,
        )
        == []
    )

    # the reason for waiting still works
    assert basis_problems("ndcg 0.2937 is below the starting value", facts, last_seq=0) == []

    # a number nobody showed is still refused
    problems = basis_problems("roughly 1600 steps per epoch, so ~88 min to go", facts, last_seq=0)
    assert problems and "not among" in problems[0]


def test_shown_values_survive_a_round_trip_through_disk() -> None:
    """The gate reads facts back from disk on the next turn, so a field that is not
    serialised is a field that silently reverts to the old, tighter rule."""
    import dataclasses
    import tempfile
    from pathlib import Path

    from oncall_flow.state_claims import StateFacts, read_facts, write_facts

    d = Path(tempfile.mkdtemp())
    facts = dataclasses.replace(StateFacts(probe_seq=3), shown_values=(2e-05, 140.0))
    write_facts(d, facts)

    assert read_facts(d).shown_values == (2e-05, 140.0)
