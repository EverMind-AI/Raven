"""A compaction is the one turn allowed to move the session's frozen tool array.

The array is captured on a session's first assembly and served unchanged until
a refresh point. A compaction is one: the history it budgets is rebuilt, so the
turn already pays for a new prefix and the array can be recaptured with it. An
ordinary turn must not invalidate it -- that would rebuild the prefix every
turn, which is the cost this whole mechanism exists to remove.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from raven.agent.loop.main import AgentLoop
from raven.context_engine.history.markers import HISTORY_COMPACTED_META
from raven.contracts.assembled import AssembledContext
from raven.contracts.tool import Tool
from tests._wiring import wire


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never invoked
        raise NotImplementedError


def _loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(provider=_StubProvider(), workspace=tmp_path / "ws", **wire())


def _assembled(metadata: dict) -> AssembledContext:
    return AssembledContext(messages=[{"role": "system", "content": "s"}], metadata=metadata)


def _run(loop: AgentLoop, monkeypatch, metadata: dict, calls: list[str], bound: list[tuple]) -> None:
    async def assemble(*args, **kwargs):
        return _assembled(metadata)

    monkeypatch.setattr(loop.tools, "schedule_definitions_refresh", calls.append)
    monkeypatch.setattr(loop.tools, "bind_session_revision", lambda key, log: bound.append((key, log)))
    monkeypatch.setattr(loop.harness.memory, "assemble", assemble)
    session = loop.sessions.get_or_create("test:c1")
    asyncio.run(
        loop._assemble_context_messages(
            session=session,
            session_key="test:c1",
            current_message="hi",
        )
    )


def test_a_compacted_turn_schedules_the_next_assembly(tmp_path: Path, monkeypatch) -> None:
    """The refresh waits for the next assembly so the compaction turn's array,
    notices and system-prompt derivations are all the one snapshot it was
    budgeted with -- not a new array beside old text."""
    loop = _loop(tmp_path)
    calls: list[str] = []
    bound: list[tuple] = []

    _run(loop, monkeypatch, {HISTORY_COMPACTED_META: True}, calls, bound)

    assert calls == ["test:c1"]
    assert bound and bound[0][0] == "test:c1"


def test_an_ordinary_turn_keeps_the_snapshot(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    calls: list[str] = []
    bound: list[tuple] = []

    _run(loop, monkeypatch, {HISTORY_COMPACTED_META: False}, calls, bound)

    assert calls == []
    assert bound and bound[0][0] == "test:c1"


# ---------------------------------------------------------------------------
# Acceptance: a real turn, recorded from the provider's side
# ---------------------------------------------------------------------------


class _RecordingProvider:
    """Records the messages and tools of every model call, then says nothing."""

    api_key = "test"

    def __init__(self) -> None:
        self.messages: list[list[dict]] = []
        self.tools: list[list[dict]] = []

    async def chat(self, messages, tools=None, **kwargs):
        from raven.providers.base import LLMResponse

        self.messages.append(list(messages))
        self.tools.append(list(tools or []))
        return LLMResponse(content="done", tool_calls=[])

    async def chat_with_retry(self, **kwargs):
        return await self.chat(kwargs.get("messages"), kwargs.get("tools"))

    def get_default_model(self) -> str:
        return "stub"

    def classify_error(self, exc: BaseException):
        raise exc


class _LateTool(Tool):
    def __init__(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "late_tool"

    @property
    def description(self) -> str:
        return "registered after the session's array was captured"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ran"


async def _nowhere(*args: Any, **kwargs: Any) -> None:
    return None


def _request():
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="chat1", sender_id="user", chat_type=ChatType.DM),
        text="hi",
    )


def _turns(tmp_path: Path, provider: _RecordingProvider) -> AgentLoop:
    return AgentLoop(provider=provider, workspace=tmp_path / "ws", **wire(max_iterations=1))


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content or [] if isinstance(block, dict))


def _names(tools: list[dict]) -> set[str]:
    return {d["function"]["name"] for d in tools}


def test_a_live_switch_does_not_move_the_array_and_the_next_turn_says_why(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tools": {"disabledTools": []}}), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    provider = _RecordingProvider()
    loop = _turns(tmp_path, provider)

    asyncio.run(loop.run_turn(_request(), _nowhere, lambda: [], stream=False))
    cfg.write_text(json.dumps({"tools": {"disabledTools": ["grep"]}}), encoding="utf-8")
    asyncio.run(loop.run_turn(_request(), _nowhere, lambda: [], stream=False))

    assert len(provider.tools) == 2, "one model call per turn"
    assert provider.tools[0] == provider.tools[1], "the array is the prefix; a switch must not move it"
    assert "grep" in _names(provider.tools[1])
    assert "Tool 'grep': currently unavailable" in _text(provider.messages[1][-1])


def test_a_compaction_schedules_the_next_turns_refresh(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tools": {}}), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    provider = _RecordingProvider()
    loop = _turns(tmp_path, provider)

    asyncio.run(loop.run_turn(_request(), _nowhere, lambda: [], stream=False))
    loop.tools.register(_LateTool())
    asyncio.run(loop.run_turn(_request(), _nowhere, lambda: [], stream=False))
    assert "late_tool" not in _names(provider.tools[1]), "an ordinary turn keeps the captured array"

    real = loop.harness.memory.assemble
    compacting = {"on": True}

    async def marked(*args, **kwargs):
        assembled = await real(*args, **kwargs)
        if compacting["on"]:
            assembled.metadata[HISTORY_COMPACTED_META] = True
            compacting["on"] = False
        return assembled

    monkeypatch.setattr(loop.harness.memory, "assemble", marked)
    asyncio.run(loop.run_turn(_request(), _nowhere, lambda: [], stream=False))
    assert "late_tool" not in _names(provider.tools[2]), (
        "the compaction turn stays coherent: array, notices and system prompt are the same snapshot"
    )

    asyncio.run(loop.run_turn(_request(), _nowhere, lambda: [], stream=False))
    assert "late_tool" in _names(provider.tools[3]), "the next assembly recaptures the array"


def test_deleting_a_session_releases_its_frozen_array(tmp_path: Path) -> None:
    """A deleted session's snapshot is released with its binding, so a later
    session reusing the key captures the registry as it is then -- and an
    active session's snapshot is never dropped to make room for another."""
    loop = _loop(tmp_path)

    with loop.tools.session_scope_for("test:c1"):
        assert "late_tool" not in {d["function"]["name"] for d in loop.tools.get_definitions()}
        loop.tools.register(_LateTool())
        assert "late_tool" not in {d["function"]["name"] for d in loop.tools.get_definitions()}

    loop.clear_session_binding("test:c1")

    with loop.tools.session_scope_for("test:c1"):
        assert "late_tool" in {d["function"]["name"] for d in loop.tools.get_definitions()}
