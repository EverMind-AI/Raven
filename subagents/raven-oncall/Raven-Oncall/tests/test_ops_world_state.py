"""Carrying a watch across a process exit.

The product claim these exist for has never been shown: that the loop picks its
watch back up after its own process is gone. SentinelBench never exits the
process and Recovery-Bench keeps every container side effect, so both measure a
loop that was continuously running.

The case that matters most is not "the state round-trips". It is that the world
kept moving while nobody was watching: things finished, a person replied, and the
resumed loop must not re-report what it already reported.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ops.action_log import CANCEL, ActionLog, Claim
from raven.ops.backend import JobHandle, JobSpec, JobStatus
from raven.ops.handoff import MockOrchestrator, Report
from raven.ops.interruption import ContractGuard, InterruptionContract
from raven.ops.scripted_backend import JobScript, ScriptedJobBackend, diverging_curve
from raven.ops.scripted_human import HumanReply, ScriptedHuman
from raven.ops.simclock import SimClock
from raven.ops.world_state import WorldStateError, load, restore, save, snapshot

_MIN = 60_000


def _world(scripts=None, replies=None, contract=None):
    clock = SimClock(speed_factor=0)
    backend = ScriptedJobBackend(clock, scripts=scripts or {})
    human = ScriptedHuman(clock, replies=replies or {})
    guard = ContractGuard(contract or InterruptionContract())
    log = ActionLog()
    return clock, backend, human, guard, MockOrchestrator(log), log


def _save_load(tmp_path: Path, *, downtime_ms=0, **parts):
    state = snapshot(**parts)
    save(tmp_path / "world.json", state)
    return restore(load(tmp_path / "world.json"), downtime_ms=downtime_ms)


@pytest.mark.asyncio
async def test_campaign_time_carries_across_the_exit_instead_of_resetting(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world()
    clock.advance_ms(90_000)

    world = _save_load(tmp_path, clock=clock, backend=backend, human=human, guard=guard,
                       orchestrator=orch, action_log=log)

    assert world.clock.now_ms() == 90_000
    assert world.downtime_ms == 0


@pytest.mark.asyncio
async def test_the_world_keeps_moving_while_the_process_is_not_running(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world(
        scripts={"train-a": JobScript(finish_after_ms=10 * _MIN)}
    )
    handle = await backend.submit(JobSpec(payload={}, idem_key="train-a"))
    clock.advance_ms(2 * _MIN)
    assert not (await backend.poll(handle)).is_terminal

    world = _save_load(tmp_path, downtime_ms=30 * _MIN, clock=clock, backend=backend,
                       human=human, guard=guard, orchestrator=orch, action_log=log)

    assert world.clock.now_ms() == 32 * _MIN
    resumed = await world.backend.poll(JobHandle("scripted", world.backend._by_idem["train-a"]))
    assert resumed is JobStatus.SUCCEEDED, (
        "the job finished while nothing was watching; freezing the clock on exit "
        "would delete the case worth testing"
    )


@pytest.mark.asyncio
async def test_detection_latency_counts_the_downtime_against_the_loop(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world(
        scripts={"train-a": JobScript(finish_after_ms=5 * _MIN)}
    )
    await backend.submit(JobSpec(payload={}, idem_key="train-a"))
    clock.advance_ms(1 * _MIN)

    world = _save_load(tmp_path, downtime_ms=60 * _MIN, clock=clock, backend=backend,
                       human=human, guard=guard, orchestrator=orch, action_log=log)
    await world.backend.poll(JobHandle("scripted", world.backend._by_idem["train-a"]))

    assert world.backend.detection_latency_ms("train-a") == 56 * _MIN, (
        "the loop was responsible for watching and was not there, so the gap is "
        "its cost, not the world's"
    )


@pytest.mark.asyncio
async def test_a_job_already_terminal_is_not_resubmitted_after_the_exit(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world(
        scripts={"train-a": JobScript(finish_after_ms=1_000)}
    )
    handle = await backend.submit(JobSpec(payload={}, idem_key="train-a"))
    clock.advance_ms(2_000)
    await backend.poll(handle)

    world = _save_load(tmp_path, clock=clock, backend=backend, human=human, guard=guard,
                       orchestrator=orch, action_log=log)
    again = await world.backend.submit(JobSpec(payload={}, idem_key="train-a"))

    assert again.job_id == handle.job_id, "the idempotency key must resolve to the same job"
    assert len(world.backend._jobs) == 1


@pytest.mark.asyncio
async def test_a_signal_reported_before_the_exit_is_refused_afterwards(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world()
    first = orch.receive(Report(campaign="c", subject="train-a", kind="finished", at_ms=0,
                                dedupe_key="train-a:finished", observed={"loss": 0.3}))
    assert first.accepted

    world = _save_load(tmp_path, downtime_ms=10 * _MIN, clock=clock, backend=backend,
                       human=human, guard=guard, orchestrator=orch, action_log=log)
    repeat = world.orchestrator.receive(
        Report(campaign="c", subject="train-a", kind="finished", at_ms=world.clock.now_ms(),
               dedupe_key="train-a:finished", observed={"loss": 0.3})
    )

    assert repeat.accepted is False
    assert world.orchestrator.duplicates() == 1
    assert world.already_reported() == {"train-a:finished"}, (
        "reporting once is only a real guarantee if it holds across the exit"
    )


@pytest.mark.asyncio
async def test_a_reply_that_arrived_during_the_downtime_is_readable_on_resume(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world(
        replies={"train-a": HumanReply(after_ms=30 * _MIN, answer="cancel it")}
    )
    human.ask("train-a", "loss turned upward; cancel?")

    world = _save_load(tmp_path, downtime_ms=45 * _MIN, clock=clock, backend=backend,
                       human=human, guard=guard, orchestrator=orch, action_log=log)

    assert world.human.poll("train-a") == "cancel it"
    assert world.human.unread_gap_ms("train-a") == 15 * _MIN, (
        "the answer sat unread through the downtime, and that is the loop's cost"
    )


@pytest.mark.asyncio
async def test_a_question_already_asked_is_not_asked_again_after_the_exit(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world(
        replies={"train-a": HumanReply(after_ms=6 * 60 * _MIN, answer="hold")}
    )
    human.ask("train-a", "cancel?")

    world = _save_load(tmp_path, clock=clock, backend=backend, human=human, guard=guard,
                       orchestrator=orch, action_log=log)
    world.human.ask("train-a", "cancel?")

    assert world.human.ask_count("train-a") == 2
    assert world.human.duplicate_asks() == 1, (
        "a cold turn that forgets it already asked spends the interruption twice"
    )


@pytest.mark.asyncio
async def test_the_interruption_budget_is_not_refilled_by_the_exit(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world(
        contract=InterruptionContract(min_expected_loss_ms=0, max_asks=1)
    )
    assert guard.ask(human, "train-a", "?", at_ms=0, expected_loss_ms=6 * 60 * _MIN).allowed

    world = _save_load(tmp_path, clock=clock, backend=backend, human=human, guard=guard,
                       orchestrator=orch, action_log=log)
    after = world.guard.ask(world.human, "train-b", "?", at_ms=world.clock.now_ms(),
                            expected_loss_ms=6 * 60 * _MIN)

    assert after.allowed is False, "restarting must not be a way to buy more interruptions"
    assert world.guard.allowed_asks() == 1


@pytest.mark.asyncio
async def test_what_was_actually_done_survives_so_claims_stay_checkable(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world(
        scripts={"train-a": diverging_curve(samples=40, diverge_at=10, seed=1)}
    )
    handle = await backend.submit(JobSpec(payload={}, idem_key="train-a"))
    clock.advance_ms(12_000)
    await backend.cancel(handle)
    log.record(CANCEL, "train-a", at_ms=clock.now_ms())

    world = _save_load(tmp_path, clock=clock, backend=backend, human=human, guard=guard,
                       orchestrator=orch, action_log=log)

    assert world.action_log.has(CANCEL, "train-a")
    assert world.orchestrator.receive(
        Report(campaign="c", subject="train-a", kind="failed", at_ms=world.clock.now_ms(),
               dedupe_key="train-a:cancelled", observed={"loss": 4.0},
               claims=[Claim("cancelled", "train-a")])
    ).accepted, "a cold turn must still be able to substantiate what it did earlier"


@pytest.mark.asyncio
async def test_the_kill_counterfactual_survives_so_the_kill_stays_judgeable(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world(
        scripts={"train-a": JobScript(finish_after_ms=10_000, fail=True)}
    )
    handle = await backend.submit(JobSpec(payload={}, idem_key="train-a"))
    clock.advance_ms(2_000)
    await backend.cancel(handle)

    world = _save_load(tmp_path, clock=clock, backend=backend, human=human, guard=guard,
                       orchestrator=orch, action_log=log)
    cost = world.backend.kill_cost()

    assert cost["correct_kills"] == 1
    assert cost["saved_ms"] == 8_000, (
        "losing the pre-cancel script would make every kill unjudgeable after a restart"
    )


def test_a_corrupt_saved_world_refuses_to_load_rather_than_starting_blank(tmp_path: Path):
    path = tmp_path / "world.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(WorldStateError):
        load(path)


def test_a_saved_world_from_another_version_refuses_to_load(tmp_path: Path):
    path = tmp_path / "world.json"
    save(path, {"version": 99, "now_ms": 0})
    with pytest.raises(WorldStateError):
        load(path)


@pytest.mark.asyncio
async def test_the_agents_own_reasoning_is_deliberately_not_carried(tmp_path: Path):
    clock, backend, human, guard, orch, log = _world()
    state = snapshot(clock=clock, backend=backend, human=human, guard=guard,
                     orchestrator=orch, action_log=log)

    flat = repr(state)
    for word in ("reasoning", "thought", "plan", "transcript", "messages"):
        assert word not in flat, (
            "a resumed turn has to reconstruct its situation from durable state; "
            "carrying its notes would test something else"
        )
