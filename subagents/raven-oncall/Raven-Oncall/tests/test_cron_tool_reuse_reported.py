"""The cron tool says when an add reused an existing job instead of creating one.

Measured 2026-08-06 on the CFD line: a turn asked for a check 28 seconds away from
a pending on-call wake, dedup returned that wake, and the tool answered "Created
job 'ops:cfd-transient:r1'". The turn then held a job it had never asked for that
claimed to be brand new, judged it a duplicate, and removed it. The campaign was
left with no pending wake at all; the solver finished that night and nothing
reported it.

Every step the loop took was correct given what it was told. The defect is in what
it was told, so the fix is in the sentence, not in a guard on the removal.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _tool(tmp_path: Path):
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.proactive_engine.schedulers.cron.tool import CronTool

    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    tool = CronTool(svc)
    for attr, value in (("_channel", "tui"), ("_chat_id", "default")):
        if hasattr(tool, attr):
            setattr(tool, attr, value)
    if hasattr(tool, "set_context"):
        tool.set_context("tui", "default")
    return tool, svc


@pytest.mark.asyncio
async def test_a_fresh_add_is_still_reported_as_created(tmp_path: Path) -> None:
    tool, svc = _tool(tmp_path)

    out = await tool.execute(action="add", at="2099-01-01T00:00:00", message="water the plants")

    assert "Created job" in out
    assert len(svc.list_jobs()) == 1


@pytest.mark.asyncio
async def test_a_deduped_add_says_it_reused_and_names_no_creation(tmp_path: Path) -> None:
    tool, svc = _tool(tmp_path)
    await tool.execute(action="add", at="2099-01-01T00:00:00", message="water the plants")

    out = await tool.execute(action="add", at="2099-01-01T00:00:00", message="water the plants")

    assert "Created job" not in out
    assert "reused" in out
    assert len(svc.list_jobs()) == 1


@pytest.mark.asyncio
async def test_a_deduped_add_tells_the_caller_not_to_remove_it(tmp_path: Path) -> None:
    """The 2026-08-06 removal is the failure this sentence exists to prevent, so the
    instruction is asserted rather than left to the wording of the moment."""
    tool, _ = _tool(tmp_path)
    await tool.execute(action="add", at="2099-01-01T00:00:00", message="water the plants")

    out = await tool.execute(action="add", at="2099-01-01T00:00:00", message="water the plants")

    assert "Do not remove it" in out


@pytest.mark.asyncio
async def test_the_reused_job_id_is_the_one_that_will_fire(tmp_path: Path) -> None:
    """The id alone is asserted alongside the reuse wording on purpose: the old
    sentence named the same id under "Created", so an id-only assertion passes with
    the defect in place. Checked by removing the branch -- this has to go red too."""
    tool, svc = _tool(tmp_path)
    await tool.execute(action="add", at="2099-01-01T00:00:00", message="water the plants")
    first = svc.list_jobs()[0]

    out = await tool.execute(action="add", at="2099-01-01T00:00:00", message="water the plants")

    assert "reused" in out and first.id in out
    assert [j.id for j in svc.list_jobs()] == [first.id]
