"""Tests for raven.importer.orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from raven.importer.orchestrator import ImportSummary, ProgressEvent, run_import
from raven.importer.state import ImportState
from raven.importer.types import (
    ImportMessage,
    ImportSession,
    Platform,
    ScanResult,
    SourceKind,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeBackend:
    """Records store() calls for assertion."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_on = fail_on or set()

    async def recall(self, query: str, *, user_id: str | None = None, agent_id: str | None = None, top_k: int) -> list:
        return []

    async def store(
        self, session_id: str, messages: list[dict[str, Any]], *, metadata: dict[str, Any] | None = None
    ) -> None:
        if session_id in self._fail_on:
            raise RuntimeError(f"store failed for {session_id}")
        self.calls.append({"session_id": session_id, "messages": messages, "metadata": metadata})

    async def feedback(self, signals: dict[str, Any]) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _msg(
    content: str = "hello",
    role: str = "user",
    ts: int = 1000,
    sender: str = "user",
    tool_calls: tuple[dict[str, Any], ...] | None = None,
    tool_call_id: str | None = None,
) -> ImportMessage:
    return ImportMessage(
        role=role, content=content, timestamp=ts, sender_id=sender, tool_calls=tool_calls, tool_call_id=tool_call_id
    )


def _session(
    n_msgs: int = 3,
    app_id: str = "test_app",
    project_id: str = "proj",
    session_id: str = "sess-1",
    content: str = "hello",
) -> ImportSession:
    msgs = tuple(_msg(content=f"{content}-{i}", ts=1000 + i) for i in range(n_msgs))
    return ImportSession(app_id=app_id, project_id=project_id, session_id=session_id, messages=msgs)


def _scan_result(key: str = "k1", platform: Platform = Platform.CLAUDE_CODE) -> ScanResult:
    return ScanResult(
        source_key=key,
        platform=platform,
        kind=SourceKind.CONVERSATION,
        file_paths=(Path("/fake"),),
        estimated_size=100,
        mtime=1000.0,
    )


class FakeScanner:
    def __init__(self, sessions: dict[str, ImportSession] | None = None, *, fail_on: set[str] | None = None) -> None:
        self.platform = Platform.CLAUDE_CODE
        self._sessions = sessions or {}
        self._fail_on = fail_on or set()

    async def scan(self) -> list[ScanResult]:
        return []

    async def read(self, result: ScanResult) -> ImportSession:
        if result.source_key in self._fail_on:
            raise OSError(f"read failed for {result.source_key}")
        return self._sessions.get(result.source_key, _session(session_id=f"import-{result.source_key}"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunImportBasic:
    @pytest.mark.asyncio
    async def test_empty_items(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        summary = await run_import([], backend, state)
        assert summary == ImportSummary(total=0, submitted=0, skipped=0, failed=0, errors=())
        assert backend.calls == []

    @pytest.mark.asyncio
    async def test_single_session(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"k1": _session(n_msgs=3, session_id="s1")})
        result = _scan_result("k1")

        summary = await run_import([(scanner, result)], backend, state)

        assert summary.total == 1
        assert summary.submitted == 1
        assert summary.skipped == 0
        assert summary.failed == 0
        assert len(backend.calls) == 1
        assert backend.calls[0]["session_id"] == "s1"
        assert len(backend.calls[0]["messages"]) == 3
        assert backend.calls[0]["metadata"]["is_final"] is True
        assert state.is_submitted("claude_code", "k1")

    @pytest.mark.asyncio
    async def test_multiple_sessions(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner(
            {
                "a": _session(n_msgs=2, session_id="sa"),
                "b": _session(n_msgs=2, session_id="sb"),
            }
        )
        items = [(scanner, _scan_result("a")), (scanner, _scan_result("b"))]

        summary = await run_import(items, backend, state)

        assert summary.total == 2
        assert summary.submitted == 2
        assert state.is_submitted("claude_code", "a")
        assert state.is_submitted("claude_code", "b")


class TestIdempotent:
    @pytest.mark.asyncio
    async def test_skip_already_submitted(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        state.mark_submitted("claude_code", "k1")
        backend = FakeBackend()
        scanner = FakeScanner()

        summary = await run_import([(scanner, _scan_result("k1"))], backend, state)

        assert summary.skipped == 1
        assert summary.submitted == 0
        assert backend.calls == []

    @pytest.mark.asyncio
    async def test_retry_previously_failed(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        state.mark_failed("claude_code", "k1", "old error")
        backend = FakeBackend()
        scanner = FakeScanner({"k1": _session(n_msgs=1, session_id="s1")})

        summary = await run_import([(scanner, _scan_result("k1"))], backend, state)

        assert summary.submitted == 1
        assert summary.skipped == 0
        assert state.is_submitted("claude_code", "k1")


class TestErrorIsolation:
    @pytest.mark.asyncio
    async def test_read_failure_continues(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner(
            {"b": _session(n_msgs=1, session_id="sb")},
            fail_on={"a"},
        )
        items = [(scanner, _scan_result("a")), (scanner, _scan_result("b"))]

        summary = await run_import(items, backend, state)

        assert summary.failed == 1
        assert summary.submitted == 1
        assert len(summary.errors) == 1
        assert summary.errors[0].source_key == "a"
        assert not state.is_submitted("claude_code", "a")
        assert state.is_submitted("claude_code", "b")

    @pytest.mark.asyncio
    async def test_store_failure_continues(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend(fail_on={"import-a"})
        scanner = FakeScanner(
            {
                "a": _session(n_msgs=1, session_id="import-a"),
                "b": _session(n_msgs=1, session_id="import-b"),
            }
        )
        items = [(scanner, _scan_result("a")), (scanner, _scan_result("b"))]

        summary = await run_import(items, backend, state)

        assert summary.failed == 1
        assert summary.submitted == 1
        assert not state.is_submitted("claude_code", "a")
        assert state.is_submitted("claude_code", "b")


class TestBatching:
    @pytest.mark.asyncio
    async def test_msg_count_limit(self, tmp_path: Path) -> None:
        """150 messages -> 2 batches (100 + 50)."""
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"k1": _session(n_msgs=150, session_id="s1", content="x")})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        assert len(backend.calls) == 2
        assert len(backend.calls[0]["messages"]) == 100
        assert backend.calls[0]["metadata"]["is_final"] is False
        assert len(backend.calls[1]["messages"]) == 50
        assert backend.calls[1]["metadata"]["is_final"] is True

    @pytest.mark.asyncio
    async def test_char_limit_fallback(self, tmp_path: Path) -> None:
        """5 messages of 8000 chars each = 40K total -> splits before exceeding 30K."""
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        big_content = "x" * 8000
        scanner = FakeScanner({"k1": _session(n_msgs=5, session_id="s1", content=big_content)})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        assert len(backend.calls) >= 2
        for call in backend.calls[:-1]:
            assert call["metadata"]["is_final"] is False
        assert backend.calls[-1]["metadata"]["is_final"] is True

    @pytest.mark.asyncio
    async def test_is_final_only_on_last_batch(self, tmp_path: Path) -> None:
        """Exactly 100 messages -> 1 batch with is_final=True."""
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"k1": _session(n_msgs=100, session_id="s1", content="x")})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        assert len(backend.calls) == 1
        assert backend.calls[0]["metadata"]["is_final"] is True

    @pytest.mark.asyncio
    async def test_empty_session_no_store(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        empty = ImportSession(app_id="a", project_id="p", session_id="s", messages=())
        scanner = FakeScanner({"k1": empty})

        summary = await run_import([(scanner, _scan_result("k1"))], backend, state)

        assert backend.calls == []
        assert summary.submitted == 1
        assert state.is_submitted("claude_code", "k1")


class TestMessageConversion:
    @pytest.mark.asyncio
    async def test_tool_calls_pass_through(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        tc = ({"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},)
        msg = _msg(role="assistant", content="thinking", tool_calls=tc, sender="assistant")
        session = ImportSession(app_id="a", project_id="p", session_id="s", messages=(msg,))
        scanner = FakeScanner({"k1": session})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        stored = backend.calls[0]["messages"][0]
        assert stored["tool_calls"] == [tc[0]]

    @pytest.mark.asyncio
    async def test_tool_call_id_pass_through(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        msg = _msg(role="tool", content="result", tool_call_id="call_1")
        session = ImportSession(app_id="a", project_id="p", session_id="s", messages=(msg,))
        scanner = FakeScanner({"k1": session})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        stored = backend.calls[0]["messages"][0]
        assert stored["tool_call_id"] == "call_1"

    @pytest.mark.asyncio
    async def test_no_tool_fields_when_absent(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        msg = _msg(role="user", content="hi")
        session = ImportSession(app_id="a", project_id="p", session_id="s", messages=(msg,))
        scanner = FakeScanner({"k1": session})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        stored = backend.calls[0]["messages"][0]
        assert "tool_calls" not in stored
        assert "tool_call_id" not in stored


class TestMetadata:
    @pytest.mark.asyncio
    async def test_metadata_contains_scope_fields(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"k1": _session(n_msgs=1, app_id="claude_code", project_id="my-proj", session_id="s1")})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        meta = backend.calls[0]["metadata"]
        assert meta["app_id"] == "claude_code"
        assert meta["project_id"] == "my-proj"
        assert meta["is_final"] is True


class TestOnProgress:
    @pytest.mark.asyncio
    async def test_callback_called_per_item(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner(
            {
                "a": _session(n_msgs=1, session_id="sa"),
                "b": _session(n_msgs=1, session_id="sb"),
            }
        )
        items = [(scanner, _scan_result("a")), (scanner, _scan_result("b"))]
        events: list[ProgressEvent] = []

        await run_import(items, backend, state, on_progress=events.append)

        assert len(events) == 2
        assert events[0] == ProgressEvent(
            platform="claude_code",
            source_key="a",
            status="submitted",
            current=1,
            total=2,
            error=None,
        )
        assert events[1] == ProgressEvent(
            platform="claude_code",
            source_key="b",
            status="submitted",
            current=2,
            total=2,
            error=None,
        )

    @pytest.mark.asyncio
    async def test_callback_reports_skipped_and_failed(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        state.mark_submitted("claude_code", "a")
        backend = FakeBackend()
        scanner = FakeScanner(
            {"c": _session(n_msgs=1, session_id="sc")},
            fail_on={"b"},
        )
        items = [
            (scanner, _scan_result("a")),
            (scanner, _scan_result("b")),
            (scanner, _scan_result("c")),
        ]
        events: list[ProgressEvent] = []

        await run_import(items, backend, state, on_progress=events.append)

        assert events[0].status == "skipped"
        assert events[1].status == "failed"
        assert events[1].error is not None
        assert events[2].status == "submitted"

    @pytest.mark.asyncio
    async def test_no_callback_does_not_error(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"a": _session(n_msgs=1, session_id="sa")})

        summary = await run_import([(scanner, _scan_result("a"))], backend, state)

        assert summary.submitted == 1
