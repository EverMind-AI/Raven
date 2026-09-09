"""The oncall decision chain, fork semantics preserved.

The three gates the resubmit/kill chain rests on, exercised as mechanisms
(the part-2b tools are faces over these): the basis-freshness gate (a
wait/kill/resubmit must cite a number a fresh observation showed), the retry
ladder (a fresh idempotency key per attempt), escalate-once per trial across
crashes, and the interruption contract (threshold, quiet hours, ask budget --
a denied ask never reaches the person, the attempt is recorded as a model
signal, and the counts survive a cold start).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.campaign import Campaign, Trial  # noqa: E402
from oncall_flow.escalation import (  # noqa: E402
    anything_running,
    append_note,
    append_owner_answer,
    blocking_question_open,
    load_guard,
    save_guard,
    unanswered_question,
)
from oncall_flow.instrument import log_event, read_events, write_meta  # noqa: E402
from oncall_flow.interruption import ContractGuard, InterruptionContract  # noqa: E402
from oncall_flow.ledger import Ledger  # noqa: E402
from oncall_flow.mock_backend import JobPlan, MockJobBackend  # noqa: E402
from oncall_flow.policy import (  # noqa: E402
    RetryPolicy,  # noqa: E402
    attempt_key,
    attempt_no,
    base_trial,
    basis_refusal,
    record_basis,
)
from oncall_flow.policy import from_meta as policy_from_meta  # noqa: E402
from oncall_flow.state_claims import (  # noqa: E402
    StateFacts,
    basis_problems,
    read_decision_seq,
    write_facts,
)

_MIN = 60_000


# ── The retry ladder ────────────────────────────────────────────────


def test_retry_policy_counts_retries_after_the_first_attempt() -> None:
    assert RetryPolicy().max_retries == 0, "the fork's default: fail once, escalate"
    assert RetryPolicy(0).should_retry(1) is False
    ladder = RetryPolicy(2)
    assert ladder.should_retry(1) is True
    assert ladder.should_retry(2) is True
    assert ladder.should_retry(3) is False


def test_the_policy_reads_the_campaign_declaration() -> None:
    assert policy_from_meta({}) == RetryPolicy()
    assert policy_from_meta({"retry": {"max_retries": 2}}) == RetryPolicy(2)
    assert policy_from_meta({"retry": {"max_retries": "nope"}}) == RetryPolicy(), (
        "an unreadable declaration is the default, not an error: the policy gates "
        "escalation, and a typo must not turn every failure into silence"
    )
    assert policy_from_meta({"retry": {"max_retries": -3}}) == RetryPolicy(0)


def test_attempt_keys_round_trip_the_fork_shape() -> None:
    assert attempt_key("t1", 1) == "t1"
    assert attempt_key("t1", 3) == "t1#a3"
    assert attempt_no("t1") == 1
    assert attempt_no("t1#a3") == 3
    assert base_trial("t1#a3") == "t1"
    assert base_trial("t1") == "t1"
    assert attempt_no("t1#aX") == 1, "a suffix that is not an attempt is part of the trial id"


async def test_a_retry_mints_a_fresh_idempotency_key(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger.json")
    backend = MockJobBackend(
        plans={
            "t1": JobPlan(fail=True, error="flaky node"),
            "t1#a2": JobPlan(metrics={"score": 1.0}),
        }
    )
    asked: list[str] = []

    async def esc(trial_id, rec):
        asked.append(trial_id)

    camp = Campaign("c", [Trial("t1")], backend, led, retry_policy=RetryPolicy(1), escalation=esc)
    await camp.run(max_passes=10)

    first, second = led.get("t1"), led.get("t1#a2")
    assert first is not None and first.status.value == "failed"
    assert second is not None and second.status.value == "succeeded"
    assert first.handle.job_id != second.handle.job_id, "each retry is a distinct backend job"
    assert asked == [], "a chain that ends in success never escalates"
    assert camp.is_done()


# ── Escalate-once ───────────────────────────────────────────────────


async def test_escalation_fires_at_most_once_per_trial_across_crashes(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger.json")
    backend = MockJobBackend(default_plan=JobPlan(fail=True, error="boom"))
    asked: list[str] = []

    async def esc(trial_id, rec):
        asked.append(trial_id)

    camp = Campaign("c", [Trial("t1")], backend, led, retry_policy=RetryPolicy(0), escalation=esc)
    await camp.step()
    assert asked == ["t1"]
    assert led.get("t1").escalated is True

    await camp.step()
    assert asked == ["t1"], "a second pass does not re-escalate"

    # A crash-restart builds a fresh Campaign over the same ledger; the flag
    # is the durable record.
    reborn = Campaign("c", [Trial("t1")], backend, led, retry_policy=RetryPolicy(0), escalation=esc)
    await reborn.step()
    assert asked == ["t1"], "the ledger's escalated flag survives the crash"
    assert reborn.is_done(), "an escalated trial is settled, not pending"


# ── The basis-freshness gate ────────────────────────────────────────


def test_an_empty_basis_is_refused_outright() -> None:
    assert basis_problems("", StateFacts(probe_seq=5), last_seq=0) == ["basis is empty"]


def test_a_decision_needs_an_observation_newer_than_the_last_decision() -> None:
    facts = StateFacts(metric_readings={"loss": (0.51, 0.42)}, probe_seq=2)
    stale = basis_problems("loss now 0.42", facts, last_seq=2)
    assert any("no observation has been recorded" in p for p in stale)
    assert any("ops_tune_status" in p for p in stale), "the refusal names the tool that records a look"
    assert basis_problems("loss now 0.42", facts, last_seq=1) == []


def test_the_basis_must_cite_a_number_the_probe_showed() -> None:
    facts = StateFacts(metric_readings={"loss": (0.51, 0.42)}, probe_seq=2, shown_values=(64.0,))
    assert basis_problems("looks stuck around 9.99", facts, last_seq=1) == [
        "the number in basis is not among the readings recorded for this campaign"
    ]
    assert basis_problems("still no movement", facts, last_seq=1) == ["basis cites no number"]
    assert basis_problems("batch size 64 is wrong for this host", facts, last_seq=1) == [], (
        "anything the tool printed is citable, not just readings -- else the friction "
        "lands only on the reasons for acting"
    )


def test_nonfinite_readings_are_cited_by_name_and_no_readings_refuse_nothing() -> None:
    diverged = StateFacts(metric_readings={"loss": (float("nan"),)}, probe_seq=1)
    assert basis_problems("loss is NaN, the run has diverged", diverged, last_seq=0) == []
    assert basis_problems("loss is 3.14", diverged, last_seq=0) == [
        "the number in basis is not among the readings recorded for this campaign"
    ]
    silent = StateFacts(probe_seq=1)
    assert basis_problems("the job prints nothing at all", silent, last_seq=0) == [], (
        "a fact the harness does not hold refuses nothing: the run with no readings "
        "is precisely the one whose kill is least in doubt"
    )


def test_the_gate_cycles_probe_decide_probe_on_disk(tmp_path: Path) -> None:
    cdir = tmp_path / "camp"
    write_facts(cdir, StateFacts(metric_readings={"loss": (0.42,)}, probe_seq=1))

    assert basis_refusal(cdir, "loss read 0.42 just now", "kill") is None
    record_basis(cdir, "loss read 0.42 just now", "kill")
    assert read_decision_seq(cdir) == 1

    refused = basis_refusal(cdir, "loss read 0.42 just now", "kill")
    assert refused is not None and refused.startswith("REFUSED: "), (
        "the same reading cannot justify a second decision; knowing now entails looking now"
    )

    write_facts(cdir, StateFacts(metric_readings={"loss": (0.40,)}, probe_seq=2))
    assert basis_refusal(cdir, "loss fell to 0.40", "resubmit") is None

    kinds = [e["kind"] for e in read_events(cdir)]
    assert kinds == ["basis_accepted", "basis_refused"], "accepted and refused decisions both land in the trail"


# ── The interruption contract ───────────────────────────────────────


def test_the_threshold_moves_with_the_quiet_hours_and_may_forbid() -> None:
    contract = InterruptionContract(
        min_expected_loss_ms=30 * _MIN,
        quiet_hours=(22, 6),
        quiet_min_expected_loss_ms=120 * _MIN,
    )
    assert contract.threshold_at_hour(12) == 30 * _MIN
    assert contract.threshold_at_hour(23) == 120 * _MIN, "quiet hours wrap midnight"
    assert contract.threshold_at_hour(5) == 120 * _MIN
    assert contract.threshold_at_hour(6) == 30 * _MIN, "[start, end) is half-open"

    forbidding = InterruptionContract(quiet_hours=(22, 6))
    assert forbidding.threshold_at_hour(23) is None, "no quiet threshold means never, not zero"

    text = InterruptionContract(min_expected_loss_ms=30 * _MIN, max_asks=2).as_instruction()
    assert "at least 30 minutes" in text and "at most 2 times" in text
    assert "cannot estimate the cost" in text, "the unestimated-ask escape hatch is stated"


def test_the_guard_denies_below_the_bar_and_records_the_attempt() -> None:
    guard = ContractGuard(InterruptionContract(min_expected_loss_ms=30 * _MIN), start_hour=9)

    denied = guard.check(at_ms=0, expected_loss_ms=10 * _MIN)
    assert denied.allowed is False and "under the" in denied.reason
    allowed = guard.check(at_ms=0, expected_loss_ms=45 * _MIN)
    assert allowed.allowed is True

    unestimated = guard.check(at_ms=0, expected_loss_ms=None)
    assert unestimated.allowed is True and unestimated.estimated is False, (
        "a loop that cannot price the unfamiliar is exactly the one that must be heard"
    )

    summary = guard.summary()
    assert summary["allowed"] == 2 and summary["breach_attempts"] == 1
    assert summary["unestimated"] == 1


def test_the_ask_budget_runs_out_and_quiet_hours_deny_by_wall_clock() -> None:
    guard = ContractGuard(InterruptionContract(max_asks=1), start_hour=9)
    assert guard.check(at_ms=0, expected_loss_ms=999 * _MIN).allowed is True
    spent = guard.check(at_ms=0, expected_loss_ms=999 * _MIN)
    assert spent.allowed is False and "1 interruptions" in spent.reason

    night = ContractGuard(InterruptionContract(quiet_hours=(22, 6)), start_hour=22)
    denied = night.check(at_ms=0, expected_loss_ms=999 * _MIN)
    assert denied.allowed is False and "forbids interruption at 22:00" in denied.reason
    assert night.hour_at(8 * 60 * _MIN) == 6, "the campaign clock wraps into daytime"
    assert night.check(at_ms=8 * 60 * _MIN, expected_loss_ms=999 * _MIN).allowed is True


def test_the_guard_counts_survive_a_cold_start(tmp_path: Path) -> None:
    cdir = tmp_path / "camp"
    write_meta(
        cdir,
        {
            "interruption_contract": {"min_expected_loss_ms": 30 * _MIN, "max_asks": 2},
            "start_hour": 9,
        },
    )

    guard = load_guard(cdir)
    assert guard.check(at_ms=0, expected_loss_ms=45 * _MIN).allowed is True
    save_guard(cdir, guard)

    woken = load_guard(cdir)
    assert woken.allowed_asks() == 1, "a wake turn starts cold; the counts are the enforcement"
    assert woken.check(at_ms=0, expected_loss_ms=45 * _MIN).allowed is True
    save_guard(cdir, woken)
    third = load_guard(cdir)
    assert third.check(at_ms=0, expected_loss_ms=45 * _MIN).allowed is False, (
        "the budget is spent across turns, not per turn"
    )

    bare = load_guard(tmp_path / "undeclared")
    assert bare.check(at_ms=0, expected_loss_ms=0).allowed is True, (
        "no contract means no threshold, not a secret default"
    )


# ── The question trail ──────────────────────────────────────────────


def test_a_question_stays_open_until_a_note_closes_it(tmp_path: Path) -> None:
    cdir = tmp_path / "camp"
    cdir.mkdir()
    log_event(cdir, "ask_owner", question="kill train-c?", allowed=True, blocks_progress=True)
    assert unanswered_question(cdir) == "kill train-c?"
    assert blocking_question_open(cdir) == "kill train-c?"

    append_owner_answer(cdir, "yes, kill it")
    assert unanswered_question(cdir) == ""
    assert blocking_question_open(cdir) == "", "the owner's answer lands as a note and closes it"


def test_a_denied_ask_never_opens_a_question(tmp_path: Path) -> None:
    cdir = tmp_path / "camp"
    cdir.mkdir()
    log_event(cdir, "ask_owner", question="may I?", allowed=False)
    assert unanswered_question(cdir) == "", "a denied ask never reached the person"

    log_event(cdir, "ask_owner", question="checking in", allowed=True, blocks_progress=False)
    assert unanswered_question(cdir) == "checking in"
    assert blocking_question_open(cdir) == "", "only a question the loop marked blocking blocks"
    append_note(cdir, "the answer no longer matters; proceeding on the fallback")
    assert unanswered_question(cdir) == ""


def test_anything_running_reads_the_ledger_file(tmp_path: Path) -> None:
    cdir = tmp_path / "camp"
    led = Ledger(cdir / "ledger.json")
    assert anything_running(cdir) is False
    led.record("t1", campaign="c")
    assert anything_running(cdir) is True
    from oncall_flow.backend import JobResult, JobStatus

    led.set_result("t1", JobResult(JobStatus.FAILED, error="done"))
    assert anything_running(cdir) is False


# ── fork test_ops_policy.py, merged (2c-ii) ─────────────────────────


def test_default_policy_never_retries() -> None:
    policy = RetryPolicy()
    assert policy.should_retry(1) is False


def test_max_retries_allows_that_many_further_attempts() -> None:
    policy = RetryPolicy(max_retries=2)
    assert policy.should_retry(1) is True
    assert policy.should_retry(2) is True
    assert policy.should_retry(3) is False
