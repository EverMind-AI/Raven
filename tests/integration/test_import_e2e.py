"""L5 -- cold-start import end-to-end: Scanner -> orchestrator -> MemoryBackend.

Drives the real :class:`ClaudeCodeScanner` against a synthetic ``~/.claude``
tree and feeds its output through the real :func:`run_import` orchestrator,
recording what would have reached a memory backend via a fake. This proves
the full pipeline wiring (scan -> read -> batch -> store -> state) without
requiring a live EverOS instance or LLM.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from raven.importer.orchestrator import ImportSummary, ProgressEvent, run_import
from raven.importer.scanners.claude_code import ClaudeCodeScanner
from raven.importer.state import ImportState
from raven.importer.types import Scanner, ScanResult, SourceKind
from raven.utils.text import parse_iso_ts_ms

_OLD_MTIME = time.time() - 600

# ---------------------------------------------------------------------------
# Fixture data builders
# ---------------------------------------------------------------------------


def _write_conversation(path: Path) -> None:
    events = [
        {
            "type": "user",
            "timestamp": "2026-07-15T10:00:00Z",
            "message": {"role": "user", "content": "Hello, help me write a function"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-07-15T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Sure, here's a function:"},
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "write_file",
                        "input": {"path": "test.py", "content": "def hello(): pass"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-07-15T10:00:10Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_abc", "content": "File written successfully"},
                    {"type": "text", "text": "Great, now add a docstring"},
                ],
            },
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    os.utime(path, (_OLD_MTIME, _OLD_MTIME))


def _write_memory_files(project_dir: Path) -> None:
    mem_dir = project_dir / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)

    (mem_dir / "MEMORY.md").write_text(
        "# Project Memory\n\n- [arch](architecture.md) - Architecture notes\n",
        encoding="utf-8",
    )
    (mem_dir / "architecture.md").write_text(
        "---\nname: architecture\ndescription: System architecture\nmetadata:\n  type: reference\n---\n\n"
        "The system uses a layered architecture.\n\nEach layer has a single responsibility.\n",
        encoding="utf-8",
    )


def _write_large_conversation(path: Path, n_events: int = 160) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for i in range(n_events):
            role = "user" if i % 2 == 0 else "assistant"
            event = {
                "type": role,
                "timestamp": f"2026-07-15T10:{i // 60:02d}:{i % 60:02d}Z",
                "message": {"role": role, "content": f"Message number {i}"},
            }
            f.write(json.dumps(event) + "\n")
    os.utime(path, (_OLD_MTIME, _OLD_MTIME))


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    """Build a fake ~/.claude/ directory with a conversation, memory files,
    and a large conversation, all under one project."""
    claude_dir = tmp_path / ".claude"
    project_dir = claude_dir / "projects" / "test-project"

    _write_conversation(project_dir / "sess-001.jsonl")
    _write_memory_files(project_dir)
    _write_large_conversation(project_dir / "sess-large.jsonl")

    return claude_dir


@pytest.fixture
def scanner(claude_home: Path) -> ClaudeCodeScanner:
    return ClaudeCodeScanner(claude_dir=claude_home)


# ---------------------------------------------------------------------------
# Recording backend
# ---------------------------------------------------------------------------


class RecordingBackend:
    """Fake MemoryBackend that records every store() call verbatim."""

    def __init__(self) -> None:
        self.store_calls: list[dict[str, Any]] = []

    async def recall(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        top_k: int,
    ) -> list[Any]:
        return []

    async def store(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.store_calls.append(
            {"session_id": session_id, "messages": list(messages), "metadata": dict(metadata or {})}
        )

    async def feedback(self, signals: dict[str, Any]) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _items_of_kind(
    scanner: Scanner,
    results: list[ScanResult],
    kind: SourceKind,
    *,
    source_key: str | None = None,
) -> list[tuple[Scanner, ScanResult]]:
    matches = [r for r in results if r.kind == kind and (source_key is None or r.source_key == source_key)]
    return [(scanner, r) for r in matches]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pipeline_conversation(scanner: ClaudeCodeScanner, tmp_path: Path) -> None:
    """Scanner -> run_import -> verify store calls for a conversation."""
    results = await scanner.scan()
    items = _items_of_kind(scanner, results, SourceKind.CONVERSATION, source_key="sess-001")
    assert len(items) == 1

    backend = RecordingBackend()
    state = ImportState(path=tmp_path / "state.json")

    summary = await run_import(items, backend, state)

    assert summary == ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=())
    assert len(backend.store_calls) == 1

    call = backend.store_calls[0]
    assert call["session_id"] == "import-claude_code-sess-001"
    assert call["metadata"]["app_id"] == "claude_code"
    assert call["metadata"]["project_id"] == "test-project"
    assert call["metadata"]["is_final"] is True

    roles = [m["role"] for m in call["messages"]]
    assert roles == ["user", "assistant", "tool", "user"]
    assert call["messages"][0]["content"] == "Hello, help me write a function"
    assert call["messages"][3]["content"] == "Great, now add a docstring"


@pytest.mark.asyncio
async def test_full_pipeline_memory_files(scanner: ClaudeCodeScanner, tmp_path: Path) -> None:
    """Scanner -> run_import -> verify store calls for memory files."""
    results = await scanner.scan()
    items = _items_of_kind(scanner, results, SourceKind.MEMORY_FILE)
    assert len(items) == 1

    backend = RecordingBackend()
    state = ImportState(path=tmp_path / "state.json")

    summary = await run_import(items, backend, state)

    assert summary.total == 1
    assert summary.submitted == 1
    assert len(backend.store_calls) == 1

    call = backend.store_calls[0]
    assert call["session_id"] == "import-claude_code-mem-test-project"
    assert call["metadata"]["app_id"] == "claude_code"
    assert call["metadata"]["project_id"] == "test-project"
    assert call["metadata"]["is_final"] is True

    contents = [m["content"] for m in call["messages"]]
    assert any("test-project" in c for c in contents)
    assert any("architecture.md" in c for c in contents)
    assert "The system uses a layered architecture." in contents
    assert "Each layer has a single responsibility." in contents


@pytest.mark.asyncio
async def test_batching_large_conversation(scanner: ClaudeCodeScanner, tmp_path: Path) -> None:
    """160 messages -> multiple store calls with is_final only on the last."""
    results = await scanner.scan()
    items = _items_of_kind(scanner, results, SourceKind.CONVERSATION, source_key="sess-large")
    assert len(items) == 1

    backend = RecordingBackend()
    state = ImportState(path=tmp_path / "state.json")

    summary = await run_import(items, backend, state)

    assert summary.submitted == 1
    assert len(backend.store_calls) == 2

    first, second = backend.store_calls
    assert len(first["messages"]) == 100
    assert first["metadata"]["is_final"] is False
    assert len(second["messages"]) == 60
    assert second["metadata"]["is_final"] is True

    total_messages = len(first["messages"]) + len(second["messages"])
    assert total_messages == 160


@pytest.mark.asyncio
async def test_idempotent_resume(scanner: ClaudeCodeScanner, tmp_path: Path) -> None:
    """Run twice -> second run skips all, submitted count = 0."""
    results = await scanner.scan()
    items = _items_of_kind(scanner, results, SourceKind.CONVERSATION, source_key="sess-001")

    backend = RecordingBackend()
    state = ImportState(path=tmp_path / "state.json")

    first_summary = await run_import(items, backend, state)
    assert first_summary.submitted == 1
    assert len(backend.store_calls) == 1

    second_summary = await run_import(items, backend, state)
    assert second_summary.submitted == 0
    assert second_summary.skipped == 1
    assert len(backend.store_calls) == 1


@pytest.mark.asyncio
async def test_progress_callback(scanner: ClaudeCodeScanner, tmp_path: Path) -> None:
    """Verify on_progress fires once per item, with correct current/total."""
    results = await scanner.scan()
    items = _items_of_kind(scanner, results, SourceKind.CONVERSATION, source_key="sess-001")
    items += _items_of_kind(scanner, results, SourceKind.MEMORY_FILE)
    assert len(items) == 2

    backend = RecordingBackend()
    state = ImportState(path=tmp_path / "state.json")
    events: list[ProgressEvent] = []

    await run_import(items, backend, state, on_progress=events.append)

    assert len(events) == 2
    assert [e.current for e in events] == [1, 2]
    assert all(e.total == 2 for e in events)
    assert all(e.status == "submitted" for e in events)
    assert all(e.error is None for e in events)


@pytest.mark.asyncio
async def test_message_shape(scanner: ClaudeCodeScanner, tmp_path: Path) -> None:
    """Verify stored messages carry role, content, sender_id, timestamp,
    and tool_calls / tool_call_id exactly where expected."""
    results = await scanner.scan()
    items = _items_of_kind(scanner, results, SourceKind.CONVERSATION, source_key="sess-001")

    backend = RecordingBackend()
    state = ImportState(path=tmp_path / "state.json")

    await run_import(items, backend, state)

    messages = backend.store_calls[0]["messages"]
    assert len(messages) == 4

    user_msg, assistant_msg, tool_msg, followup_msg = messages

    assert user_msg["role"] == "user"
    assert user_msg["content"] == "Hello, help me write a function"
    assert user_msg["sender_id"] == "user"
    assert user_msg["timestamp"] == parse_iso_ts_ms("2026-07-15T10:00:00Z")
    assert "tool_calls" not in user_msg
    assert "tool_call_id" not in user_msg

    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] == "Sure, here's a function:"
    assert assistant_msg["sender_id"] == "assistant"
    assert assistant_msg["timestamp"] == parse_iso_ts_ms("2026-07-15T10:00:05Z")
    assert assistant_msg["tool_calls"][0]["id"] == "toolu_abc"
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "write_file"

    assert tool_msg["role"] == "tool"
    assert tool_msg["content"] == "File written successfully"
    assert tool_msg["tool_call_id"] == "toolu_abc"
    assert tool_msg["timestamp"] == parse_iso_ts_ms("2026-07-15T10:00:10Z")

    assert followup_msg["role"] == "user"
    assert followup_msg["content"] == "Great, now add a docstring"
    assert followup_msg["timestamp"] == parse_iso_ts_ms("2026-07-15T10:00:10Z")
