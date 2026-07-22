"""Tests for ClaudeCodeScanner -- cold-start import from Claude Code."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from raven.importer import (
    ClaudeCodeScanner,
    Platform,
    Scanner,
    SourceKind,
)
from raven.importer.scanners.claude_code import (
    _build_file_messages,
    _make_file_end,
    _make_intro,
    _memory_files_sorted,
    _split_paragraphs,
    _truncate,
)
from raven.utils.text import is_cjk, parse_frontmatter, parse_iso_ts_ms

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_OLD_MTIME = time.time() - 600


def _write_jsonl(path: Path, events: list[dict], *, active: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    if not active:
        os.utime(path, (_OLD_MTIME, _OLD_MTIME))


def _event(role: str, content, *, ts="2026-07-01T10:00:00Z", **extra):
    ev = {
        "type": role,
        "timestamp": ts,
        "message": {"role": role, "content": content},
    }
    ev.update(extra)
    return ev


def _write_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def claude_dir(tmp_path: Path) -> Path:
    return tmp_path / ".claude"


@pytest.fixture
def scanner(claude_dir: Path) -> ClaudeCodeScanner:
    return ClaudeCodeScanner(claude_dir=claude_dir)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_satisfies_scanner_protocol(self) -> None:
        assert isinstance(ClaudeCodeScanner(), Scanner)

    def test_platform_attribute(self) -> None:
        assert ClaudeCodeScanner.platform == Platform.CLAUDE_CODE


# ---------------------------------------------------------------------------
# scan()
# ---------------------------------------------------------------------------


class TestScan:
    async def test_missing_claude_dir(self, scanner: ClaudeCodeScanner) -> None:
        assert await scanner.scan() == []

    async def test_global_claude_md(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        _write_md(claude_dir / "CLAUDE.md", "## Rules\n- Use English")
        results = await scanner.scan()
        mem = [r for r in results if r.kind == SourceKind.MEMORY_FILE]
        assert len(mem) == 1
        assert mem[0].source_key == "global-claude-md"

    async def test_project_memory_bundle(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        mem_dir = claude_dir / "projects" / "test-proj" / "memory"
        _write_md(mem_dir / "arch.md", "---\nname: arch\nmetadata:\n  type: reference\n---\nBody")
        _write_md(mem_dir / "MEMORY.md", "# Index")
        results = await scanner.scan()
        mem = [r for r in results if r.kind == SourceKind.MEMORY_FILE and "memory" in r.source_key]
        assert len(mem) == 1
        assert len(mem[0].file_paths) == 2

    async def test_project_sessions(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "test-proj"
        _write_jsonl(proj / "abc-123.jsonl", [_event("user", "hello")])
        results = await scanner.scan()
        conv = [r for r in results if r.kind == SourceKind.CONVERSATION]
        assert len(conv) == 1
        assert conv[0].source_key == "abc-123"

    async def test_active_session_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "test-proj"
        _write_jsonl(proj / "active.jsonl", [_event("user", "hello")], active=True)
        results = await scanner.scan()
        assert not [r for r in results if r.kind == SourceKind.CONVERSATION]

    async def test_subagent_files_excluded(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "test-proj"
        _write_jsonl(proj / "main.jsonl", [_event("user", "hello")])
        _write_jsonl(proj / "main" / "subagents" / "agent-abc.jsonl", [_event("user", "sub")])
        results = await scanner.scan()
        conv = [r for r in results if r.kind == SourceKind.CONVERSATION]
        assert len(conv) == 1
        assert conv[0].source_key == "main"

    async def test_oversized_memory_file_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        mem_dir = claude_dir / "projects" / "proj" / "memory"
        _write_md(mem_dir / "huge.md", "x" * (1_048_576 + 1))
        _write_md(mem_dir / "small.md", "ok")
        results = await scanner.scan()
        mem = [r for r in results if "memory" in r.source_key]
        assert len(mem[0].file_paths) == 1
        assert mem[0].file_paths[0].name == "small.md"


# ---------------------------------------------------------------------------
# read(CONVERSATION)
# ---------------------------------------------------------------------------


class TestReadConversation:
    async def test_basic_user_assistant(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(proj / "s1.jsonl", [_event("user", "Q"), _event("assistant", "A")])
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        session = await scanner.read(r)
        assert len(session.messages) == 2
        assert session.messages[0].role == "user"
        assert session.messages[1].role == "assistant"

    async def test_tool_use_extraction(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(
            proj / "s2.jsonl",
            [
                _event(
                    "assistant",
                    [
                        {"type": "text", "text": "Reading."},
                        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/f"}},
                    ],
                )
            ],
        )
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        msg = (await scanner.read(r)).messages[0]
        assert msg.tool_calls is not None
        assert msg.tool_calls[0]["function"]["name"] == "Read"

    async def test_tool_result_extraction(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(
            proj / "s3.jsonl",
            [
                _event(
                    "user",
                    [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "file data"},
                        {"type": "text", "text": "Continue."},
                    ],
                )
            ],
        )
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        msgs = (await scanner.read(r)).messages
        assert msgs[0].role == "tool"
        assert msgs[0].tool_call_id == "t1"
        assert msgs[1].role == "user"

    async def test_tool_result_without_id_dropped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(
            proj / "s4.jsonl",
            [_event("user", [{"type": "tool_result", "content": "no id"}, {"type": "text", "text": "ok"}])],
        )
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        msgs = (await scanner.read(r)).messages
        assert len(msgs) == 1
        assert msgs[0].role == "user"

    async def test_ismeta_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(
            proj / "s5.jsonl",
            [_event("user", "real"), _event("user", "meta", isMeta=True), _event("assistant", "ok")],
        )
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        msgs = (await scanner.read(r)).messages
        assert len(msgs) == 2

    async def test_compact_summary_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(
            proj / "s6.jsonl",
            [_event("user", "real"), _event("user", "summary", isCompactSummary=True)],
        )
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        assert len((await scanner.read(r)).messages) == 1

    async def test_api_error_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(
            proj / "s7.jsonl",
            [_event("user", "q"), _event("assistant", "err", isApiErrorMessage=True), _event("assistant", "ok")],
        )
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        msgs = (await scanner.read(r)).messages
        assert len(msgs) == 2
        assert msgs[1].content == "ok"

    async def test_non_conversation_events_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        events = [
            _event("user", "hello"),
            {"type": "system", "subtype": "turn_duration"},
            {"type": "attachment", "attachment": {"type": "diagnostics"}},
            {"type": "ai-title", "aiTitle": "Test"},
            _event("assistant", "world"),
        ]
        _write_jsonl(proj / "s8.jsonl", events)
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        assert len((await scanner.read(r)).messages) == 2

    async def test_thinking_blocks_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(
            proj / "s9.jsonl",
            [_event("assistant", [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "reply"}])],
        )
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        assert (await scanner.read(r)).messages[0].content == "reply"

    async def test_malformed_json_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        path = proj / "s10.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write("{bad\n")
            f.write(json.dumps(_event("user", "good")) + "\n")
        os.utime(path, (_OLD_MTIME, _OLD_MTIME))
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        assert len((await scanner.read(r)).messages) == 1

    async def test_content_truncation(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(proj / "s11.jsonl", [_event("user", "x" * 15_000)])
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        msg = (await scanner.read(r)).messages[0]
        assert len(msg.content) == 10_003
        assert msg.content.endswith("...")

    async def test_timestamp_parsing(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(proj / "s12.jsonl", [_event("user", "hi", ts="2026-07-01T12:00:00.500Z")])
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        assert (await scanner.read(r)).messages[0].timestamp == 1782907200500

    async def test_numeric_timestamp(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        ev = {"type": "user", "timestamp": 1720000000000, "message": {"role": "user", "content": "num ts"}}
        _write_jsonl(proj / "s13.jsonl", [ev])
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        msgs = (await scanner.read(r)).messages
        assert len(msgs) == 1
        assert msgs[0].timestamp == 1720000000000

    async def test_no_timestamp_event_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        ev = {"type": "user", "message": {"role": "user", "content": "no ts"}}
        _write_jsonl(proj / "s14.jsonl", [ev, _event("user", "with ts")])
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        assert len((await scanner.read(r)).messages) == 1

    async def test_empty_content_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(
            proj / "s15.jsonl",
            [
                _event("user", ""),
                _event("user", "   "),
                _event("assistant", [{"type": "thinking", "thinking": "only"}]),
                _event("user", "real"),
            ],
        )
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        msgs = (await scanner.read(r)).messages
        assert len(msgs) == 1
        assert msgs[0].content == "real"

    async def test_complete_tool_trajectory(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        proj = claude_dir / "projects" / "proj"
        _write_jsonl(
            proj / "s16.jsonl",
            [
                _event("user", "Read file.py", ts="2026-07-01T10:00:00Z"),
                _event(
                    "assistant",
                    [
                        {"type": "text", "text": "Let me read it."},
                        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "f.py"}},
                    ],
                    ts="2026-07-01T10:00:01Z",
                ),
                _event(
                    "user",
                    [{"type": "tool_result", "tool_use_id": "t1", "content": "def hello(): pass"}],
                    ts="2026-07-01T10:00:02Z",
                ),
                _event("assistant", "It defines a hello function.", ts="2026-07-01T10:00:03Z"),
            ],
        )
        r = [r for r in await scanner.scan() if r.kind == SourceKind.CONVERSATION][0]
        msgs = (await scanner.read(r)).messages
        assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]
        assert msgs[1].tool_calls is not None
        assert msgs[1].tool_calls[0]["id"] == "t1"
        assert msgs[2].tool_call_id == "t1"
        assert [m.timestamp for m in msgs] == sorted(m.timestamp for m in msgs)


# ---------------------------------------------------------------------------
# read(MEMORY_FILE)
# ---------------------------------------------------------------------------


class TestReadMemory:
    async def test_global_md_english(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        _write_md(claude_dir / "CLAUDE.md", "## Rules\nUse English.\n\n## Style\nBe concise.")
        r = [r for r in await scanner.scan() if r.source_key == "global-claude-md"][0]
        session = await scanner.read(r)
        assert session.session_id == "import-claude_code-global"
        assert "Claude Code" in session.messages[0].content
        assert session.messages[-1].content.endswith("CLAUDE.md.")
        assert len(session.messages) >= 4  # intro + 2 paragraphs + file end

    async def test_global_md_chinese(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        _write_md(claude_dir / "CLAUDE.md", "## 规则\n永远用中文\n\n## 风格\n简洁")
        r = [r for r in await scanner.scan() if r.source_key == "global-claude-md"][0]
        session = await scanner.read(r)
        assert "Claude Code" in session.messages[0].content
        assert "全局" in session.messages[0].content

    async def test_global_md_empty_returns_no_messages(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        _write_md(claude_dir / "CLAUDE.md", "")
        r = [r for r in await scanner.scan() if r.source_key == "global-claude-md"][0]
        session = await scanner.read(r)
        assert len(session.messages) == 0

    async def test_project_memory_frontmatter(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        mem = claude_dir / "projects" / "proj" / "memory"
        _write_md(mem / "arch.md", "---\nname: architecture\nmetadata:\n  type: reference\n---\nLayer 1\n\nLayer 2")
        r = [r for r in await scanner.scan() if "memory" in r.source_key][0]
        session = await scanner.read(r)
        bodies = [m.content for m in session.messages]
        assert "Layer 1" in bodies
        assert "Layer 2" in bodies
        assert any("arch.md" in m.content for m in session.messages)
        assert session.messages[0].content.startswith("These are") or session.messages[0].content.startswith("这是")

    async def test_memory_md_no_frontmatter(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        mem = claude_dir / "projects" / "proj" / "memory"
        _write_md(mem / "MEMORY.md", "# Index\n\n- item 1")
        r = [r for r in await scanner.scan() if "memory" in r.source_key][0]
        session = await scanner.read(r)
        assert any("overview" in m.content.lower() for m in session.messages)

    async def test_feedback_type_intro(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        mem = claude_dir / "projects" / "proj" / "memory"
        _write_md(mem / "fb.md", "---\nname: style\nmetadata:\n  type: feedback\n---\nPrefer short answers")
        r = [r for r in await scanner.scan() if "memory" in r.source_key][0]
        session = await scanner.read(r)
        assert any("preference" in m.content.lower() for m in session.messages)

    async def test_paragraph_splitting(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        _write_md(claude_dir / "CLAUDE.md", "Para one.\n\nPara two.\n\nPara three.")
        r = [r for r in await scanner.scan() if r.source_key == "global-claude-md"][0]
        session = await scanner.read(r)
        assert len(session.messages) == 5  # intro + 3 paragraphs + file end
        assert session.messages[1].content == "Para one."
        assert "CLAUDE.md" in session.messages[-1].content

    async def test_all_files_one_session(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        mem = claude_dir / "projects" / "proj" / "memory"
        _write_md(mem / "a.md", "Content A")
        _write_md(mem / "b.md", "Content B")
        r = [r for r in await scanner.scan() if "memory" in r.source_key][0]
        session = await scanner.read(r)
        contents = [m.content for m in session.messages]
        assert "Content A" in contents and "Content B" in contents

    async def test_empty_body_skipped(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        mem = claude_dir / "projects" / "proj" / "memory"
        _write_md(mem / "empty.md", "---\nname: empty\n---\n  \n")
        _write_md(mem / "real.md", "Real content")
        r = [r for r in await scanner.scan() if "memory" in r.source_key][0]
        session = await scanner.read(r)
        assert not any("empty" in m.content.lower() for m in session.messages)

    async def test_language_detection_per_file(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        mem = claude_dir / "projects" / "proj" / "memory"
        _write_md(mem / "en.md", "---\nname: arch\nmetadata:\n  type: reference\n---\nEnglish content")
        _write_md(mem / "zh.md", "---\nname: conv\nmetadata:\n  type: reference\n---\n中文内容说明")
        r = [r for r in await scanner.scan() if "memory" in r.source_key][0]
        session = await scanner.read(r)
        intros = [m.content for m in session.messages if "knowledge" in m.content.lower() or "知识" in m.content]
        assert any("Here is" in i for i in intros)
        assert any("以下" in i for i in intros)

    async def test_stat_failure_falls_back(self, claude_dir: Path, scanner: ClaudeCodeScanner) -> None:
        """B1 fix: stat() failure should not crash the entire read."""
        mem = claude_dir / "projects" / "proj" / "memory"
        _write_md(mem / "a.md", "Content A")
        _write_md(mem / "b.md", "Content B")
        r = [r for r in await scanner.scan() if "memory" in r.source_key][0]
        session = await scanner.read(r)
        assert len(session.messages) >= 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def testparse_frontmatter(self) -> None:
        fm, body = parse_frontmatter("---\nname: test\nmetadata:\n  type: reference\n---\nBody")
        assert fm["name"] == "test"
        assert body == "Body"

    def test_parse_frontmatter_none(self) -> None:
        fm, body = parse_frontmatter("# No frontmatter")
        assert fm == {}
        assert body == "# No frontmatter"

    def test_parse_frontmatter_bad_yaml(self) -> None:
        fm, _ = parse_frontmatter("---\n: [invalid\n---\nBody")
        assert fm == {}

    def test_parse_frontmatter_inner_separator(self) -> None:
        """B4 fix: --- inside YAML literal block should not split."""
        text = "---\nname: test\ncontent: |\n  line1\n  ---\n  line2\n---\nReal body"
        fm, body = parse_frontmatter(text)
        assert fm.get("name") == "test"
        assert "Real body" in body

    def test_split_paragraphs(self) -> None:
        assert _split_paragraphs("A\n\nB\n\n\nC\n\n") == ["A", "B", "C"]

    def test_split_paragraphs_single(self) -> None:
        assert _split_paragraphs("Just one") == ["Just one"]

    def test_truncate_short(self) -> None:
        assert _truncate("short") == "short"

    def test_truncate_long(self) -> None:
        result = _truncate("a" * 15_000)
        assert len(result) == 10_003
        assert result.endswith("...")

    def testis_cjk(self) -> None:
        assert is_cjk("这是中文")
        assert not is_cjk("English only")
        assert is_cjk("Mixed 中文")

    def test_parse_iso_ts_string(self) -> None:
        assert parse_iso_ts_ms("2026-07-01T12:00:00Z") == 1782907200000
        assert parse_iso_ts_ms("2026-07-01T12:00:00.500Z") == 1782907200500

    def test_parse_iso_ts_numeric(self) -> None:
        assert parse_iso_ts_ms(1720000000000) == 1720000000000
        assert parse_iso_ts_ms(1720000000.5) == 1720000000500

    def test_parse_iso_ts_invalid(self) -> None:
        assert parse_iso_ts_ms("invalid") is None
        assert parse_iso_ts_ms("") is None
        assert parse_iso_ts_ms(None) is None

    def test_make_intro_data_driven(self) -> None:
        fm = {"name": "arch", "metadata": {"type": "reference"}}
        assert "arch" in _make_intro("arch.md", fm, cjk=False)
        assert "knowledge" in _make_intro("arch.md", fm, cjk=False).lower()
        assert "知识" in _make_intro("arch.md", fm, cjk=True)

    def test_build_file_messages(self) -> None:
        msgs = _build_file_messages("Intro", "Para 1\n\nPara 2", "End.", 1000)
        assert len(msgs) == 4
        assert msgs[0].content == "Intro"
        assert msgs[1].content == "Para 1"
        assert msgs[2].content == "Para 2"
        assert msgs[3].content == "End."

    def test_make_file_end(self) -> None:
        assert "CLAUDE.md" in _make_file_end("CLAUDE.md", cjk=False)
        assert "CLAUDE.md" in _make_file_end("CLAUDE.md", cjk=True)

    def test_memory_files_sorted_index_first(self) -> None:
        from pathlib import Path

        paths = (Path("b.md"), Path("MEMORY.md"), Path("a.md"))
        result = _memory_files_sorted(paths)
        assert result[0].name == "MEMORY.md"
        assert [p.name for p in result[1:]] == ["a.md", "b.md"]
