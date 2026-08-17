"""Kill verdicts in the scripted world.

Whether a kill was right can only be answered where the counterfactual is known:
the script says what the job would have done if left alone. Production cannot
know this, so the accounting lives on the scripted backend -- it is carrier-side
scoring, not a runtime metric.
"""

from __future__ import annotations

import pytest

from raven.ops.backend import JobSpec
from raven.ops.scripted_backend import JobScript, ScriptedJobBackend
from raven.ops.simclock import SimClock


def _clock() -> SimClock:
    return SimClock(speed_factor=0)


def _spec(key: str) -> JobSpec:
    return JobSpec(payload={"job": "train"}, idem_key=key)


@pytest.mark.asyncio
async def test_killing_a_doomed_job_is_correct_and_saves_its_remaining_time():
    clock = _clock()
    backend = ScriptedJobBackend(clock, scripts={"doomed": JobScript(finish_after_ms=10_000, fail=True)})
    handle = await backend.submit(_spec("doomed"))
    clock.advance_ms(2_000)
    await backend.cancel(handle)

    cost = backend.kill_cost()
    assert cost["correct_kills"] == 1
    assert cost["wrong_kills"] == 0
    assert cost["saved_ms"] == 8_000
    assert cost["wasted_ms"] == 0


@pytest.mark.asyncio
async def test_killing_a_job_that_would_have_succeeded_is_wrong_and_wastes_its_run():
    clock = _clock()
    backend = ScriptedJobBackend(clock, scripts={"fine": JobScript(finish_after_ms=10_000, fail=False)})
    handle = await backend.submit(_spec("fine"))
    clock.advance_ms(9_000)
    await backend.cancel(handle)

    cost = backend.kill_cost()
    assert cost["wrong_kills"] == 1
    assert cost["correct_kills"] == 0
    assert cost["wasted_ms"] == 9_000
    assert cost["saved_ms"] == 0


@pytest.mark.asyncio
async def test_a_job_left_to_finish_is_not_counted_as_any_kill():
    clock = _clock()
    backend = ScriptedJobBackend(clock, scripts={"fine": JobScript(finish_after_ms=1_000, fail=False)})
    await backend.submit(_spec("fine"))
    clock.advance_ms(5_000)

    cost = backend.kill_cost()
    assert cost["correct_kills"] == 0
    assert cost["wrong_kills"] == 0
    assert cost["killed_never_finishing"] == 0


@pytest.mark.asyncio
async def test_killing_a_never_finishing_job_is_reported_apart_from_saved_time():
    clock = _clock()
    backend = ScriptedJobBackend(clock, scripts={"hangs": JobScript(finish_after_ms=None)})
    handle = await backend.submit(_spec("hangs"))
    clock.advance_ms(3_000)
    await backend.cancel(handle)

    cost = backend.kill_cost()
    assert cost["killed_never_finishing"] == 1
    assert cost["correct_kills"] == 0
    assert cost["wrong_kills"] == 0
    assert cost["saved_ms"] == 0, "no horizon, so remaining time is undefined"


@pytest.mark.asyncio
async def test_verdicts_are_reported_per_job_so_a_run_can_be_read_back():
    clock = _clock()
    backend = ScriptedJobBackend(
        clock,
        scripts={
            "doomed": JobScript(finish_after_ms=10_000, fail=True),
            "fine": JobScript(finish_after_ms=10_000, fail=False),
        },
    )
    doomed = await backend.submit(_spec("doomed"))
    fine = await backend.submit(_spec("fine"))
    clock.advance_ms(4_000)
    await backend.cancel(doomed)
    await backend.cancel(fine)

    verdicts = {v.idem_key: v for v in backend.kill_verdicts()}
    assert verdicts["doomed"].correct is True
    assert verdicts["doomed"].saved_ms == 6_000
    assert verdicts["fine"].correct is False
    assert verdicts["fine"].wasted_ms == 4_000
    assert verdicts["fine"].ran_ms == 4_000
