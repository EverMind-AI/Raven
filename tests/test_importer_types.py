"""Tests for raven.importer types and Scanner protocol."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from raven.importer import (
    ImportMessage,
    ImportSession,
    Platform,
    Scanner,
    ScanResult,
    SourceKind,
    Tier,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestPlatform:
    def test_values(self) -> None:
        assert set(Platform) == {
            Platform.CLAUDE_CODE,
            Platform.CODEX,
            Platform.KIMICODE,
            Platform.HERMES,
            Platform.OPENCLAW,
        }

    def test_str_serialization(self) -> None:
        assert str(Platform.CLAUDE_CODE) == "claude_code"
        assert str(Platform.KIMICODE) == "kimicode"


class TestSourceKind:
    def test_values(self) -> None:
        assert set(SourceKind) == {
            SourceKind.MEMORY_FILE,
            SourceKind.CONVERSATION,
        }


class TestTier:
    def test_values(self) -> None:
        assert set(Tier) == {Tier.MEMORY_FILES, Tier.FULL}

    def test_str_serialization(self) -> None:
        assert str(Tier.FULL) == "full"


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


class TestImportMessage:
    def test_required_fields(self) -> None:
        msg = ImportMessage(
            role="user",
            content="hello",
            timestamp=1720000000000,
            sender_id="alice",
        )
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.timestamp == 1720000000000
        assert msg.sender_id == "alice"

    def test_tool_calls_default_none(self) -> None:
        msg = ImportMessage(role="assistant", content="ok", timestamp=0, sender_id="bot")
        assert msg.tool_calls is None

    def test_tool_calls_optional(self) -> None:
        tc = ({"id": "1", "type": "function", "function": {"name": "f"}},)
        msg = ImportMessage(
            role="assistant",
            content="",
            timestamp=0,
            sender_id="bot",
            tool_calls=tc,
        )
        assert msg.tool_calls == tc

    def test_tool_call_id_default_none(self) -> None:
        msg = ImportMessage(role="user", content="x", timestamp=0, sender_id="u")
        assert msg.tool_call_id is None

    def test_tool_call_id_on_tool_message(self) -> None:
        msg = ImportMessage(role="tool", content="result", timestamp=0, sender_id="u", tool_call_id="t1")
        assert msg.tool_call_id == "t1"

    def test_frozen(self) -> None:
        msg = ImportMessage(role="user", content="x", timestamp=0, sender_id="u")
        with pytest.raises(dataclasses.FrozenInstanceError):
            msg.content = "y"  # type: ignore[misc]


class TestImportSession:
    def test_required_fields(self) -> None:
        sess = ImportSession(
            app_id="claude_code",
            project_id="proj",
            session_id="s1",
        )
        assert sess.app_id == "claude_code"
        assert sess.session_id == "s1"

    def test_messages_default_empty(self) -> None:
        sess = ImportSession(app_id="a", project_id="p", session_id="s")
        assert sess.messages == ()

    def test_messages_are_tuple(self) -> None:
        msg = ImportMessage(role="user", content="hi", timestamp=0, sender_id="u")
        sess = ImportSession(app_id="a", project_id="p", session_id="s", messages=(msg,))
        assert isinstance(sess.messages, tuple)
        assert len(sess.messages) == 1

    def test_frozen(self) -> None:
        sess = ImportSession(app_id="a", project_id="p", session_id="s")
        with pytest.raises(dataclasses.FrozenInstanceError):
            sess.app_id = "b"  # type: ignore[misc]


class TestScanResult:
    def test_all_fields(self) -> None:
        r = ScanResult(
            source_key="proj-memory",
            platform=Platform.CLAUDE_CODE,
            kind=SourceKind.MEMORY_FILE,
            file_paths=(Path("/a/b.md"), Path("/a/c.md")),
            estimated_size=4096,
            mtime=1720000000.0,
        )
        assert r.source_key == "proj-memory"
        assert r.platform == Platform.CLAUDE_CODE
        assert r.kind == SourceKind.MEMORY_FILE
        assert len(r.file_paths) == 2
        assert r.estimated_size == 4096

    def test_file_paths_is_tuple(self) -> None:
        r = ScanResult(
            source_key="k",
            platform=Platform.CODEX,
            kind=SourceKind.CONVERSATION,
            file_paths=(Path("/x.jsonl"),),
            estimated_size=0,
            mtime=0.0,
        )
        assert isinstance(r.file_paths, tuple)

    def test_frozen(self) -> None:
        r = ScanResult(
            source_key="k",
            platform=Platform.HERMES,
            kind=SourceKind.CONVERSATION,
            file_paths=(),
            estimated_size=0,
            mtime=0.0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.source_key = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Scanner Protocol
# ---------------------------------------------------------------------------


class _CompleteScanner:
    platform = Platform.CLAUDE_CODE

    async def scan(self) -> list[ScanResult]:
        return []

    async def read(self, result: ScanResult) -> ImportSession:
        return ImportSession(app_id="a", project_id="p", session_id="s")


class _IncompleteScanner:
    platform = Platform.CODEX

    async def scan(self) -> list[ScanResult]:
        return []


class TestScannerProtocol:
    def test_complete_scanner_satisfies_protocol(self) -> None:
        assert isinstance(_CompleteScanner(), Scanner)

    def test_incomplete_scanner_fails_protocol(self) -> None:
        assert not isinstance(_IncompleteScanner(), Scanner)
