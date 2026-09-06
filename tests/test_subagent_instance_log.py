"""build_turn is the one recipe for a finished turn's message rows."""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent.subagent.instance_log import (
    append_turn,
    build_turn,
    instance_title,
    open_instance_log,
    transcript_path,
)


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


def test_an_instance_is_named_before_it_has_said_anything(tmp_path: Path) -> None:
    # The title used to arrive with the first turn, and the first turn is
    # written when the dispatch *finishes*. Every surface that heads a panel by
    # what the instance was dispatched for therefore fell back to the handle --
    # an id -- for exactly as long as the run was still going.
    open_instance_log(tmp_path, agent="Coder", handle="h1", session_key="s1", kind="spawn", title="做一版 PPT")

    assert instance_title(tmp_path, "Coder", "h1") == "做一版 PPT"


def test_opening_an_instance_twice_writes_one_header(tmp_path: Path) -> None:
    # An instance is reached through several lanes and a later dispatch of the
    # same handle joins its conversation. It must not rename it, and must not
    # leave a second metadata row in the middle of the file.
    open_instance_log(tmp_path, agent="Coder", handle="h1", session_key="s1", kind="spawn", title="first")
    open_instance_log(tmp_path, agent="Coder", handle="h1", session_key="s1", kind="dag", title="second")
    append_turn(tmp_path, agent="Coder", handle="h1", session_key="s1", kind="dag", prompt="go", answer="done")

    written = [
        json.loads(line)
        for line in transcript_path(tmp_path, "Coder", "h1").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [row.get("_type") for row in written].count("metadata") == 1
    assert instance_title(tmp_path, "Coder", "h1") == "first"


def test_an_unnamed_instance_still_opens(tmp_path: Path) -> None:
    # A direct chat has no line saying what the instance is for, and must not
    # invent one -- but the header it opens is still the file's first row.
    open_instance_log(tmp_path, agent="Coder", handle="h1", session_key="s1", kind="chat")

    assert transcript_path(tmp_path, "Coder", "h1").is_file()
    assert instance_title(tmp_path, "Coder", "h1") == ""
