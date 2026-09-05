"""build_turn is the one recipe for a finished turn's message rows."""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent.subagent.instance_log import append_turn, build_turn, transcript_path


def test_turn_is_prompt_then_work_then_answer() -> None:
    turn = build_turn(
        prompt="read the readme",
        messages=[{"role": "assistant", "content": "looking"}],
        answer="there is no readme",
    )
    assert [row["role"] for row in turn] == ["user", "assistant", "assistant"]
    assert turn[0]["content"] == "read the readme"
    assert turn[-1]["content"] == "there is no readme"


def test_rows_built_by_turn_are_timestamped() -> None:
    turn = build_turn(prompt="hi", answer="hello")
    assert len(turn) == 2
    assert all(row["timestamp"] for row in turn)


def test_messages_rows_pass_through_with_or_without_timestamp() -> None:
    # Transcript producers attach a timestamp only when the underlying event
    # carries a wall clock. Rows arriving without one are legitimate.
    row_with_timestamp = {"role": "assistant", "content": "x", "timestamp": "2026-01-01T00:00:00"}
    row_without_timestamp = {"role": "assistant", "content": "y"}
    turn = build_turn(prompt="ask", messages=[row_with_timestamp, row_without_timestamp])
    assert turn[1] == row_with_timestamp
    assert turn[2] == row_without_timestamp


def test_non_dict_messages_are_dropped() -> None:
    turn = build_turn(prompt="hi", messages=["not a dict", {"role": "tool", "content": "x"}])  # type: ignore[list-item]
    assert [row["role"] for row in turn] == ["user", "tool"]


def test_error_is_recorded_as_a_failed_answer() -> None:
    turn = build_turn(prompt="hi", error="boom")
    assert turn[-1]["role"] == "assistant"
    assert turn[-1]["content"] == "[failed] boom"


def test_omitted_parts_add_no_rows() -> None:
    assert build_turn() == []
    assert [row["role"] for row in build_turn(messages=[{"role": "tool", "content": "x"}])] == ["tool"]


def test_append_turn_writes_exactly_what_build_turn_returns(tmp_path: Path) -> None:
    # The whole point of the extraction: the payload handed to everos and the
    # rows written to the log must be the same turn, or the memory would
    # describe a conversation no file on disk agrees with.
    kwargs = {
        "prompt": "read the readme",
        "messages": [{"role": "assistant", "content": "looking"}],
        "answer": "there is no readme",
    }
    append_turn(tmp_path, agent="Coder", handle="h1", session_key="s1", kind="spawn", **kwargs)
    written = [
        json.loads(line)
        for line in transcript_path(tmp_path, "Coder", "h1").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = [row for row in written if row.get("_type") != "metadata"]
    expected = build_turn(**kwargs)
    assert [(r["role"], r["content"]) for r in rows] == [(e["role"], e["content"]) for e in expected]
