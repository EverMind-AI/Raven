"""Tests for AgentLoop's response_modifier hook.

The hook is a generic content transform applied to the final assistant
content just before it's sent as the reply. Sentinel's NudgeInjector
is the intended primary user, but the hook itself is agnostic.

Contract this file pins:
- modifier=None → content passes through unchanged (regression safety)
- modifier set → modifier is called with (session_key, content) and its return
  value replaces the content
- a SENTINEL-origin turn skips the modifier (anti-cascade)
- modifier exception does not crash the loop (logged, original content preserved)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class StubProvider(LLMProvider):
    """Always returns a fixed assistant message. No tool calls."""

    def __init__(self, content: str = "stub response"):
        super().__init__(api_key="test")
        self._content = content

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content=self._content, finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _make_agent(
    workspace: Path,
    modifier: Callable[[str, str], str] | None = None,
    provider: LLMProvider | None = None,
) -> AgentLoop:
    return AgentLoop(
        provider=provider or StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        response_modifier=modifier,
        restrict_to_workspace=True,
    )


def _make_msg(content: str = "hello", metadata: dict[str, Any] | None = None) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel="test",
            chat_id="chat1",
            sender_id="user",
            chat_type=ChatType.DM,
            extras=metadata or {},
        ),
        text=content,
    )


@pytest.mark.asyncio
async def test_no_modifier_content_passes_through(workspace):
    agent = _make_agent(workspace, modifier=None)
    out = await agent._process_message(_make_msg())
    assert out is not None
    assert out[0] == "stub response"


@pytest.mark.asyncio
async def test_modifier_transforms_content(workspace):
    calls: list[tuple[str, str]] = []

    def mod(session_key: str, content: str) -> str:
        calls.append((session_key, content))
        return content + " [MODIFIED]"

    agent = _make_agent(workspace, modifier=mod)
    out = await agent._process_message(_make_msg())
    assert out is not None
    assert out[0] == "stub response [MODIFIED]"
    assert len(calls) == 1
    assert calls[0][0] == "test:chat1"
    assert calls[0][1] == "stub response"


@pytest.mark.asyncio
async def test_modifier_skipped_for_sentinel_origin(workspace):
    # The after_send gate reads origin. A SENTINEL-origin turn (e.g. the
    # supersede notice) skips the modifier.
    from raven.spine import Origin

    calls: list[tuple[str, str]] = []

    def mod(session_key: str, content: str) -> str:
        calls.append((session_key, content))
        return content + " [MODIFIED]"

    agent = _make_agent(workspace, modifier=mod)
    out = await agent._process_message(_make_msg(), origin=Origin.SENTINEL)
    assert out is not None and out[0] == "stub response"
    assert calls == []


@pytest.mark.asyncio
async def test_modifier_runs_for_user_origin(workspace):
    # A menu pick is origin=USER (its reply IS the user's intent) — the modifier
    # runs.
    from raven.spine import Origin

    calls: list[tuple[str, str]] = []

    def mod(session_key: str, content: str) -> str:
        calls.append((session_key, content))
        return content + " [MODIFIED]"

    agent = _make_agent(workspace, modifier=mod)
    out = await agent._process_message(_make_msg(), origin=Origin.USER)
    assert out is not None and out[0] == "stub response [MODIFIED]"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_modifier_skipped_for_subagent_origin(workspace):
    # parity: a subagent result re-injection returned from the
    # system-message branch before reaching after_send today, so it never ran
    # the modifier. On the spine path (origin=SUBAGENT, non-system) it must still
    # skip — else the summary gets a nudge layered on (regression).
    from raven.spine import Origin

    calls: list[tuple[str, str]] = []

    def mod(session_key: str, content: str) -> str:
        calls.append((session_key, content))
        return content + " [MODIFIED]"

    agent = _make_agent(workspace, modifier=mod)
    out = await agent._process_message(_make_msg(), origin=Origin.SUBAGENT)
    assert out is not None and out[0] == "stub response"
    assert calls == []


@pytest.mark.asyncio
async def test_personalization_skipped_for_subagent_only(workspace, monkeypatch):
    # parity (asymmetry lock): a subagent result re-injection must NOT be
    # personalized (the system-message branch never ran personalization, and the
    # announce is system-generated, not user input). The skip is SUBAGENT-only —
    # a SENTINEL turn reaches and runs personalization today, so it must keep it.
    import raven.agent.personalizer as personalizer_mod
    from raven.spine import Origin

    classified: list[str] = []

    class _SpyPersonalizer:
        def __init__(self, *a, **k):
            pass

        async def classify(self, content, history=None):
            classified.append(content)
            return {"needs_clarification": False}

        async def post_learn(self, *a, **k):
            pass

    monkeypatch.setattr(personalizer_mod, "Personalizer", _SpyPersonalizer)

    agent = _make_agent(workspace)
    agent.configure_personalization(True)

    await agent._process_message(_make_msg("subagent announce"), origin=Origin.SUBAGENT)
    assert classified == []  # SUBAGENT skips personalization

    await agent._process_message(_make_msg("sentinel notice"), origin=Origin.SENTINEL)
    assert classified == ["sentinel notice"]  # SENTINEL still personalized


@pytest.mark.asyncio
async def test_modifier_exception_preserves_content(workspace):
    def bad_mod(session_key: str, content: str) -> str:
        raise RuntimeError("boom")

    agent = _make_agent(workspace, modifier=bad_mod)
    out = await agent._process_message(_make_msg())
    # Exception logged but original content survives.
    assert out is not None
    assert out[0] == "stub response"


# ---------------------------------------------------------------------------
# on_user_inbound hook — Sentinel engagement tracking entry point


@pytest.mark.asyncio
async def test_on_user_inbound_called_for_user_message(workspace):
    received = []

    agent = AgentLoop(
        provider=StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        on_user_inbound=lambda msg: received.append(msg),
        restrict_to_workspace=True,
    )
    await agent._process_message(_make_msg("hello"))
    assert len(received) == 1
    assert received[0].text == "hello"


@pytest.mark.asyncio
async def test_on_user_inbound_skipped_for_sentinel_origin(workspace):
    from raven.spine import Origin

    received = []

    agent = AgentLoop(
        provider=StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        on_user_inbound=lambda msg: received.append(msg),
        restrict_to_workspace=True,
    )
    await agent._process_message(_make_msg("sentinel-originated"), origin=Origin.SENTINEL)
    assert received == []


@pytest.mark.asyncio
async def test_on_user_inbound_exception_does_not_crash(workspace):
    def bad_cb(msg):
        raise RuntimeError("boom")

    agent = AgentLoop(
        provider=StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        on_user_inbound=bad_cb,
        restrict_to_workspace=True,
    )
    out = await agent._process_message(_make_msg())
    assert out is not None
    assert out[0] == "stub response"
