"""Tests for raven.importer.orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from raven.importer import orchestrator
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


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry waits are real seconds in production; tests that care set them."""
    monkeypatch.setattr(orchestrator, "_STORE_RETRY_BACKOFF_S", (0.0, 0.0, 0.0))


class FakeBackend:
    """Records store() calls for assertion."""

    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        drop_on: set[str] | None = None,
        drop_first: dict[str, int] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.attempts = 0
        self._fail_on = fail_on or set()
        # A backend that reports a dropped write instead of raising: the shape
        # a real EverosBackend takes when the memory service is unavailable.
        self._drop_on = drop_on or set()
        # Refuse the first n writes of a session, then accept: a transient fault.
        self._drop_first = dict(drop_first or {})

    async def recall(self, query: str, *, user_id: str | None = None, agent_id: str | None = None, top_k: int) -> list:
        return []

    async def store(
        self, session_id: str, messages: list[dict[str, Any]], *, metadata: dict[str, Any] | None = None
    ) -> bool:
        self.attempts += 1
        if session_id in self._fail_on:
            raise RuntimeError(f"store failed for {session_id}")
        if session_id in self._drop_on:
            return False
        if self._drop_first.get(session_id, 0) > 0:
            self._drop_first[session_id] -= 1
            return False
        self.calls.append({"session_id": session_id, "messages": messages, "metadata": metadata})
        return True

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
    tool_calls: tuple[dict[str, Any], ...] | None = None,
    tool_call_id: str | None = None,
) -> ImportMessage:
    return ImportMessage(role=role, content=content, timestamp=ts, tool_calls=tool_calls, tool_call_id=tool_call_id)


def _session(
    n_msgs: int = 3,
    session_id: str = "sess-1",
    content: str = "hello",
) -> ImportSession:
    msgs = tuple(_msg(content=f"{content}-{i}", ts=1000 + i) for i in range(n_msgs))
    return ImportSession(session_id=session_id, messages=msgs)


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

    @pytest.mark.asyncio
    async def test_a_dropped_write_is_treated_exactly_like_a_raised_one(self, tmp_path: Path) -> None:
        """The policy does not change, only the signal that triggers it.

        A backend that reports a dropped write rather than raising must still
        leave the source unsubmitted. Without this the resume state marks the
        source done, and a rerun skips the very history that was never stored
        -- silent, permanent, and invisible in the summary.
        """
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend(drop_on={"import-a"})
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


class TestBatchProgress:
    @pytest.mark.asyncio
    async def test_on_batch_reports_the_messages_landed_so_far_per_source(self, tmp_path: Path) -> None:
        """25 messages in batches of 10: a report before the first send, then one
        after each landed batch, all naming the source."""
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"a": _session(n_msgs=25, session_id="import-a")})
        seen: list[tuple[str, str, int, int]] = []

        await run_import(
            [(scanner, _scan_result("a"))],
            backend,
            state,
            on_batch=lambda platform, key, sent, total: seen.append((platform, key, sent, total)),
        )

        assert seen == [
            ("claude_code", "a", 0, 25),
            ("claude_code", "a", 10, 25),
            ("claude_code", "a", 20, 25),
            ("claude_code", "a", 25, 25),
        ]

    @pytest.mark.asyncio
    async def test_the_reports_name_the_source_the_pass_is_on(self, tmp_path: Path) -> None:
        """A reader draws one source's share at a time, so a report has to name
        the source it belongs to -- not the first of the run."""
        state = ImportState(path=tmp_path / "state.json")
        scanner = FakeScanner(
            {"a": _session(n_msgs=12, session_id="import-a"), "b": _session(n_msgs=3, session_id="import-b")}
        )
        seen: list[tuple[str, int, int]] = []

        await run_import(
            [(scanner, _scan_result("a")), (scanner, _scan_result("b"))],
            FakeBackend(),
            state,
            on_batch=lambda _platform, key, sent, total: seen.append((key, sent, total)),
        )

        assert seen == [("a", 0, 12), ("a", 10, 12), ("a", 12, 12), ("b", 0, 3), ("b", 3, 3)]

    @pytest.mark.asyncio
    async def test_a_retried_batch_is_counted_once_and_only_after_it_lands(self, tmp_path: Path) -> None:
        """The count is what the memory service holds, not what was attempted:
        a batch refused twice adds its ten once, after the attempt that lands."""
        state = ImportState(path=tmp_path / "state.json")
        events: list[str] = []

        class _Recording(FakeBackend):
            async def store(self, session_id: str, messages: list[dict[str, Any]], *, metadata=None) -> bool:
                events.append("attempt")
                return await super().store(session_id, messages, metadata=metadata)

        scanner = FakeScanner({"a": _session(n_msgs=12, session_id="import-a")})

        await run_import(
            [(scanner, _scan_result("a"))],
            _Recording(drop_first={"import-a": 2}),
            state,
            on_batch=lambda _platform, _key, sent, _total: events.append(f"sent={sent}"),
        )

        assert events == ["sent=0", "attempt", "attempt", "attempt", "sent=10", "attempt", "sent=12"]

    @pytest.mark.asyncio
    async def test_a_stop_between_two_batches_reports_only_what_landed(self, tmp_path: Path) -> None:
        """The rest of the source is never sent, so nothing may report it: a
        later run sends the source whole and its share starts from zero again."""
        state = ImportState(path=tmp_path / "state.json")
        cancel = tmp_path / "import_cancel"
        backend = FakeBackend()
        inner = backend.store

        async def _store_then_cancel(*args: Any, **kwargs: Any) -> bool:
            landed = await inner(*args, **kwargs)
            cancel.touch()
            return landed

        backend.store = _store_then_cancel  # type: ignore[method-assign]
        scanner = FakeScanner({"a": _session(n_msgs=25, session_id="import-a")})
        seen: list[int] = []

        summary = await run_import(
            [(scanner, _scan_result("a"))],
            backend,
            state,
            on_batch=lambda _platform, _key, sent, _total: seen.append(sent),
            cancel_path=cancel,
        )

        assert summary.cancelled is True
        assert state.is_submitted("claude_code", "a") is False
        assert seen == [0, 10]

    @pytest.mark.asyncio
    async def test_a_source_with_nothing_to_send_reports_nothing(self, tmp_path: Path) -> None:
        """No messages is no batches: a (0, 0) report has a reader divide by it."""
        state = ImportState(path=tmp_path / "state.json")
        scanner = FakeScanner({"a": ImportSession(session_id="import-a", messages=())})
        seen: list[tuple[int, int]] = []

        await run_import(
            [(scanner, _scan_result("a"))],
            FakeBackend(),
            state,
            on_batch=lambda _platform, _key, sent, total: seen.append((sent, total)),
        )

        assert seen == []
        assert state.is_submitted("claude_code", "a")

    @pytest.mark.asyncio
    async def test_a_source_given_up_on_never_reports_its_last_batch_as_landed(self, tmp_path: Path) -> None:
        """The source fails with its second batch never accepted, so the count
        stops at the ten that did land."""
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend(drop_first={"import-a": 99})
        scanner = FakeScanner({"a": _session(n_msgs=12, session_id="import-a")})
        seen: list[int] = []

        summary = await run_import(
            [(scanner, _scan_result("a"))],
            backend,
            state,
            on_batch=lambda _platform, _key, sent, _total: seen.append(sent),
        )

        assert summary.failed == 1
        assert seen == [0]


class TestStoreRetry:
    """A refused batch is sent again, with a wait, before its source is given up on."""

    @staticmethod
    def _recording_pause(waits: list[float]):
        async def _pause(seconds: float, cancelled: Any) -> bool:
            waits.append(seconds)
            return True

        return _pause

    @pytest.mark.asyncio
    async def test_a_refused_batch_is_retried_after_each_wait_and_lands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        waits: list[float] = []
        monkeypatch.setattr(orchestrator, "_STORE_RETRY_BACKOFF_S", (5.0, 7.0, 9.0))
        monkeypatch.setattr(orchestrator, "_pause", self._recording_pause(waits))
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend(drop_first={"import-a": 2})
        scanner = FakeScanner({"a": _session(n_msgs=1, session_id="import-a")})

        summary = await run_import([(scanner, _scan_result("a"))], backend, state)

        assert (summary.submitted, summary.failed) == (1, 0)
        assert state.is_submitted("claude_code", "a")
        assert backend.attempts == 3
        assert len(backend.calls) == 1
        assert waits == [5.0, 7.0]

    @pytest.mark.asyncio
    async def test_a_batch_refused_every_time_fails_its_source_after_the_last_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        waits: list[float] = []
        monkeypatch.setattr(orchestrator, "_STORE_RETRY_BACKOFF_S", (1.0, 2.0, 3.0))
        monkeypatch.setattr(orchestrator, "_pause", self._recording_pause(waits))
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend(drop_on={"import-a"})
        scanner = FakeScanner({"a": _session(n_msgs=1, session_id="import-a")})

        summary = await run_import([(scanner, _scan_result("a"))], backend, state)

        assert (summary.submitted, summary.failed) == (0, 1)
        assert backend.attempts == 4
        assert waits == [1.0, 2.0, 3.0]
        assert "after 4 attempts" in summary.errors[0].error

    @pytest.mark.asyncio
    async def test_a_raised_store_error_is_retried_the_same_way(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend(fail_on={"import-a"})
        scanner = FakeScanner({"a": _session(n_msgs=1, session_id="import-a")})

        summary = await run_import([(scanner, _scan_result("a"))], backend, state)

        assert backend.attempts == 4
        assert summary.errors[0].error.startswith("store failed for import-a for import-a after 4 attempts")

    @pytest.mark.asyncio
    async def test_a_stop_during_the_wait_ends_the_run_and_leaves_the_source_unmarked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cancel = tmp_path / "cancel"

        async def _stopped_pause(seconds: float, cancelled: Any) -> bool:
            cancel.touch()
            return False

        monkeypatch.setattr(orchestrator, "_pause", _stopped_pause)
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend(drop_first={"import-a": 1})
        scanner = FakeScanner(
            {"a": _session(n_msgs=1, session_id="import-a"), "b": _session(n_msgs=1, session_id="import-b")}
        )
        items = [(scanner, _scan_result("a")), (scanner, _scan_result("b"))]

        summary = await run_import(items, backend, state, cancel_path=cancel)

        assert (summary.submitted, summary.failed) == (0, 0)
        assert not state.is_submitted("claude_code", "a")
        assert state.get_progress()["entries"] == {}
        assert [c["session_id"] for c in backend.calls] == []

    @pytest.mark.asyncio
    async def test_pause_returns_early_when_the_stop_arrives(self) -> None:
        seen = 0

        def _cancelled() -> bool:
            nonlocal seen
            seen += 1
            return seen > 1

        assert await orchestrator._pause(0.0, lambda: False) is True
        assert await orchestrator._pause(30.0, _cancelled) is False
        assert seen == 2


class TestBatching:
    @pytest.mark.asyncio
    async def test_msg_count_limit(self, tmp_path: Path) -> None:
        """120 messages -> 12 batches of 10, only the last one final, every one bulk."""
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"k1": _session(n_msgs=120, session_id="s1", content="x")})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        assert [len(c["messages"]) for c in backend.calls] == [10] * 12
        assert [c["metadata"]["is_final"] for c in backend.calls] == [False] * 11 + [True]
        assert all(c["metadata"]["bulk"] is True for c in backend.calls)

    def test_a_batch_stays_inside_the_zone_everos_extracts_linearly(self) -> None:
        """Fifty is a ceiling, not a tuning knob. EverOS extracts on every add
        and the cost is superlinear in the count: against a real service a
        15-message batch took 12s and a 52-message batch 24s, while a batch of
        100 ran past the six-minute extraction budget and failed every
        memory-file source it belonged to."""
        from raven.importer.orchestrator import _BATCH_MSG_LIMIT

        assert _BATCH_MSG_LIMIT <= 10

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
        """Exactly 10 messages -> 1 batch with is_final=True."""
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"k1": _session(n_msgs=10, session_id="s1", content="x")})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        assert len(backend.calls) == 1
        assert backend.calls[0]["metadata"]["is_final"] is True

    @pytest.mark.asyncio
    async def test_empty_session_no_store(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        empty = ImportSession(session_id="s", messages=())
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
        msg = _msg(role="assistant", content="thinking", tool_calls=tc)
        session = ImportSession(session_id="s", messages=(msg,))
        scanner = FakeScanner({"k1": session})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        stored = backend.calls[0]["messages"][0]
        assert stored["tool_calls"] == [tc[0]]

    @pytest.mark.asyncio
    async def test_tool_call_id_pass_through(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        msg = _msg(role="tool", content="result", tool_call_id="call_1")
        session = ImportSession(session_id="s", messages=(msg,))
        scanner = FakeScanner({"k1": session})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        stored = backend.calls[0]["messages"][0]
        assert stored["tool_call_id"] == "call_1"

    @pytest.mark.asyncio
    async def test_no_tool_fields_when_absent(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        msg = _msg(role="user", content="hi")
        session = ImportSession(session_id="s", messages=(msg,))
        scanner = FakeScanner({"k1": session})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        stored = backend.calls[0]["messages"][0]
        assert "tool_calls" not in stored
        assert "tool_call_id" not in stored


class TestMetadata:
    @pytest.mark.asyncio
    async def test_metadata_marks_the_write_bulk_and_names_no_owner(self, tmp_path: Path) -> None:
        """``bulk`` tells the backend nothing waits on this append, so it may
        take its extraction budget; app_id/project_id are deliberately omitted
        so EverOS defaults to 'default'/'default', matching the daily recall
        partition."""
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"k1": _session(n_msgs=1, session_id="s1")})

        await run_import([(scanner, _scan_result("k1"))], backend, state)

        assert backend.calls[0]["metadata"] == {"is_final": True, "bulk": True}


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


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_stops_before_next_item(self, tmp_path: Path) -> None:
        """When cancel file exists before loop starts, no items are processed."""
        cancel = tmp_path / "import_cancel"
        cancel.touch()
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"a": _session(n_msgs=1, session_id="sa"), "b": _session(n_msgs=1, session_id="sb")})
        items = [(scanner, _scan_result("a")), (scanner, _scan_result("b"))]

        summary = await run_import(items, backend, state, cancel_path=cancel)

        assert summary.cancelled is True
        assert summary.submitted == 0
        assert summary.total == 2
        assert backend.calls == []

    @pytest.mark.asyncio
    async def test_cancel_mid_import(self, tmp_path: Path) -> None:
        """Cancel file created after first item completes stops before second."""
        cancel = tmp_path / "import_cancel"
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"a": _session(n_msgs=1, session_id="sa"), "b": _session(n_msgs=1, session_id="sb")})
        items = [(scanner, _scan_result("a")), (scanner, _scan_result("b"))]

        original_store = backend.store

        async def _store_then_cancel(*args: Any, **kwargs: Any) -> None:
            await original_store(*args, **kwargs)
            cancel.touch()

        backend.store = _store_then_cancel

        summary = await run_import(items, backend, state, cancel_path=cancel)

        assert summary.cancelled is True
        assert summary.submitted == 1
        assert summary.total == 2

    @pytest.mark.asyncio
    async def test_cancel_between_batches_leaves_the_source_unsent_and_unmarked(self, tmp_path: Path) -> None:
        """A long conversation is many batches, and a stop that waited for the
        whole source was not a stop. The interrupted source keeps no entry, so
        a later run sends it whole rather than skipping it half-landed."""
        cancel = tmp_path / "import_cancel"
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"a": _session(n_msgs=250, session_id="sa"), "b": _session(n_msgs=1, session_id="sb")})
        items = [(scanner, _scan_result("a")), (scanner, _scan_result("b"))]

        original_store = backend.store

        async def _store_then_cancel(*args: Any, **kwargs: Any) -> bool:
            landed = await original_store(*args, **kwargs)
            cancel.touch()
            return landed

        backend.store = _store_then_cancel

        summary = await run_import(items, backend, state, cancel_path=cancel)

        assert summary.cancelled is True
        assert summary.submitted == 0
        assert len(backend.calls) == 1
        assert not state.is_submitted("claude_code", "a")
        assert "claude_code:a" not in state.get_progress()["entries"]

    @pytest.mark.asyncio
    async def test_no_cancel_path_runs_normally(self, tmp_path: Path) -> None:
        """Without cancel_path, import runs all items to completion."""
        state = ImportState(path=tmp_path / "state.json")
        backend = FakeBackend()
        scanner = FakeScanner({"a": _session(n_msgs=1, session_id="sa")})

        summary = await run_import([(scanner, _scan_result("a"))], backend, state)

        assert summary.cancelled is False
        assert summary.submitted == 1
