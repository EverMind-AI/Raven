"""The wall-clock deadline stops the turn, and never stops it silently.

The hard stop exists so a turn is not killed mid-call by the harness with no
reply at all. That only helps if the stop itself delivers something: a turn that
ends between iterations has no assistant reply yet, so the loop hands back the
most recent assistant text — or, failing that, an explicit note.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.main import _DEADLINE_STATIC_FALLBACK, _last_assistant_text
from raven.agent.loop.time_budget import DEADLINE_ENV
from raven.agent.profile import PROFILES
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _NeverFinishesProvider(LLMProvider):
    """Keeps asking for a tool, so only the deadline can end the turn."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.calls = 0

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
        self.calls += 1
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"t{self.calls}", name="no_such_tool", arguments={})],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


def _make_agent(workspace: Path, provider: LLMProvider) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=50,
        restrict_to_workspace=True,
        profile=PROFILES["eval_answer"],
        runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
    )


@pytest.mark.asyncio
async def test_deadline_inside_the_margin_stops_before_any_call(workspace, monkeypatch) -> None:
    # eval_answer hard-stops with 60s left; 30s left is already inside it.
    monkeypatch.setenv(DEADLINE_ENV, str(time.time() + 30))
    provider = _NeverFinishesProvider()
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="do the thing",
        ),
        session_key="s1",
    )

    assert provider.calls == 0, "an iteration that cannot finish must not be started"
    assert out is not None
    assert out[0] == _DEADLINE_STATIC_FALLBACK
    assert out[0].strip(), "a deadline stop must never return an empty reply"


@pytest.mark.asyncio
async def test_attended_profile_is_never_hard_stopped(workspace, monkeypatch) -> None:
    monkeypatch.setenv(DEADLINE_ENV, str(time.time() + 1))
    provider = _NeverFinishesProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        profile=PROFILES["interactive"],
        runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
    )

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="do the thing",
        ),
        session_key="s1",
    )

    assert provider.calls > 0, "an attended session must keep running past its budget"


def test_last_assistant_text_prefers_the_most_recent_non_empty() -> None:
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "first pass"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "result"},
    ]
    assert _last_assistant_text(messages) == "first pass"


def test_last_assistant_text_is_none_without_any() -> None:
    assert _last_assistant_text([{"role": "user", "content": "q"}]) is None
    assert _last_assistant_text([{"role": "assistant", "content": "   "}]) is None
