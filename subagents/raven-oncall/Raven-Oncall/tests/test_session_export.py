"""Tests for the session transcript exporter."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from raven.session.export import (
    default_export_path,
    render_transcript,
    write_transcript,
)
from raven.session.manager import Session


def _session(key: str = "tui:20260622_120000_abcdef", **kw) -> Session:
    return Session(key=key, **kw)


# ── render_transcript ──────────────────────────────────────────────────


def test_header_reflects_metadata():
    s = _session(
        created_at=datetime(2026, 6, 22, 12, 0, 0),
        updated_at=datetime(2026, 6, 22, 12, 5, 0),
        metadata={"title": "My chat"},
    )
    s.add_message("user", "hello")
    s.add_message("assistant", "hi there")
    out = render_transcript(s)
    assert s.key in out
    assert "My chat" in out
    assert "2026-06-22" in out
    assert "2" in out  # message count


def test_user_and_assistant_headings():
    s = _session()
    s.add_message("user", "question?")
    s.add_message("assistant", "answer.")
    out = render_transcript(s)
    assert "question?" in out
    assert "answer." in out
    # distinct headings for the two roles
    lower = out.lower()
    assert "user" in lower and "assistant" in lower


def test_reasoning_block_present_when_set():
    s = _session()
    s.add_message("user", "q")
    s.record(
        {
            "role": "assistant",
            "content": "final answer",
            "reasoning_content": "let me think step by step",
        }
    )
    out = render_transcript(s)
    assert "let me think step by step" in out
    assert "final answer" in out


def test_reasoning_block_absent_when_unset():
    s = _session()
    s.add_message("user", "q")
    s.add_message("assistant", "plain answer")
    out = render_transcript(s)
    assert "plain answer" in out
    # no reasoning marker leaks when none was recorded
    assert "think" not in out.lower()


def test_tool_calls_and_results_rendered():
    s = _session()
    s.add_message("user", "read it")
    s.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "x.txt"}'},
                }
            ],
        }
    )
    s.record(
        {
            "role": "tool",
            "content": "the file body",
            "tool_call_id": "c1",
            "name": "read_file",
        }
    )
    out = render_transcript(s)
    assert "read_file" in out
    assert "x.txt" in out
    assert "the file body" in out


def test_empty_session_renders_header_only():
    s = _session()
    out = render_transcript(s)
    assert s.key in out
    assert isinstance(out, str) and out.strip()


# ── default_export_path / write_transcript ─────────────────────────────


def test_default_export_path_is_workspace_relative(tmp_path: Path):
    p = default_export_path(tmp_path, "tui:20260622_120000_abcdef")
    assert p.parent == tmp_path / "exports"
    assert p.suffix == ".md"
    # ':' is folded to a filesystem-safe filename
    assert ":" not in p.name


def test_write_transcript_writes_and_returns_abs_path(tmp_path: Path):
    s = _session()
    s.add_message("user", "hello")
    dest = default_export_path(tmp_path, s.key)
    written = write_transcript(s, dest)
    assert written.is_absolute()
    assert written.exists()
    assert "hello" in written.read_text(encoding="utf-8")


def test_write_transcript_overwrites(tmp_path: Path):
    s = _session()
    s.add_message("user", "first")
    dest = default_export_path(tmp_path, s.key)
    write_transcript(s, dest)
    s.add_message("assistant", "second")
    write_transcript(s, dest)
    body = dest.read_text(encoding="utf-8")
    assert "first" in body and "second" in body
    # overwrite, not append: the user line appears once
    assert body.count("first") == 1


def test_write_transcript_creates_parent_dir(tmp_path: Path):
    s = _session()
    dest = tmp_path / "nested" / "deep" / "out.md"
    written = write_transcript(s, dest)
    assert written.exists()
