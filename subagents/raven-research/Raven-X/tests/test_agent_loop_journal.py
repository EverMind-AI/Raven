"""Crash-durable turn journal: a diagnostic sidecar that exists while a turn
is in flight and disappears once the canonical save lands.

The canonical session is written once, at end of turn — a batch-timeout kill
used to leave zero trajectory data on disk. These tests pin the TurnJournal
contract (checkpoint watermark / rollback rewind / shrink realign / discard /
fail-quiet) and the loop wiring: mid-turn the partial file already holds the
story so far; after a successful save it is gone.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop import journal as journal_mod
from raven.agent.loop.journal import TurnJournal
from raven.agent.tools.base import Tool
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _make_msg(content: str = "hello") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel="tui",
            chat_id="chat1",
            sender_id="user",
            chat_type=ChatType.DM,
        ),
        text=content,
    )


class TestTurnJournal:
    def test_checkpoint_writes_only_new_messages_once(self, workspace):
        path = workspace / "t.partial.jsonl"
        journal = TurnJournal(path, base=1)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        journal.checkpoint(msgs)
        msgs.append({"role": "assistant", "content": "a"})
        journal.checkpoint(msgs)
        journal.checkpoint(msgs)

        lines = _read_lines(path)
        assert [r.get("role") for r in lines] == ["user", "assistant"]
        assert all(r.get("ts") for r in lines)

    def test_rewind_records_rollback_and_rejournals(self, workspace):
        path = workspace / "t.partial.jsonl"
        journal = TurnJournal(path, base=0)
        msgs = [{"role": "assistant", "content": "draft"}]
        journal.checkpoint(msgs)
        journal.rewind(0, injected=1, notes=["loopscan_spin share=0.5 windows=12"])
        msgs[:] = [{"role": "user", "content": "review feedback"}]
        journal.checkpoint(msgs)

        lines = _read_lines(path)
        assert lines[0]["content"] == "draft"
        assert lines[1]["event"] == "rollback" and lines[1]["injected"] == 1
        assert lines[1]["notes"] == ["loopscan_spin share=0.5 windows=12"]
        assert lines[2]["content"] == "review feedback"

    def test_shrunk_history_realigns_watermark(self, workspace):
        path = workspace / "t.partial.jsonl"
        journal = TurnJournal(path, base=0)
        journal.checkpoint([{"role": "user", "content": "a"}, {"role": "tool", "content": "b"}])
        journal.checkpoint([{"role": "user", "content": "a"}])
        journal.checkpoint([{"role": "user", "content": "a"}, {"role": "tool", "content": "b2"}])

        lines = _read_lines(path)
        events = [r.get("event") for r in lines]
        assert "history_shrunk" in events
        assert lines[-1]["content"] == "b2"

    def test_discard_removes_file_and_tolerates_absence(self, workspace):
        path = workspace / "t.partial.jsonl"
        journal = TurnJournal(path, base=0)
        journal.event("turn_start")
        assert path.exists()
        journal.discard()
        assert not path.exists()
        journal.discard()

    def test_write_failure_disables_quietly(self, workspace):
        path = workspace / "blocked.partial.jsonl"
        path.mkdir()
        journal = TurnJournal(path, base=0)
        journal.event("turn_start")
        journal.checkpoint([{"role": "user", "content": "u"}])
        journal.discard()


class _PeekJournalTool(Tool):
    """Reads the partial journal from inside the turn — the crash window."""

    def __init__(self, partial_path: Path):
        self.partial_path = partial_path
        self.saw_partial = False
        self.partial_records: list[dict] = []

    @property
    def name(self):
        return "peek"

    @property
    def description(self):
        return "stub"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        if self.partial_path.exists():
            self.saw_partial = True
            self.partial_records = _read_lines(self.partial_path)
        return "peeked"


class _OneToolProvider(LLMProvider):
    """One tool call, then a final answer."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="peek", arguments={})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="final answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_partial_journal_lives_during_turn_and_dies_after(workspace):
    agent = AgentLoop(
        provider=_OneToolProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=3,
        restrict_to_workspace=True,
    )
    partial = agent.sessions.partial_path("tui:chat1")
    tool = _PeekJournalTool(partial)
    agent.tools.register(tool)

    out = await agent._process_message(_make_msg())
    assert out is not None

    assert tool.saw_partial, "partial journal missing while the turn was in flight"
    events = [r.get("event") for r in tool.partial_records]
    assert "turn_start" in events
    assert "llm_call_start" in events
    roles = [r.get("role") for r in tool.partial_records]
    assert "user" in roles, "the turn's user message must be journaled before the LLM call"

    assert not partial.exists(), "partial journal must be discarded after the canonical save"
    session_file = workspace / "sessions" / "tui" / "chat1.jsonl"
    assert session_file.exists(), "canonical session must still be persisted normally"


def test_discard_keeps_file_when_env_opts_in(monkeypatch):
    """The journal holds each tool body as first appended, before any emergency
    shrink rewrote it, so it is the only on-disk copy of elided evidence."""
    monkeypatch.setenv("RAVEN_KEEP_TURN_JOURNAL", "1")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "s.partial.jsonl"
        journal = TurnJournal(path, base=0)
        journal.checkpoint([{"role": "tool", "content": "full page text"}])

        journal.discard()

        assert not path.exists()
        kept = path.with_suffix(".kept.jsonl")
        assert kept.exists()
        assert "full page text" in kept.read_text()


def test_discard_deletes_by_default():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "s.partial.jsonl"
        journal = TurnJournal(path, base=0)
        journal.checkpoint([{"role": "tool", "content": "full page text"}])

        journal.discard()

        assert not path.exists()
        assert not path.with_suffix(".kept.jsonl").exists()


def test_discard_refuses_to_keep_when_disk_is_tight(monkeypatch):
    """Other tenants have filled this shared volume before; keeping diagnostics
    must never be the thing that stops a training run."""
    monkeypatch.setenv("RAVEN_KEEP_TURN_JOURNAL", "1")
    monkeypatch.setattr(
        journal_mod.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=0, used=0, free=1024),
    )
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "s.partial.jsonl"
        journal = TurnJournal(path, base=0)
        journal.checkpoint([{"role": "tool", "content": "full page text"}])

        journal.discard()

        assert not path.exists()
        assert not path.with_suffix(".kept.jsonl").exists()
