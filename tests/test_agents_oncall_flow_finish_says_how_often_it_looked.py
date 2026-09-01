"""A delivered watch says how many times it looked.

The report gate already makes a conclusion checkable: ``observed`` says what was
seen, ``baseline`` what it started from. That is enough for a campaign that did
something -- and it does not separate the two endings of a campaign whose correct
outcome was to do nothing. A watch that looked every ten minutes for four hours
and never saw the condition hold, and one that never looked at all, file the same
report: "the condition never held, nothing done".

The count is the difference, and it is not the loop's to state -- a wake turn is a
cold start with no memory of the earlier looks. It is in the trail either way, so
it is read from there and attached. Measured on SentinelBench: 20 of 100 tasks
have "stay silent" as the right answer.

No verdict comes with it. Whether twelve looks over four hours was attentive
depends on what was being watched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.instrument import log_event, read_events  # noqa: E402
from oncall_flow.state_claims import StateFacts, write_facts  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops_escalation import OpsFinishTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def _watched(tmp_path: Path, *, looks: int, wakes: int, opened_minutes_ago: int = 0) -> Path:
    from datetime import datetime, timedelta

    cdir = tmp_path / "watch-volt"
    cdir.mkdir(exist_ok=True)
    opened = datetime.now() - timedelta(minutes=opened_minutes_ago)
    (cdir / "meta.json").write_text(
        json.dumps({"backend": "process", "declared_at": opened.isoformat(timespec="seconds")}),
        encoding="utf-8",
    )
    if looks:
        write_facts(cdir, StateFacts(probe_seq=looks))
    for _ in range(wakes):
        log_event(cdir, "check_later", eta_seconds=600)
    return cdir


async def _finish(cdir: Path, **over):
    tool = OpsFinishTool()
    tool.set_context("tui", "default")
    args = dict(
        campaign=cdir.name,
        subject="VOLT never dropped 10%",
        outcome="done",
        dedupe_key="k1",
        observed={"lowest_seen": 231.4, "threshold": 223.58},
        condition_type="absolute",
        narrative="Watched the price. It never reached the threshold, so no order was placed.",
        ledger=str(cdir / "ledger.json"),
    )
    args.update(over)
    return await tool.execute(**args)


@pytest.mark.asyncio
async def test_the_delivered_report_says_how_many_looks_it_took(tmp_path) -> None:
    cdir = _watched(tmp_path, looks=24, wakes=23, opened_minutes_ago=222)

    out = await _finish(cdir)

    assert "Accepted" in out
    assert "24 looks" in out
    assert "3h 42m" in out


@pytest.mark.asyncio
async def test_a_watch_that_never_looked_cannot_look_like_one_that_did(tmp_path) -> None:
    """The two endings this whole line exists to separate."""
    cdir = _watched(tmp_path, looks=0, wakes=0)

    out = await _finish(cdir)

    assert "watch kept" not in out


@pytest.mark.asyncio
async def test_the_count_is_in_the_report_file_too(tmp_path) -> None:
    """The file is what the owner opens a day later, and a wake turn after that."""
    cdir = _watched(tmp_path, looks=12, wakes=11, opened_minutes_ago=60)

    await _finish(cdir)

    written = next(cdir.glob("report-*.md")).read_text(encoding="utf-8")
    assert "12 looks" in written and "11 wakes arranged" in written


@pytest.mark.asyncio
async def test_the_count_is_in_the_trail_as_the_report_was_filed(tmp_path) -> None:
    """Same reading in both places: a report saying twelve and a trail saying nine
    would leave a reader unable to use either."""
    cdir = _watched(tmp_path, looks=9, wakes=8, opened_minutes_ago=30)

    await _finish(cdir)

    concluded = [e for e in read_events(cdir) if e.get("kind") == "concluded"]
    assert concluded and "9 looks" in concluded[-1].get("watch_kept", "")


@pytest.mark.asyncio
async def test_it_is_a_count_and_not_a_verdict(tmp_path) -> None:
    """Whether it looked often enough depends on what was being watched, and that
    is the reader's call, not this line's."""
    cdir = _watched(tmp_path, looks=2, wakes=1, opened_minutes_ago=600)

    out = await _finish(cdir)

    body = out.lower()
    assert "2 looks" in body
    for verdict in ("too few", "insufficient", "not enough", "diligent", "thorough"):
        assert verdict not in body
