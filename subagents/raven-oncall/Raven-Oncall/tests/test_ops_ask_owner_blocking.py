"""Whether to wait is derived from two facts, not chosen from two tools.

The loop answers: is anything burning machine time, and is anything worth doing
left until this is answered. Waiting is right only where it costs nothing --
nothing running and nothing else worth doing -- and there it is clearly right,
because the owner is looking at the window and a twenty-minute timer would be
the only thing between them and an answer they already have.

Blocking is about spending, not about thinking. Reading logs, reading the case
and writing down what was found stay open; submitting more trials does not.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.tools.ops_escalation import (
    OpsAskOwnerTool, _anything_running, blocking_question_open,
)


class _Broker:
    def __init__(self, answer=""):
        self.answer, self.asked = answer, []

    async def await_question(self, conversation_id, *, prompt, choices=None, default="", **kw):
        self.asked.append(prompt)
        return self.answer


class _Msg:
    def __init__(self):
        self.sent = []

    async def execute(self, content, channel=None, chat_id=None, **kw):
        self.sent.append(content)
        return "Message sent to tui:default"


class _Registry:
    def __init__(self, msg):
        self._msg = msg

    def get(self, name):
        return self._msg if name == "message" else None


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    d = tmp_path / "blade"
    d.mkdir()
    (d / "meta.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("raven.agent.tools.ops_escalation._campaign_dir", lambda c, l=None: d)
    return d


def _ledger(d, *statuses):
    (d / "ledger.json").write_text(json.dumps({"version": 1, "records": {
        f"t{i}": {"status": s} for i, s in enumerate(statuses)}}), encoding="utf-8")


def _events(d):
    p = d / "events.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_nothing_running_and_blocking_waits_for_the_answer(campaign):
    _ledger(campaign, "succeeded")
    broker, msg = _Broker("go the second way"), _Msg()
    out = pytest.run = None
    import asyncio
    out = asyncio.run(OpsAskOwnerTool(registry=_Registry(msg), broker=broker).execute(
        campaign="blade", question="which way?", expected_loss_minutes=60,
        blocks_progress=True))
    assert broker.asked, "it should have waited"
    assert "second way" in out
    assert msg.sent == [], "waiting inline replaces posting"


def test_the_inline_answer_lands_in_the_notes(campaign):
    """The next wake reads notes, not this turn -- an answer only heard here
    would be lost exactly as a chat message would."""
    import asyncio

    _ledger(campaign, "succeeded")
    asyncio.run(OpsAskOwnerTool(registry=_Registry(_Msg()), broker=_Broker("use 1e-6")).execute(
        campaign="blade", question="which nu?", expected_loss_minutes=60, blocks_progress=True))
    notes = [json.loads(l) for l in (campaign / "notes.jsonl").read_text().splitlines()]
    assert notes and "1e-6" in notes[0]["note"] and notes[0]["source"] == "owner"


def test_a_trial_still_running_does_not_wait(campaign):
    """Holding here would buy nothing: the job burns either way, and the work
    that could have happened meanwhile would not."""
    import asyncio

    _ledger(campaign, "running")
    broker, msg = _Broker("answer"), _Msg()
    asyncio.run(OpsAskOwnerTool(registry=_Registry(msg), broker=broker).execute(
        campaign="blade", question="which way?", expected_loss_minutes=60, blocks_progress=True))
    assert broker.asked == [] and msg.sent, "posted, not waited"


def test_a_non_blocking_question_does_not_wait(campaign):
    import asyncio

    _ledger(campaign, "succeeded")
    broker, msg = _Broker("answer"), _Msg()
    asyncio.run(OpsAskOwnerTool(registry=_Registry(msg), broker=broker).execute(
        campaign="blade", question="which way?", expected_loss_minutes=60, blocks_progress=False))
    assert broker.asked == [] and msg.sent


def test_the_reading_is_recorded_either_way(campaign):
    """blocks_progress is the loop's own judgement and the thing to score it on."""
    import asyncio

    _ledger(campaign, "running")
    asyncio.run(OpsAskOwnerTool(registry=_Registry(_Msg())).execute(
        campaign="blade", question="q", expected_loss_minutes=60, blocks_progress=True))
    asks = [e for e in _events(campaign) if e["kind"] == "ask_owner"]
    assert asks and asks[-1]["blocks_progress"] is True


def test_anything_running_reads_the_ledger(campaign):
    _ledger(campaign, "succeeded", "failed")
    assert _anything_running(campaign) is False
    _ledger(campaign, "succeeded", "running")
    assert _anything_running(campaign) is True


def test_only_a_blocking_question_closes_the_submit_door(campaign):
    import asyncio

    _ledger(campaign, "running")
    asyncio.run(OpsAskOwnerTool(registry=_Registry(_Msg())).execute(
        campaign="blade", question="pick one", expected_loss_minutes=60, blocks_progress=False))
    assert blocking_question_open(campaign) == ""

    asyncio.run(OpsAskOwnerTool(registry=_Registry(_Msg())).execute(
        campaign="blade", question="budget is out, go on?", expected_loss_minutes=60,
        blocks_progress=True))
    assert "budget" in blocking_question_open(campaign)
