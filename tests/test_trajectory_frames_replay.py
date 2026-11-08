"""Replaying an external ACP run from its wire journal."""

import asyncio
import json
from pathlib import Path

import pytest

from raven.trajectory import frames_replay


def _journal(tmp_path: Path) -> Path:
    lines = []

    def frame(direction: str, body: dict) -> None:
        lines.append(
            json.dumps({"_type": "acp_frame", "timestamp": "2026-10-27T09:00:00", "dir": direction, "frame": body})
        )

    frame(
        "out", {"method": "session/prompt", "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "do it"}]}}
    )
    for chunk in ("I will ", "read it"):
        frame(
            "in",
            {
                "method": "session/update",
                "params": {
                    "sessionId": "s",
                    "update": {"sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": chunk}]},
                },
            },
        )
    frame(
        "in",
        {
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "c1",
                    "title": "read_file",
                    "kind": "read",
                    "status": "in_progress",
                    "rawInput": {"path": "a.py"},
                },
            },
        },
    )
    frame(
        "in",
        {
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c1",
                    "status": "completed",
                    "rawOutput": "contents",
                },
            },
        },
    )
    frame(
        "in",
        {
            "method": "session/update",
            "params": {
                "sessionId": "other",
                "update": {"sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": "not ours"}]},
            },
        },
    )
    path = tmp_path / "journal.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_replay_reads_the_runs_own_stretch(tmp_path: Path):
    messages = frames_replay.replay(_journal(tmp_path), session_id="s")
    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
    assert messages[0]["content"] == "do it"
    assert messages[1]["content"] == "I will read it"
    assert messages[1]["tool_calls"][0]["function"]["arguments"] == '{"path": "a.py"}'
    assert messages[2]["content"] == "contents"


def test_a_byte_range_keeps_the_run_it_names_and_leaves_the_others(tmp_path: Path):
    """The range is what makes one call's stretch readable out of a connection's
    journal: a prompt before ``start`` and one after ``end`` both belong to other
    runs, and neither may leak into this one."""
    path = tmp_path / "three.jsonl"
    text = ""

    def frame(direction: str, body: dict) -> None:
        nonlocal text
        text += (
            json.dumps({"_type": "acp_frame", "timestamp": "2026-10-27T09:00:00", "dir": direction, "frame": body})
            + "\n"
        )

    def prompt(said: str) -> None:
        frame(
            "out",
            {"method": "session/prompt", "params": {"sessionId": "s", "prompt": [{"type": "text", "text": said}]}},
        )

    def say(said: str) -> None:
        frame(
            "in",
            {
                "method": "session/update",
                "params": {
                    "sessionId": "s",
                    "update": {"sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": said}]},
                },
            },
        )

    prompt("EARLIER")
    say("OLD")
    start = len(text.encode())
    prompt("TARGET")
    say("KEEP")
    frame(
        "in",
        {
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "c1",
                    "title": "read_file",
                    "kind": "read",
                    "status": "in_progress",
                    "rawInput": {"path": "a.py"},
                },
            },
        },
    )
    frame(
        "in",
        {
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c1",
                    "status": "completed",
                    "rawOutput": "DONE",
                },
            },
        },
    )
    end = len(text.encode())
    prompt("LATER")
    say("DROP")
    path.write_text(text, encoding="utf-8", newline="")

    messages = frames_replay.replay(path, session_id="s", start=start, end=end)
    assert [message["content"] for message in messages] == ["TARGET", "KEEP", "DONE"]
    assert not any(
        "OLD" in str(message.get("content")) or "DROP" in str(message.get("content")) for message in messages
    )

    # The control: ignoring both edges would read all three runs as one. The
    # provider-row projection drops a say that follows a settled tool call, so
    # the control reads the events rather than the rows.
    _, whole_events = frames_replay.events(path, session_id="s")
    whole_text = " ".join(str(event.get("text")) for event in whole_events)
    assert "OLD" in whole_text and "DROP" in whole_text


def test_a_windows_journal_written_with_crlf_keeps_the_runs_tail(tmp_path: Path):
    """A journal written before the writer stopped translating newlines records
    LF byte offsets in a CRLF file. The offset a call stored is one byte per
    preceding line short of the file's real position, so a raw seek drops the
    run's last frames -- the Replay block and the backfill both read this way."""
    path = _journal(tmp_path)
    logical = path.read_bytes()
    crlf = tmp_path / "crlf.jsonl"
    crlf.write_bytes(logical.replace(b"\n", b"\r\n"))

    messages = frames_replay.replay(crlf, session_id="s", start=0, end=len(logical))
    assert [message["role"] for message in messages] == ["user", "assistant", "tool"]
    assert messages[-1]["content"] == "contents"


def _codex_journal(tmp_path: Path, *, handshake: bool) -> tuple[Path, int]:
    text = ""

    def line(direction: str, frame: dict) -> None:
        nonlocal text
        text += (
            json.dumps({"_type": "acp_frame", "timestamp": "2026-10-27T09:00:00", "dir": direction, "frame": frame})
            + "\n"
        )

    if handshake:
        line("out", {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        line("in", {"jsonrpc": "2.0", "id": 0, "result": {"agentInfo": {"name": "codex-acp"}}})
    handshake_end = len(text.encode())
    line(
        "out",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/prompt",
            "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "run it"}]},
        },
    )
    line(
        "in",
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "c1",
                    "title": "lint",
                    "kind": "execute",
                    "status": "in_progress",
                    "rawInput": {"command": ["sh", "-c", "exit 1"]},
                },
            },
        },
    )
    line(
        "in",
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c1",
                    "status": "completed",
                    "rawOutput": {"formatted_output": "", "exit_code": 1},
                },
            },
        },
    )
    path = tmp_path / "codex.jsonl"
    path.write_text(text, encoding="utf-8", newline="")
    return path, handshake_end


def test_a_recorded_initialize_names_the_dialect_when_the_journal_has_no_handshake(tmp_path: Path):
    """An adopted connection's journal starts at the run, so the handshake the
    live turn read is not in the file. The run records it; replay must use it."""
    path, _ = _codex_journal(tmp_path, handshake=False)
    _, events = frames_replay.events(
        path, session_id="s", initialize={"agentInfo": {"name": "codex-acp"}}, handshake_end=0
    )
    result = next(event for event in events if event.get("t") == "result")
    assert result["ok"] is False

    # The control: without the recorded handshake the spec dialect says completed.
    _, base = frames_replay.events(path, session_id="s", handshake_end=0)
    assert next(event for event in base if event.get("t") == "result")["ok"] is True


def test_an_old_run_without_handshake_end_reads_the_handshake_from_its_own_journal(tmp_path: Path):
    path, handshake_end = _codex_journal(tmp_path, handshake=True)
    _, events = frames_replay.events(path, session_id="s", start=handshake_end)
    assert next(event for event in events if event.get("t") == "result")["ok"] is False


def test_items_split_the_run_by_what_each_row_is(tmp_path: Path):
    rows = frames_replay.items(frames_replay.replay(_journal(tmp_path), session_id="s"))
    assert [r["key"] for r in rows] == ["llm.input", "llm.output", "tool.input", "tool.output"]
    assert rows[0]["value"] == "do it"
    assert rows[1]["value"] == "I will read it"
    assert rows[2]["value"].startswith("read ")
    assert rows[3]["value"] == "contents"


def test_a_message_that_thought_first_keeps_its_thinking_row(tmp_path: Path):
    path = tmp_path / "j.jsonl"
    path.write_text(
        json.dumps(
            {
                "_type": "acp_frame",
                "timestamp": "2026-10-27T09:00:00",
                "dir": "in",
                "frame": {
                    "method": "session/update",
                    "params": {
                        "sessionId": "s",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": [{"type": "text", "text": "answer"}],
                        },
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rows = frames_replay.items([{"role": "assistant", "content": "answer", "reasoning_content": "hmm"}])
    assert [r["key"] for r in rows] == ["llm.thinking", "llm.output"]


def test_items_clip_a_long_part_and_say_so():
    rows = frames_replay.items([{"role": "tool", "content": "x" * 30_000}])
    assert rows[0]["value"].startswith("x" * 20_000)
    assert rows[0]["value"].endswith("(truncated)")
    assert len(rows[0]["value"]) < 20_100


def test_a_missing_journal_replays_nothing(tmp_path: Path):
    assert frames_replay.replay(tmp_path / "absent.jsonl", session_id="s") == []


_PROMPT = "fix the import"


def _frame_line(direction: str, body: dict) -> str:
    return (
        json.dumps({"_type": "acp_frame", "timestamp": "2026-10-27T09:00:00", "dir": direction, "frame": body}) + "\n"
    )


def _update(session: str, update: dict) -> dict:
    return {"method": "session/update", "params": {"sessionId": session, "update": update}}


def _say(text: str) -> dict:
    return {"sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": text}]}


def _run_journal(tmp_path: Path, agent: str, frames: list[dict], *, history: list[dict] = ()) -> tuple[Path, int]:
    """A journal laid out the way a connection writes one: the handshake, then
    (for a resumed session) the load and the history it replays, then the turn."""
    text = _frame_line("out", {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": 1}})
    text += _frame_line(
        "in",
        {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": 1, "agentInfo": {"name": agent, "version": "1"}}},
    )
    handshake_end = len(text.encode("utf-8"))
    if history:
        text += _frame_line("out", {"jsonrpc": "2.0", "id": 1, "method": "session/load", "params": {"sessionId": "s"}})
        for update in history:
            text += _frame_line("in", _update("s", update))
        text += _frame_line("in", {"jsonrpc": "2.0", "id": 1, "result": {}})
    text += _frame_line(
        "out",
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {"sessionId": "s", "prompt": [{"type": "text", "text": _PROMPT}]},
        },
    )
    for body in frames:
        text += _frame_line("in", body)
    path = tmp_path / "journal.jsonl"
    path.write_text(text, encoding="utf-8")
    return path, handshake_end


_PARITY_CASES = {
    "plan": (
        "claude-agent-acp",
        [
            _update("s", _say("Planning.")),
            _update(
                "s",
                {
                    "sessionUpdate": "plan",
                    "entries": [
                        {"content": "read a.py", "status": "in_progress"},
                        {"content": "fix it", "status": "pending"},
                    ],
                },
            ),
            _update("s", _say("Done.")),
        ],
    ),
    "steer": (
        "claude-agent-acp",
        [
            _update("s", _say("first I look")),
            _update(
                "s", {"sessionUpdate": "user_message_chunk", "content": [{"type": "text", "text": "also check b.py"}]}
            ),
            _update("s", _say("ok, b.py")),
        ],
    ),
    "revised arguments": (
        "claude-agent-acp",
        [
            _update(
                "s",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "c1",
                    "title": "Read",
                    "kind": "read",
                    "status": "pending",
                    "rawInput": {},
                },
            ),
            _update(
                "s",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c1",
                    "status": "in_progress",
                    "rawInput": {"file_path": "b.py"},
                },
            ),
            _update(
                "s",
                {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed", "rawOutput": "x = 1"},
            ),
        ],
    ),
    "permission command": (
        "codex-acp",
        [
            _update(
                "s",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "c1",
                    "title": "Read a.py",
                    "kind": "read",
                    "status": "in_progress",
                    "locations": [{"path": "/w/a.py"}],
                },
            ),
            {
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s",
                    "toolCall": {"toolCallId": "c1", "rawInput": {"command": "cat a.py"}},
                    "options": [],
                },
            },
            _update(
                "s",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c1",
                    "status": "completed",
                    "rawOutput": {"formatted_output": "x = 1", "exit_code": 0},
                },
            ),
        ],
    ),
    "codex exit 1": (
        "codex-acp",
        [
            _update(
                "s",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "c1",
                    "title": "false",
                    "kind": "execute",
                    "status": "in_progress",
                    "rawInput": {"command": ["false"]},
                },
            ),
            _update(
                "s",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c1",
                    "status": "completed",
                    "rawOutput": {"formatted_output": "", "exit_code": 1},
                },
            ),
        ],
    ),
}


def _without_times(events: list[dict]) -> list[dict]:
    return [{key: value for key, value in event.items() if key != "at"} for event in events]


@pytest.mark.parametrize("case", list(_PARITY_CASES))
def test_a_replayed_run_reads_the_way_the_live_collector_read_it(tmp_path: Path, case: str):
    from raven.acp_client.acp_agent import _TurnCollector
    from raven.acp_client.acp_dialects import dialect_for

    agent, frames = _PARITY_CASES[case]
    collector = _TurnCollector(dialect=dialect_for({"agentInfo": {"name": agent}}), prompt=_PROMPT, track_files=False)
    for body in frames:
        asyncio.run(collector(body["method"], body["params"]))
    path, handshake_end = _run_journal(tmp_path, agent, frames)

    prompt, replayed = frames_replay.events(path, session_id="s", handshake_end=handshake_end)

    assert prompt == _PROMPT
    assert _without_times(replayed) == _without_times(collector.events)


def test_a_resumed_run_replays_only_the_turn_it_was_prompted_for(tmp_path: Path):
    history = [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "old1",
            "title": "ls",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": "ls"},
        },
        {"sessionUpdate": "tool_call_update", "toolCallId": "old1", "status": "completed", "rawOutput": "a.py"},
        _say("earlier turn"),
    ]
    frames = [
        _update(
            "s",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "new1",
                "title": "Read",
                "kind": "read",
                "status": "in_progress",
                "rawInput": {"file_path": "a.py"},
            },
        ),
        _update(
            "s", {"sessionUpdate": "tool_call_update", "toolCallId": "new1", "status": "completed", "rawOutput": "x"}
        ),
        _update("s", _say("this turn done")),
    ]
    path, handshake_end = _run_journal(tmp_path, "claude-agent-acp", frames, history=history)

    prompt, replayed = frames_replay.events(path, session_id="s", start=handshake_end, handshake_end=handshake_end)

    assert prompt == _PROMPT
    assert [(e["t"], e.get("id") or e.get("text")) for e in replayed] == [
        ("call", "new1"),
        ("result", "new1"),
        ("say", "this turn done"),
    ]
