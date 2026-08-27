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


class _Cron:
    """Records the wake a tool schedules, the way CronService would take it."""

    def __init__(self):
        self.jobs = []

    def add_job(self, **kw):
        self.jobs.append(kw)
        from types import SimpleNamespace

        return SimpleNamespace(id="job-1", name=kw.get("name", ""))

    def list_jobs(self, include_disabled=False):
        return []


def test_a_wait_that_ends_with_no_answer_schedules_the_next_look(campaign):
    """Otherwise the turn ends with nothing pending and the campaign never returns.

    Measured 2026-08-21: two campaigns asked a blocking question, waited out the
    broker, and sat untouched for three hours with 86% and 81% of their budgets
    unspent. The reply text promised "the owner's reply will reach the next
    round" -- there was no next round.
    """
    import asyncio

    _ledger(campaign, "succeeded")
    cron = _Cron()
    tool = OpsAskOwnerTool(registry=_Registry(_Msg()), broker=_Broker(""), cron_service=cron)
    # The wake's own door, not set_context: the loop deliberately never gives this
    # tool a delivery context, so a test that armed the wake through set_context
    # was testing a wiring production does not have.
    tool.set_wake_context("tui", "default", "tui:20260826_135122_5b9597")
    out = asyncio.run(tool.execute(
        campaign="blade", question="which way?", expected_loss_minutes=60,
        blocks_progress=True))

    assert cron.jobs, "nothing would have brought the campaign back"
    assert cron.jobs[0]["name"] == "ops:blade:after-ask"
    assert "ops_tune_status" in cron.jobs[0]["message"], "a wake turn's whole context is this text"
    assert "nobody answered" in out


def test_an_answered_wait_needs_no_wake(campaign):
    """The answer is in hand and the turn carries on with it."""
    import asyncio

    _ledger(campaign, "succeeded")
    cron = _Cron()
    tool = OpsAskOwnerTool(registry=_Registry(_Msg()), broker=_Broker("go left"), cron_service=cron)
    tool.set_wake_context("tui", "default", "tui:20260826_135122_5b9597")
    asyncio.run(tool.execute(
        campaign="blade", question="which way?", expected_loss_minutes=60,
        blocks_progress=True))

    assert cron.jobs == []


def test_a_non_blocking_ask_also_leaves_a_wake(campaign):
    """It said it had other work, but nothing guarantees it does any.

    Measured 2026-08-21: two of the four asks that day were non-blocking (the
    parameter is required and was not passed, so it defaulted), and both
    campaigns then sat untouched for three hours -- 86% and 81% of budget
    unspent -- because the turn ended with nothing pending.
    """
    import asyncio

    _ledger(campaign, "succeeded")
    cron = _Cron()
    tool = OpsAskOwnerTool(registry=_Registry(_Msg()), broker=None, cron_service=cron)
    tool.set_wake_context("tui", "default", "tui:20260826_135122_5b9597")
    asyncio.run(tool.execute(campaign="blade", question="which way?",
                             expected_loss_minutes=60, blocks_progress=False))

    assert cron.jobs, "a non-blocking ask still has to leave something pending"
    assert cron.jobs[0]["name"] == "ops:blade:after-ask"


def test_a_running_trial_keeps_its_own_wake(campaign):
    """The trial already has one set for when it should be done, and wakes are
    idempotent per campaign -- scheduling here would replace it with a sooner one
    and throw away the eta the loop reasoned about."""
    import asyncio

    _ledger(campaign, "running")
    cron = _Cron()
    tool = OpsAskOwnerTool(registry=_Registry(_Msg()), broker=None, cron_service=cron)
    tool.set_wake_context("tui", "default", "tui:20260826_135122_5b9597")
    asyncio.run(tool.execute(campaign="blade", question="which way?",
                             expected_loss_minutes=60, blocks_progress=False))

    assert cron.jobs == []


def test_the_loop_gives_the_ask_tool_its_wake_context() -> None:
    """The wiring the tests above assumed and production did not have.

    Every test in this file that exercises the safety wake sets the context by
    hand. That is legitimate as a unit, and it is also exactly how a feature ships
    green and dead: ``_update_tool_context`` drives a fixed list of tool names,
    ``ops_ask_owner`` is deliberately not on it (adopting the turn's channel would
    deliver a wake turn's question to "cron", which nothing subscribes to), and so
    the wake asked for on every ask was armed on none -- ``_schedule_ops_wake``
    refuses without a channel and reports the refusal as a returned string.

    Measured 2026-08-26: a campaign asked a blocking question with all four trials
    terminal, no wake in either store, and stopped there.

    So this pins the loop's side of it: the tool is handed a wake context, and its
    delivery context is still left alone.
    """
    from raven.agent.tools.ops_escalation import OpsAskOwnerTool

    tool = OpsAskOwnerTool(registry=_Registry(_Msg()))
    assert tool._wake_channel == "" and tool._session_key == ""

    from raven.agent.loop.main import AgentLoop

    class _Tools:
        def get(self, name):
            return tool if name == "ops_ask_owner" else None

    loop = AgentLoop.__new__(AgentLoop)
    loop.tools = _Tools()
    AgentLoop._set_tool_context(loop, "tui", "default", None, "tui:20260826_135122_5b9597", "")

    assert tool._wake_channel == "tui"
    assert tool._wake_chat_id == "default"
    assert tool._session_key == "tui:20260826_135122_5b9597"
    # Delivery is still deliberately unset, which is what keeps the question off
    # the "cron" channel.
    assert tool._channel == "" and tool._chat_id == ""
