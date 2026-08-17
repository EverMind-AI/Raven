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
("已提交并修好一个物理量错误"), and the three wake turns after it, including the
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
from pathlib import Path

import pytest

from raven.agent.tools.ops_escalation import OpsFinishTool


class _Msg:
    def __init__(self, reply="Message sent to tui:default", raises=False):
        self.reply, self.raises = reply, raises
        self.sent: list[dict] = []

    async def execute(self, content, channel=None, chat_id=None, **kw):
        if self.raises:
            raise RuntimeError("gateway down")
        self.sent.append({"content": content, "channel": channel, "chat_id": chat_id})
        return self.reply


class _Registry:
    def __init__(self, msg):
        self._msg = msg

    def get(self, name):
        return self._msg if name == "message" else None


class _Cron:
    def __init__(self):
        self.names = ["ops:c:r1"]

    def list_jobs(self):
        from types import SimpleNamespace

        return [SimpleNamespace(id=str(i), name=n) for i, n in enumerate(self.names)]

    def remove_job(self, job_id):
        del self.names[int(job_id)]
        return True


def _campaign(tmp_path: Path) -> Path:
    cdir = tmp_path / "c"
    cdir.mkdir(exist_ok=True)
    (cdir / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    return cdir


def _finish(msg, cron=None):
    t = OpsFinishTool(cron_service=cron or _Cron())
    t.set_registry(_Registry(msg))
    t.set_context("tui", "default")
    return t


@pytest.mark.asyncio
async def test_an_accepted_report_is_delivered(tmp_path):
    cdir = _campaign(tmp_path)
    msg = _Msg()
    out = await _finish(msg).execute(
        campaign="c",
        subject="dambreak 跑到 endTime",
        outcome="done",
        dedupe_key="k1",
        observed={"endTime_reached": 1.0, "core_minutes_used": 65.5},
        condition_type="absolute",
        narrative="结果可用。",
        ledger=str(cdir / "ledger.json"),
    )

    assert "Accepted" in out
    assert len(msg.sent) == 1, "the close is the one message that must reach a person"
    body = msg.sent[0]["content"]
    assert "dambreak 跑到 endTime" in body
    assert "endTime_reached" in body, "the readings, not just the headline"
    assert msg.sent[0]["channel"] == "tui" and msg.sent[0]["chat_id"] == "default"


@pytest.mark.asyncio
async def test_a_refused_report_is_not_delivered(tmp_path):
    """Nothing unchecked goes out. A refused report never happened."""
    cdir = _campaign(tmp_path)
    msg = _Msg()
    out = await _finish(msg).execute(
        campaign="c",
        subject="s",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.36},
        condition_type="relative",  # relative with no baseline
        ledger=str(cdir / "ledger.json"),
    )

    assert "REFUSED" in out
    assert msg.sent == []


@pytest.mark.asyncio
async def test_delivery_failure_does_not_block_the_close(tmp_path):
    """A campaign that finished but could not be announced is still finished --
    leaving it open would recreate the failure this tool exists to prevent."""
    cdir = _campaign(tmp_path)
    msg = _Msg(raises=True)
    cron = _Cron()
    out = await _finish(msg, cron).execute(
        campaign="c",
        subject="s",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.36},
        condition_type="absolute",
        ledger=str(cdir / "ledger.json"),
    )

    assert "Accepted" in out
    assert (cdir / "concluded.json").exists(), "the close must not depend on the channel"
    assert cron.names == [], "and the wakes must still stand down"
    assert "could not be delivered" in out or "not delivered" in out, "but the reply has to say the person was not told"


@pytest.mark.asyncio
async def test_the_delivery_outcome_is_recorded(tmp_path):
    cdir = _campaign(tmp_path)
    msg = _Msg()
    await _finish(msg).execute(
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
async def test_it_does_not_adopt_the_wake_turn_channel(tmp_path):
    """A wake turn runs on channel "cron", which the front end does not subscribe
    to. Delivering there would announce the result into the one place nobody
    reads -- so with no context set, the messaging tool's own default target is
    used, which is how ops_ask_owner has been reaching tui:default all along."""
    cdir = _campaign(tmp_path)
    msg = _Msg()
    t = OpsFinishTool(cron_service=_Cron(), registry=_Registry(msg))  # no set_context
    await t.execute(
        campaign="c",
        subject="s",
        outcome="done",
        dedupe_key="k1",
        observed={"x": 1},
        condition_type="absolute",
        ledger=str(cdir / "ledger.json"),
    )
    assert msg.sent[0]["channel"] is None
    assert msg.sent[0]["chat_id"] is None


@pytest.mark.asyncio
async def test_no_messaging_tool_still_closes(tmp_path):
    cdir = _campaign(tmp_path)
    t = OpsFinishTool(cron_service=_Cron())  # no registry set at all
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
