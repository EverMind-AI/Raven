# Cold-Start Import Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the orchestration layer that reads Scanner output, batches messages respecting EverOS buffer limits, and feeds them to MemoryBackend with idempotent state tracking.

**Architecture:** A single async function `run_import` in `raven/importer/orchestrator.py` with a private `_feed_session` helper. No class wrapper. The caller (CLI layer) owns scanning, filtering, user interaction, and MemoryBackend lifecycle. The orchestrator only does: read -> batch -> store -> track state.

**Tech Stack:** Python 3.12+, asyncio, loguru, pytest, dataclasses (stdlib)

## Global Constraints

- Package manager: `uv` only (no pip, no hand-editing lockfiles)
- Run tests: `uv run pytest ...` (never bare pytest)
- Test file naming: `tests/test_importer_orchestrator.py`
- Comments: English only, only when explaining non-obvious "why"
- Imports: no EverOS imports in orchestrator -- interact via MemoryBackend Protocol only
- Logging: loguru with `{}` format (not `%s`)
- Type annotations on all public functions

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `raven/importer/orchestrator.py` | Create | `run_import`, `_feed_session`, `_to_store_dict`, `ImportSummary`, `ImportFailure` |
| `tests/test_importer_orchestrator.py` | Create | All orchestrator tests |
| `raven/importer/__init__.py` | Modify | Add re-exports for `run_import`, `ImportSummary`, `ImportFailure` |

---

### Task 1: Orchestrator implementation + tests

**Files:**
- Create: `raven/importer/orchestrator.py`
- Create: `tests/test_importer_orchestrator.py`
- Modify: `raven/importer/__init__.py`

**Interfaces:**
- Consumes:
  - `Scanner.read(result: ScanResult) -> ImportSession` (from `raven.importer.types`)
  - `ImportState.is_submitted(platform: str, source_key: str) -> bool`
  - `ImportState.mark_submitted(platform: str, source_key: str) -> None`
  - `ImportState.mark_failed(platform: str, source_key: str, error: str) -> None`
  - `MemoryBackend.store(session_id: str, messages: list[dict[str, Any]], *, metadata: dict[str, Any] | None = None) -> None`
  - `ImportMessage` fields: `role`, `content`, `timestamp`, `sender_id`, `tool_calls` (tuple|None), `tool_call_id` (str|None)
  - `ImportSession` fields: `app_id`, `project_id`, `session_id`, `messages` (tuple[ImportMessage, ...])
  - `ScanResult` fields: `source_key`, `platform` (Platform enum with `.value` str)
- Produces:
  - `run_import(items: Sequence[tuple[Scanner, ScanResult]], backend: MemoryBackend, state: ImportState) -> ImportSummary`
  - `ImportSummary(total: int, submitted: int, skipped: int, failed: int, errors: tuple[ImportFailure, ...])`
  - `ImportFailure(platform: str, source_key: str, error: str)`

- [ ] **Step 1: Write the test file with all tests**

Create `tests/test_importer_orchestrator.py` with mock Scanner + mock MemoryBackend and all test cases:

```python
"""Tests for raven.importer.orchestrator."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from raven.importer.orchestrator import ImportFailure, ImportSummary, run_import
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

    async def store(self, session_id: str, messages: list[dict[str, Any]], *, metadata: dict[str, Any] | None = None) -> None:
        if session_id in self._fail_on:
            raise RuntimeError(f"store failed for {session_id}")
        self.calls.append({"session_id": session_id, "messages": messages, "metadata": metadata})

    async def feedback(self, signals: dict[str, Any]) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _msg(content: str = "hello", role: str = "user", ts: int = 1000, sender: str = "user", tool_calls: tuple[dict[str, Any], ...] | None = None, tool_call_id: str | None = None) -> ImportMessage:
    return ImportMessage(role=role, content=content, timestamp=ts, sender_id=sender, tool_calls=tool_calls, tool_call_id=tool_call_id)


def _session(n_msgs: int = 3, app_id: str = "test_app", project_id: str = "proj", session_id: str = "sess-1", content: str = "hello") -> ImportSession:
    msgs = tuple(_msg(content=f"{content}-{i}", ts=1000 + i) for i in range(n_msgs))
    return ImportSession(app_id=app_id, project_id=project_id, session_id=session_id, messages=msgs)


def _scan_result(key: str = "k1", platform: Platform = Platform.CLAUDE_CODE) -> ScanResult:
    return ScanResult(source_key=key, platform=platform, kind=SourceKind.CONVERSATION, file_paths=(Path("/fake"),), estimated_size=100, mtime=1000.0)


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
        scanner = FakeScanner({
            "a": _session(n_msgs=2, session_id="sa"),
            "b": _session(n_msgs=2, session_id="sb"),
        })
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
        scanner = FakeScanner({
            "a": _session(n_msgs=1, session_id="import-a"),
            "b": _session(n_msgs=1, session_id="import-b"),
        })
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_importer_orchestrator.py -x -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'raven.importer.orchestrator'`

- [ ] **Step 3: Write the orchestrator implementation**

Create `raven/importer/orchestrator.py`:

```python
"""Cold-start import orchestrator -- read, batch, store, track."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from raven.importer.state import ImportState
from raven.importer.types import ImportMessage, ImportSession, Scanner, ScanResult
from raven.memory_engine.backend import MemoryBackend

_BATCH_MSG_LIMIT = 100
_BATCH_CHAR_LIMIT = 30_000


@dataclass(frozen=True)
class ImportFailure:
    """One failed import unit."""

    platform: str
    source_key: str
    error: str


@dataclass(frozen=True)
class ImportSummary:
    """Aggregate result of a run_import call."""

    total: int
    submitted: int
    skipped: int
    failed: int
    errors: tuple[ImportFailure, ...]


async def run_import(
    items: Sequence[tuple[Scanner, ScanResult]],
    backend: MemoryBackend,
    state: ImportState,
) -> ImportSummary:
    """Import pre-filtered scan results into the memory backend.

    The caller (CLI layer) is responsible for scanning, tier/platform
    filtering, and MemoryBackend lifecycle (start/stop).
    """
    total = len(items)
    logger.info("import started: {} items", total)

    submitted = 0
    skipped = 0
    failed = 0
    errors: list[ImportFailure] = []

    for i, (scanner, result) in enumerate(items):
        platform = result.platform.value
        key = result.source_key

        if state.is_submitted(platform, key):
            skipped += 1
            logger.info(
                "[{}/{}] skipping {}/{} (already submitted)",
                i + 1, total, platform, key,
            )
            continue

        logger.info("[{}/{}] importing {}/{}", i + 1, total, platform, key)
        try:
            session = await scanner.read(result)
            await _feed_session(backend, session)
            state.mark_submitted(platform, key)
            submitted += 1
            logger.info(
                "[{}/{}] imported {}/{} ({} messages)",
                i + 1, total, platform, key, len(session.messages),
            )
        except Exception as e:
            state.mark_failed(platform, key, str(e))
            failed += 1
            errors.append(ImportFailure(platform, key, str(e)))
            logger.warning(
                "[{}/{}] failed to import {}/{}: {}",
                i + 1, total, platform, key, e,
            )

    logger.info(
        "import finished: {} submitted, {} skipped, {} failed (of {} total)",
        submitted, skipped, failed, total,
    )
    return ImportSummary(
        total=total,
        submitted=submitted,
        skipped=skipped,
        failed=failed,
        errors=tuple(errors),
    )


async def _feed_session(backend: MemoryBackend, session: ImportSession) -> None:
    if not session.messages:
        return
    all_dicts = [_to_store_dict(m) for m in session.messages]
    metadata_base: dict[str, Any] = {
        "app_id": session.app_id,
        "project_id": session.project_id,
    }
    batch: list[dict[str, Any]] = []
    batch_chars = 0

    for msg_dict in all_dicts:
        msg_chars = len(msg_dict["content"])
        if batch and (
            len(batch) >= _BATCH_MSG_LIMIT
            or batch_chars + msg_chars > _BATCH_CHAR_LIMIT
        ):
            await backend.store(
                session.session_id,
                batch,
                metadata={**metadata_base, "is_final": False},
            )
            batch = []
            batch_chars = 0
        batch.append(msg_dict)
        batch_chars += msg_chars

    if batch:
        await backend.store(
            session.session_id,
            batch,
            metadata={**metadata_base, "is_final": True},
        )


def _to_store_dict(msg: ImportMessage) -> dict[str, Any]:
    d: dict[str, Any] = {
        "role": msg.role,
        "content": msg.content,
        "sender_id": msg.sender_id,
        "timestamp": msg.timestamp,
    }
    if msg.tool_calls:
        d["tool_calls"] = list(msg.tool_calls)
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    return d


__all__ = ["ImportFailure", "ImportSummary", "run_import"]
```

- [ ] **Step 4: Update `raven/importer/__init__.py` re-exports**

Add imports and update `__all__`:

```python
"""Cold-start import: discover and ingest history from other AI tools."""

from __future__ import annotations

from raven.importer.orchestrator import ImportFailure, ImportSummary, run_import
from raven.importer.scanners import ClaudeCodeScanner
from raven.importer.state import ImportState
from raven.importer.types import (
    ImportMessage,
    ImportSession,
    Platform,
    Scanner,
    ScanResult,
    SourceKind,
    Tier,
)

__all__ = [
    "ClaudeCodeScanner",
    "ImportFailure",
    "ImportMessage",
    "ImportSession",
    "ImportState",
    "ImportSummary",
    "Platform",
    "ScanResult",
    "Scanner",
    "SourceKind",
    "Tier",
    "run_import",
]
```

- [ ] **Step 5: Run all orchestrator tests**

Run: `uv run pytest tests/test_importer_orchestrator.py -x -v`
Expected: All 14 tests PASS

- [ ] **Step 6: Run existing importer tests to check for regressions**

Run: `uv run pytest tests/test_importer_types.py tests/test_importer_state.py tests/test_importer_claude_code_scanner.py -x -v`
Expected: All existing tests PASS

- [ ] **Step 7: Run full test suite**

Run: `uv run pytest --ignore=tests/integration -x`
Expected: All tests PASS (except known channel collection errors from missing optional extras)

- [ ] **Step 8: Verify module imports cleanly**

Run: `uv run python -c "from raven.importer import run_import, ImportSummary, ImportFailure; print('OK')"`
Expected: `OK`

- [ ] **Step 9: Commit**

```bash
git add raven/importer/orchestrator.py tests/test_importer_orchestrator.py raven/importer/__init__.py
git commit -m "feat(importer): add cold-start import orchestrator

Co-authored-by: Claude (claude-opus-4-6) <noreply@anthropic.com>"
```
