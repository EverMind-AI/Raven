"""Tests for the wake-driven CampaignDriver.

The point of B: the campaign advances only when woken (a job-completion signal
or a tick), reusing the proactive wake primitives, instead of busy-polling. A
notify enqueues one deduped ops event and requests a wake; a pump runs exactly
one reconcile and acks; serve idles between wakes and completes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from raven.ops import Campaign, JobPlan, Ledger, MockJobBackend, Trial
from raven.ops.driver import CampaignDriver
from raven.proactive_engine.wake import WakeScheduler

_TRIALS = [Trial("t1"), Trial("t2"), Trial("t3")]


def _backend() -> MockJobBackend:
    return MockJobBackend(
        plans={
            "t1": JobPlan(metrics={"ndcg": 0.61}),
            "t2": JobPlan(metrics={"ndcg": 0.74}),
            "t3": JobPlan(metrics={"ndcg": 0.68}),
        }
    )


def _driver(tmp_path: Path) -> CampaignDriver:
    campaign = Campaign("bm25", _TRIALS, _backend(), Ledger(tmp_path / "l.json"), metric="ndcg")
    return CampaignDriver(campaign, wake=WakeScheduler(coalesce_s=0.0))


async def test_notify_enqueues_one_deduped_event_and_requests_wake(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    driver.notify("job-done:t1")
    driver.notify("job-done:t2")

    assert len(driver.events) == 1
    assert driver.events.peek_all()[0].context_key == "ops:bm25"

    await asyncio.sleep(0.02)
    assert driver.wake.wake_event.is_set()


async def test_pump_reconciles_and_acks_until_done(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    done = False
    for _ in range(10):
        driver.notify("tick")
        done = await driver.pump()
        if done:
            break

    assert done
    assert driver._campaign.best().idem_key == "t2"
    assert len(driver.events) == 0


async def test_serve_idles_between_wakes_and_completes(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    server = asyncio.create_task(driver.serve())

    for _ in range(10):
        if driver._campaign.is_done():
            break
        driver.notify("tick")
        await asyncio.sleep(0.02)

    await asyncio.wait_for(server, timeout=2)
    assert driver._campaign.is_done()
    assert driver._campaign.best().idem_key == "t2"
