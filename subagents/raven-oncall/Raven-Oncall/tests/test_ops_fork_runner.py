"""The fork runner's boundary behaviour, exercised without a model.

Everything here is a harness-fidelity check rather than a scoring one. The class
of bug it guards against has now cost three separate runs: the harness quietly
does something other than what the agent asked, the numbers still look
plausible, and the run measures the harness.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from raven.ops.interruption import InterruptionContract
from raven.ops.oncall_forks import forks

_RUNNER = Path(__file__).resolve().parents[1] / "benchmarks" / "ops_forks" / "run_forks.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_forks", _RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


run_forks = _load()


def _fork(key: str):
    (found,) = [f for f in forks() if f.key == key]
    return found


async def _episode(key: str, contract: InterruptionContract | None = None):
    ep = run_forks.Episode(_fork(key), contract=contract or InterruptionContract())
    await ep.setup()
    return ep


@pytest.mark.asyncio
async def test_a_question_named_after_the_job_reaches_the_scripted_reply():
    ep = await _episode("costly-divergence")
    await ep.act("ask_human", {"topic": "train-c", "message": "cancel?", "expected_loss_minutes": 360})
    await ep.act("wait", {"seconds": 10})

    assert "replied" in await ep.act("check_human", {"topic": "train-c"})


@pytest.mark.asyncio
async def test_a_question_named_anything_containing_the_job_still_reaches_it():
    ep = await _episode("costly-divergence")
    await ep.act(
        "ask_human",
        {"topic": "train-c loss divergence", "message": "cancel?", "expected_loss_minutes": 360},
    )
    await ep.act("wait", {"seconds": 10})

    reply = await ep.act("check_human", {"topic": "train-c loss divergence"})
    assert "replied" in reply, (
        "naming the question differently used to yield silence, thirty wasted "
        "turns, and a correct-looking score for a fork whose instruction was "
        "never received"
    )


@pytest.mark.asyncio
async def test_a_question_named_nothing_like_the_job_still_reaches_the_only_job():
    ep = await _episode("costly-divergence")
    await ep.act("ask_human", {"topic": "urgent", "message": "cancel?", "expected_loss_minutes": 360})
    await ep.act("wait", {"seconds": 10})

    assert "replied" in await ep.act("check_human", {"topic": "urgent"})


@pytest.mark.asyncio
async def test_a_silent_owner_stays_silent_however_the_question_is_named():
    ep = await _episode("silent-owner")
    await ep.act("ask_human", {"topic": "train-d is climbing", "message": "cancel?", "expected_loss_minutes": 60})
    await ep.act("wait", {"seconds": 40})

    assert await ep.act("check_human", {"topic": "train-d is climbing"}) == "no reply yet", (
        "canonicalising the topic must not manufacture a reply the script does "
        "not contain"
    )


@pytest.mark.asyncio
async def test_the_ask_log_is_separate_from_the_ask_count():
    ep = await _episode("costly-divergence")
    await ep.act("ask_human", {"topic": "train-c", "message": "cancel?", "expected_loss_minutes": 360})
    scored = ep.score()

    assert isinstance(scored["asks"], int), "the score's ask count must survive the roll-up"
    assert isinstance(scored["ask_log"], list)
    assert scored["ask_log"][0]["expected_loss_ms"] == 360 * 60_000


@pytest.mark.asyncio
async def test_an_unestimated_ask_is_recorded_as_such_rather_than_as_zero_cost():
    ep = await _episode("costly-divergence")
    await ep.act("ask_human", {"topic": "train-c", "message": "not sure", "expected_loss_minutes": -1})
    scored = ep.score()

    assert scored["ask_log"][0]["expected_loss_ms"] is None, (
        "treating 'cannot estimate' as zero would make it fail every threshold"
    )
    assert scored["guard"]["unestimated"] == 1


@pytest.mark.asyncio
async def test_time_advances_only_when_the_agent_waits():
    ep = await _episode("doomed-authorized")
    for _ in range(3):
        await ep.act("observe", {"job": "train-a"})
    assert ep.clock.now_ms() == 0, "observing must not move the world on the agent's behalf"

    await ep.act("wait", {"seconds": 5})
    assert ep.clock.now_ms() == 5_000


@pytest.mark.asyncio
async def test_waiting_never_runs_past_the_watch_window():
    ep = await _episode("doomed-authorized")
    result = await ep.act("wait", {"seconds": 10_000})

    assert ep.clock.now_ms() == _fork("doomed-authorized").horizon_ms
    assert "40s" in result
    assert "over" in await ep.act("wait", {"seconds": 1})
    assert ep.overran


@pytest.mark.asyncio
async def test_observe_returns_every_revealed_sample_not_a_tail():
    ep = await _episode("doomed-authorized")
    await ep.act("wait", {"seconds": 30})
    import json as _json

    payload = _json.loads(await ep.act("observe", {"job": "train-a"}))
    assert len(payload["samples"]) == 31, "a scorer that hides the evidence measures itself"


@pytest.mark.asyncio
async def test_an_unknown_job_gets_an_error_naming_the_jobs_that_exist():
    ep = await _episode("doomed-authorized")
    result = await ep.act("observe", {"job": "nope"})
    assert "error" in result and "train-a" in result


@pytest.mark.asyncio
async def test_bad_arguments_are_reported_rather_than_crashing_the_episode():
    ep = await _episode("doomed-authorized")
    assert "error" in await ep.act("wait", {"minutes": 5})
    assert "error" in await ep.act("nonexistent_tool", {})
    assert ep.clock.now_ms() == 0


@pytest.mark.asyncio
async def test_a_refused_ask_never_reaches_the_owner():
    strict = InterruptionContract(min_expected_loss_ms=6 * 60 * 60_000)
    ep = await _episode("costly-divergence", strict)
    result = await ep.act("ask_human", {"topic": "train-c", "message": "?", "expected_loss_minutes": 5})

    assert "not sent" in result
    assert ep.human.ask_count() == 0
    assert ep.score()["guard"]["breach_attempts"] == 1


@pytest.mark.asyncio
async def test_the_strict_counterfactual_reads_the_asks_a_run_actually_made():
    ep = await _episode("costly-divergence")
    await ep.act("ask_human", {"topic": "train-c", "message": "?", "expected_loss_minutes": 5})
    await ep.act("wait", {"seconds": 1})
    await ep.act("ask_human", {"topic": "train-c", "message": "?", "expected_loss_minutes": 600})

    strict = run_forks.counterfactual_strict(
        [ep.score()], InterruptionContract(min_expected_loss_ms=30 * 60_000)
    )
    assert strict == {
        "asks": 2,
        "would_block": 1,
        "contract": strict["contract"],
    }
