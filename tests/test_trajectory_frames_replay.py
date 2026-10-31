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


def test_a_byte_range_starts_where_it_says(tmp_path: Path):
    path = _journal(tmp_path)
    raw = path.read_bytes()
    head = raw.index(b"session/prompt")
    previous = raw.rfind(b"\n", 0, head)
    cut = previous + 1 if previous >= 0 else 0
    messages = frames_replay.replay(path, session_id="s", start=cut, end=len(raw))
    assert messages and messages[0]["content"] == "do it"


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
