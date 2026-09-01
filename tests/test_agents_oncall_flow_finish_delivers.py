"""Finishing a campaign puts the result in front of someone.

Of the three ways a wake turn can reach a person, only one arrived. Measured
2026-08-12 across four arms:

  * ``ops_ask_owner`` delivers -- it hands the text to the messaging tool, and the
    trail shows ``Message sent to tui:default``.
  * a wake turn's ordinary reply goes to ``tui:cron``, which the front end does
    not subscribe to. The user searched the TUI for one and could not find it.
  * ``ops_finish`` did not deliver at all. It wrote ``reports.jsonl`` and stopped.

So an overnight run showed the operator its questions and nothing else -- not what
it did, and not how it ended. On the CFD leg the whole hour surfaced as one line
("submitted and fixed one physics-quantity error"), and the three wake turns after it, including the
final report, were only in tui.log.

The report is the one message that has already been checked: it passed the
condition_type gate, the state-claim check, missing_fields and unmeasured_fields.
Sending something that has cleared all of that introduces no unverified content.

Delivery is best-effort and never blocks the close. A campaign that finished but
could not be announced is finished; leaving it open because a channel was down
would recreate the failure ops_finish exists to prevent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops_escalation import OpsFinishTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def _campaign(tmp_path: Path) -> Path:
    cdir = tmp_path / "c"
    cdir.mkdir(exist_ok=True)
    (cdir / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    return cdir


def _finish():
    t = OpsFinishTool()
    t.set_context("tui", "default")
    return t


@pytest.mark.asyncio
async def test_an_accepted_report_is_delivered(tmp_path):
    cdir = _campaign(tmp_path)
    out = await _finish().execute(
        campaign="c",
        subject="dambreak reached endTime",
        outcome="done",
        dedupe_key="k1",
        observed={"endTime_reached": 1.0, "core_minutes_used": 65.5},
        condition_type="absolute",
        narrative="The result is usable.",
        ledger=str(cdir / "ledger.json"),
    )

    assert "Accepted" in out
    assert "Deliver the report below" in out, "the close is the one message that must reach a person"
    assert "dambreak reached endTime" in out
    assert "endTime_reached" in out, "the readings, not just the headline"


@pytest.mark.asyncio
async def test_a_refused_report_is_not_delivered(tmp_path):
    """Nothing unchecked goes out. A refused report never happened."""
    cdir = _campaign(tmp_path)
    out = await _finish().execute(
        campaign="c",
        subject="s",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.36},
        condition_type="relative",  # relative with no baseline
        ledger=str(cdir / "ledger.json"),
    )

    assert "REFUSED" in out
    assert "Deliver the report below" not in out, "a refused report never happened"


@pytest.mark.asyncio
async def test_the_delivery_outcome_is_recorded(tmp_path):
    cdir = _campaign(tmp_path)
    await _finish().execute(
        campaign="c",
        subject="s",
        outcome="failed",
        dedupe_key="k1",
        condition_type="absolute",
        no_data_reason="every round OOMed at step 0",
        ledger=str(cdir / "ledger.json"),
    )

    kinds = [json.loads(l).get("kind") for l in (cdir / "events.jsonl").read_text().splitlines()]
    assert "report_delivery" in kinds, "whether the owner was told is itself a fact to keep"


@pytest.mark.asyncio
async def test_no_messaging_tool_still_closes(tmp_path):
    cdir = _campaign(tmp_path)
    t = OpsFinishTool()
    out = await t.execute(
        campaign="c",
        subject="s",
        outcome="stopped",
        dedupe_key="k1",
        observed={"x": 1},
        condition_type="absolute",
        ledger=str(cdir / "ledger.json"),
    )
    assert "Accepted" in out
    assert (cdir / "concluded.json").exists()
