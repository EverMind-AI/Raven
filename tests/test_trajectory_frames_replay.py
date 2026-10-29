"""Replaying an external ACP run from its wire journal."""

import json
from pathlib import Path

from raven.trajectory import frames_replay


def _journal(tmp_path: Path) -> Path:
    lines = []

    def frame(direction: str, body: dict) -> None:
        lines.append(
            json.dumps({"_type": "acp_frame", "timestamp": "2026-10-27T09:00:00", "dir": direction, "frame": body})
        )

    frame("out", {"method": "session/prompt", "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "do it"}]}})
    for chunk in ("I will ", "read it"):
        frame("in", {"method": "session/update", "params": {"sessionId": "s", "update": {
            "sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": chunk}]}}})
    frame("in", {"method": "session/update", "params": {"sessionId": "s", "update": {
        "sessionUpdate": "tool_call", "toolCallId": "c1", "title": "read_file", "kind": "read",
        "status": "in_progress", "rawInput": {"path": "a.py"}}}})
    frame("in", {"method": "session/update", "params": {"sessionId": "s", "update": {
        "sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed", "rawOutput": "contents"}}})
    frame("in", {"method": "session/update", "params": {"sessionId": "other", "update": {
        "sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": "not ours"}]}}})
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
    path.write_text(json.dumps({"_type": "acp_frame", "timestamp": "2026-10-27T09:00:00", "dir": "in", "frame": {
        "method": "session/update", "params": {"sessionId": "s", "update": {
            "sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": "answer"}]}}}}) + "\n",
        encoding="utf-8")
    rows = frames_replay.items([{"role": "assistant", "content": "answer", "reasoning_content": "hmm"}])
    assert [r["key"] for r in rows] == ["llm.thinking", "llm.output"]


def test_render_reads_a_conversation(tmp_path: Path):
    text = frames_replay.render(frames_replay.replay(_journal(tmp_path), session_id="s"))
    assert "user: do it" in text
    assert "I will read it" in text
    assert "→ read" in text
    assert "← contents" in text


def test_a_missing_journal_replays_nothing(tmp_path: Path):
    assert frames_replay.replay(tmp_path / "absent.jsonl", session_id="s") == []
