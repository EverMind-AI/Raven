"""What a stored transcript says about the turn it holds: did a tool answer it, and is it taken back?"""

import json

import pytest

from raven.rpc.methods.session import _map_to_wire
from raven.session.turn_shape import REPLIED_VIA_TOOL, TakeBack, TakeBackStep, replied_via_message_tool


def _message_call() -> dict:
    arguments = '{"content": "3 tests failed"}'
    return {"id": "m1", "type": "function", "function": {"name": "message", "arguments": arguments}}


def _turn(*, answered: bool, sent: str = "Message sent to cron:job") -> list[dict]:
    closing = {"role": "assistant", "content": "[SILENT]", "silent": True}
    if answered:
        closing["replied_via_tool"] = True
    return [
        {"role": "user", "content": "[Scheduled Task] check the build", "origin": "cron"},
        {"role": "assistant", "content": "", "tool_calls": [_message_call()]},
        {"role": "tool", "content": sent, "tool_call_id": "m1", "name": "message"},
        closing,
    ]


def test_a_stored_turn_is_answered_by_its_closing_rows_verdict():
    assert replied_via_message_tool(_turn(answered=True)[-1])


def test_a_wire_turn_is_answered_by_the_same_verdict():
    assert replied_via_message_tool(_map_to_wire(_turn(answered=True), "cron:job")[-1])


def test_a_message_call_that_failed_did_not_answer_in_either_shape():
    stored = _turn(answered=False, sent="Error sending message: the channel is down")
    assert not any(replied_via_message_tool(row) for row in stored)
    assert not any(replied_via_message_tool(row) for row in _map_to_wire(stored, "cron:job"))


def test_only_an_assistant_row_carries_the_verdict():
    assert not replied_via_message_tool({"role": "user", "content": "hi", "replied_via_tool": True})
    assert not replied_via_message_tool({"role": "assistant", "content": "[SILENT]", "replied_via_tool": "yes"})


def _steps(rows: list[dict]) -> list[TakeBackStep | None]:
    reader = TakeBack()
    return [reader.read(row) for row in rows]


def test_a_turn_the_runtime_opened_and_closed_on_its_marker_is_taken_back():
    assert _steps(_turn(answered=False)) == [TakeBackStep.OPENS, None, None, TakeBackStep.TAKES_BACK]


def test_a_turn_a_tool_answered_is_kept_whatever_it_ends_on():
    assert TakeBackStep.TAKES_BACK not in _steps(_turn(answered=True))


def test_a_persons_turn_is_kept_when_the_model_answers_it_with_silence():
    rows = [{"role": "user", "content": "anything new?"}, {"role": "assistant", "content": "[SILENT]", "silent": True}]
    assert _steps(rows) == [None, None]


def test_the_marker_closes_the_turn_it_takes_back():
    """A row after the marker is no part of the turn it closed, so a second marker before the next user
    row takes nothing back."""
    rows = [*_turn(answered=False), {"role": "assistant", "content": "[SILENT]", "silent": True}]
    assert _steps(rows)[-2:] == [TakeBackStep.TAKES_BACK, None]


_PRIOR = [
    {"role": "user", "content": "hi", "timestamp": "2026-10-30T01:00:00"},
    {"role": "assistant", "content": "hello", "timestamp": "2026-10-30T01:00:01"},
]


def _timed(rows: list[dict]) -> list[dict]:
    return [{**row, "timestamp": f"2026-10-30T02:00:{second:02d}"} for second, row in enumerate(rows)]


_MARKER = {"role": "assistant", "content": "[SILENT]", "silent": True}
_OPENED = {"role": "user", "content": "[Scheduled Task] check the build", "origin": "cron"}
_SHAPES = {
    "the_runtime_opened_it_and_it_ended_silent": _turn(answered=False),
    "a_tool_answered_it": _turn(answered=True),
    "a_person_opened_it": [{"role": "user", "content": "anything new?"}, _MARKER],
    "it_answered_in_words": [_OPENED, {"role": "assistant", "content": "Report."}],
    "a_row_follows_its_marker": [_OPENED, _MARKER, {"role": "assistant", "content": "Noted."}, _MARKER],
}


def _live(row: dict) -> dict:
    from raven.agent.loop._shared import _ORIGIN_KEY, _REPLIED_VIA_TOOL_KEY, _SILENT_KEY

    names = {"origin": _ORIGIN_KEY, "silent": _SILENT_KEY, REPLIED_VIA_TOOL: _REPLIED_VIA_TOOL_KEY}
    return {names.get(key, key): value for key, value in row.items()}


def _scanned(path, rows: list[dict]) -> tuple[str, str]:
    from raven.session.manager import SessionManager

    lines = [{"_type": "metadata", "key": "cron:job"}, *rows]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    _meta, _count, last_ts, _first, preview = SessionManager._scan_file(path)
    return last_ts, preview


_NUDGE = {"role": "user", "content": "Go on.", "_recovery_synthetic": True}


@pytest.mark.parametrize("opened_by_an_earlier_write", [False, True], ids=["opened_now", "opened_earlier"])
@pytest.mark.parametrize("nudged", [False, True], ids=["", "nudged"])
@pytest.mark.parametrize("shape", list(_SHAPES), ids=list(_SHAPES))
def test_the_live_turn_the_session_list_and_the_replay_take_back_the_same_rows(
    tmp_path, shape, nudged, opened_by_an_earlier_write
):
    """Three readers answer one question about a stored turn: the ACP replay draws what stays, the
    session list orders sessions by it, and the live turn files a notice after its marker only where the
    marker stays. Each answers at the level it reads, and the answers have to agree. The live turn reads
    rows the store never sees -- a recovery nudge -- and may find its opening row filed by an earlier write."""
    from raven.acp.replay import _without_silent_runtime_turns
    from raven.agent.loop.turn_path import _taken_back_whole

    rows = [*_PRIOR, *_timed(_SHAPES[shape])]
    closing = [row for row in rows if row["role"] == "assistant"][-1]
    kept = _without_silent_runtime_turns(rows)

    live = [*_PRIOR, *(_live(row) for row in rows[len(_PRIOR) :])]
    if nudged:
        live.insert(len(live) - 1, dict(_NUDGE))
    skip = len(_PRIOR) + (1 if opened_by_an_earlier_write else 0)
    assert _taken_back_whole(live, skip) is all(row is not closing for row in kept)
    assert _scanned(tmp_path / "all.jsonl", rows) == _scanned(tmp_path / "kept.jsonl", kept)
