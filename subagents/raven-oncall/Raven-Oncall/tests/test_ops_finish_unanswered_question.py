"""Closing is the one irreversible step, and an unanswered question is the
strongest reason not to take it.

Measured 2026-08-14: an arm asked the owner twice, heard nothing, and concluded
'done' with 84% of the budget unspent and its objective never measured. It
neither waited nor continued -- it quit, and the ledger shut with the budget
inside it. Waiting costs nothing by comparison: the campaign stays open and one
sentence restarts it.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.tools.ops_escalation import _unanswered_question


def _ev(cdir, **row):
    with open(cdir / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _note(cdir, ts, text="ok"):
    with open(cdir / "notes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "note": text, "source": "owner"}) + "\n")


def test_a_question_with_no_answer_is_outstanding(tmp_path):
    _ev(tmp_path, ts="2026-08-18T10:00:00", kind="ask_owner", question="which way?")
    assert "which way" in _unanswered_question(tmp_path)


def test_a_note_after_the_question_closes_it(tmp_path):
    """The owner's answer arrives as a note, captured from the chat."""
    _ev(tmp_path, ts="2026-08-18T10:00:00", kind="ask_owner", question="which way?")
    _note(tmp_path, "2026-08-18T10:05:00", "go the second way")
    assert _unanswered_question(tmp_path) == ""


def test_a_note_from_before_the_question_does_not_close_it(tmp_path):
    """Otherwise any earlier chatter would pass for an answer."""
    _note(tmp_path, "2026-08-18T09:00:00", "morning")
    _ev(tmp_path, ts="2026-08-18T10:00:00", kind="ask_owner", question="which way?")
    assert "which way" in _unanswered_question(tmp_path)


def test_a_refused_interruption_was_never_asked(tmp_path):
    """The contract can refuse to deliver; a question nobody saw cannot be one
    the owner failed to answer."""
    _ev(tmp_path, ts="2026-08-18T10:00:00", kind="ask_owner", allowed=False, question="trivial?")
    assert _unanswered_question(tmp_path) == ""


def test_a_campaign_that_never_asked_is_free_to_close(tmp_path):
    assert _unanswered_question(tmp_path) == ""


def test_the_latest_question_is_the_one_that_counts(tmp_path):
    _ev(tmp_path, ts="2026-08-18T10:00:00", kind="ask_owner", question="first?")
    _note(tmp_path, "2026-08-18T10:05:00")
    _ev(tmp_path, ts="2026-08-18T11:00:00", kind="ask_owner", question="second?")
    assert "second" in _unanswered_question(tmp_path)
