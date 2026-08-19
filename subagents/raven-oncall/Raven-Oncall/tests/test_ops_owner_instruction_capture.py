"""What the owner types has to reach the turn that needs it.

A wake turn is a cold start: the ledger, the events, the notes, nothing else. So
a sentence typed in the chat reaches the next round only if something writes it
down, and the only thing that could was the loop remembering to call ops_note.
Measured across four campaigns that asked the owner a question: four asked, zero
recorded. Every answer would have been invisible to the turn that needed it.

Every message is recorded, not the ones that look like instructions -- deciding
which sentence is an instruction is the same judgement that was being missed. An
owner types a handful of lines over an evening, against 38 wake replies on
2026-08-17; a stray "how is it going" costs a line and a lost "widen the angle
range" costs a round.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.loop.main import _capture_owner_instruction
from raven.spine import Origin


@pytest.fixture
def bound(tmp_path, monkeypatch):
    import raven.agent.tools.ops as ops
    from raven.ops.window import bind_window

    home = tmp_path / "ops"
    (home / "blade").mkdir(parents=True)
    (home / "blade" / "meta.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "_ops_home", lambda: home)
    bind_window(home, "tui:w1", "blade", pid=1)
    return home / "blade"


def _notes(cdir):
    p = cdir / "notes.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_a_message_from_a_bound_window_lands_in_the_campaign(bound):
    _capture_owner_instruction("tui:w1", "把角度范围扩到 60 度", Origin.USER)
    notes = _notes(bound)
    assert len(notes) == 1
    assert "60" in notes[0]["note"] and notes[0]["source"] == "owner"


def test_it_records_the_trial_count_so_an_old_instruction_can_be_told_apart(bound):
    """The one failure automatic capture introduces is acting on the same
    instruction at every wake. The count at the time is what settles it."""
    (bound / "ledger.json").write_text(json.dumps(
        {"version": 1, "records": {"a": {}, "b": {}, "c": {}}}), encoding="utf-8")
    _capture_owner_instruction("tui:w1", "再加两个角度", Origin.USER)
    assert _notes(bound)[0]["trials_at_the_time"] == 3


def test_a_window_watching_nothing_records_nothing(bound):
    _capture_owner_instruction("tui:unbound", "hello", Origin.USER)
    assert _notes(bound) == []


def test_a_wake_turn_is_not_the_owner_speaking(bound):
    """Its own prompt is generated, and recording it would fill the notes with
    the harness talking to itself."""
    _capture_owner_instruction("cron:job1", "[Ops campaign 'blade' round 1 due]", Origin.USER)
    assert _notes(bound) == []


def test_a_finished_campaign_takes_no_more_instructions(bound):
    (bound / "concluded.json").write_text(json.dumps({"outcome": "done"}), encoding="utf-8")
    _capture_owner_instruction("tui:w1", "再加两个角度", Origin.USER)
    assert _notes(bound) == []


def test_slash_commands_are_not_instructions(bound):
    _capture_owner_instruction("tui:w1", "/help", Origin.USER)
    assert _notes(bound) == []


def test_a_message_that_is_not_from_a_person_is_ignored(bound):
    _capture_owner_instruction("tui:w1", "subagent finished", Origin.SUBAGENT)
    assert _notes(bound) == []


# --- answering an outstanding question wakes it now ----------------------------

class _Cron:
    def __init__(self, jobs=()):
        self._jobs, self.advanced = list(jobs), []

    def list_jobs(self, **kw):
        return self._jobs

    def advance_job_to_now(self, job_id):
        self.advanced.append(job_id)
        return True


class _Job:
    def __init__(self, jid, name, at):
        self.id, self.name = jid, name
        self.state = type("S", (), {"next_run_at_ms": at})()


def _ask(cdir, ts="2026-08-18T10:00:00"):
    with open(cdir / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "kind": "ask_owner", "question": "which way?"}) + "\n")


async def test_answering_an_outstanding_question_pulls_the_wake_to_now(bound):
    """The owner is right here; sitting out the rest of a twenty-minute timer to
    hear an answer already given is the thing to avoid."""
    _ask(bound)
    cron = _Cron([_Job("j1", "ops:blade:r2", 999)])
    _capture_owner_instruction("tui:w1", "走第二个方向", Origin.USER, cron)
    assert cron.advanced == ["j1"]


async def test_a_volunteered_instruction_does_not_wake_it(bound):
    """No question outstanding: waking on every typed line would turn a stray
    "ok" into a round of work."""
    cron = _Cron([_Job("j1", "ops:blade:r2", 999)])
    _capture_owner_instruction("tui:w1", "顺便把角度范围扩大", Origin.USER, cron)
    assert cron.advanced == []


async def test_the_answer_closes_the_question(bound):
    """A second message must not wake it again for a question already answered."""
    _ask(bound)
    cron = _Cron([_Job("j1", "ops:blade:r2", 999)])
    _capture_owner_instruction("tui:w1", "走第二个", Origin.USER, cron)
    _capture_owner_instruction("tui:w1", "另外记得存图", Origin.USER, cron)
    assert cron.advanced == ["j1"], "only the answer wakes it, not what follows"


async def test_no_pending_wake_is_not_an_error(bound):
    """Mid-turn there may be no wake scheduled yet; the note is still recorded."""
    _ask(bound)
    cron = _Cron([])
    _capture_owner_instruction("tui:w1", "走第二个", Origin.USER, cron)
    assert _notes(bound) and cron.advanced == []
