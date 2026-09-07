"""Replay of the 2026-09-01 dispatched-run incident, end to end.

Two defects, one 28-minute hang. Both are replayed against the real
``LiteLLMProvider`` built from the deployed config's shape (OpenRouter as the
``custom`` provider, ``anthropic/claude-opus-5``, ``reasoningEffort: high``,
``maxTokens: 32768``, ``llmCallTimeout: 1800``), over the ACP path -- the
streaming call -- with ``acompletion`` faked to behave the way the gateway did:

1. A thinking-only response: reasoning fills the output, no body, no tool
   call. The recovery used to answer with PREFILL, re-feeding that reasoning as
   a trailing assistant message. Anthropic refuses such a request while
   thinking is on, and OpenRouter returned neither an error nor a byte, so the
   turn hung to the request budget. The fake gateway does the same: a request
   that ends on an assistant message opens a stream that never yields.
2. The stream's only stall detector was the whole request budget (1800s), so
   that hang cost half an hour instead of minutes.

The streaming accumulator never reports ``finish_reason == "length"`` -- it
writes ``stop`` or ``tool_calls`` -- so although the reasoning had eaten the
whole output cap, the incident's response reached the classifier as a plain
thinking-only ``stop``, past the length guard. The fake returns exactly that.

Timeouts are scaled down (seconds for the incident's half hours); the ratios
and the outcomes are what matter. Written to run unchanged against a tree that
predates the fixes, where the first replay hangs for the scaled budget and ends
the turn on an error, and the second replay's stall lasts the whole budget.
"""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.recovery import has_thinking
from raven.cli._helpers import make_provider
from raven.config.schema import Config
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

# The block ``subagents/raven-code/run.py`` renders, key for key.
DEPLOYED_CONFIG: dict[str, Any] = {
    "providers": {
        "custom": {
            "apiKey": "test-key",
            "apiBase": "https://openrouter.ai/api/v1",
            "models": ["anthropic/claude-opus-5"],
        }
    },
    "agents": {
        "defaults": {
            "model": "anthropic/claude-opus-5",
            "provider": "custom",
            "reasoningEffort": "high",
            "temperature": 1.0,
            "maxTokens": 32768,
            "llmCallTimeout": 1800,
        }
    },
}

BUDGET = 2.5  # stands in for llmCallTimeout: 1800
IDLE = 0.3  # stands in for streamIdleTimeout: 180


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _deployed_provider(*, budget: float = BUDGET, idle: float | None = None):
    provider = make_provider(Config.model_validate(DEPLOYED_CONFIG))
    scaled: dict[str, Any] = {"timeout": budget}
    if idle is not None and "stream_idle_timeout" in {f.name for f in fields(provider.generation)}:
        scaled["stream_idle_timeout"] = idle
    provider.generation = dataclasses.replace(provider.generation, **scaled)
    return provider


@dataclass
class _Delta:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[Any] | None = None


@dataclass
class _Choice:
    delta: _Delta
    finish_reason: str | None = None
    index: int = 0


class _Usage(dict):
    def model_dump(self) -> dict[str, Any]:
        return dict(self)


@dataclass
class _Chunk:
    choices: list[_Choice]
    usage: Any | None = None


def _text(content: str, *, finish_reason: str | None = None) -> _Chunk:
    return _Chunk([_Choice(_Delta(content=content), finish_reason=finish_reason)])


class _Gateway:
    """``acompletion`` as OpenRouter behaved that day."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    @property
    def last_roles(self) -> list[str]:
        return [str(r["messages"][-1]["role"]) for r in self.requests]

    async def __call__(self, **kwargs: Any):
        self.requests.append(kwargs)

        async def thinking_only():
            yield _Chunk([_Choice(_Delta(reasoning_content="Let me work through the whole design first. " * 60))])
            yield _Chunk(
                [_Choice(_Delta(reasoning_content="...and every alternative."), finish_reason="stop")],
                usage=_Usage(prompt_tokens=1200, completion_tokens=32768, total_tokens=33968),
            )

        async def swallowed():
            await asyncio.sleep(3600)
            yield _text("never")

        async def answer():
            yield _text("real answer", finish_reason="stop")

        if kwargs["messages"][-1]["role"] == "assistant":
            return swallowed()
        if len(self.requests) == 1:
            return thinking_only()
        return answer()


async def _swallow(_: str) -> None:
    return None


def _turn() -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text="add the missing test",
    )


def _agent(workspace: Path, provider) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="anthropic/claude-opus-5",
        max_iterations=10,
        restrict_to_workspace=True,
    )


@pytest.mark.asyncio
async def test_the_streaming_path_hands_the_classifier_a_thinking_only_stop(workspace, monkeypatch):
    """Why the incident bypassed the length guard: over the streaming call the
    output cap is invisible to the classifier. It sees reasoning and ``stop``."""
    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", _Gateway())
    agent = _agent(workspace, _deployed_provider())

    response = await agent._llm_call_stream(
        messages=[{"role": "user", "content": "add the missing test"}],
        tools=None,
        model="anthropic/claude-opus-5",
        on_token_delta=_swallow,
    )

    assert response.finish_reason == "stop"
    assert response.content == ""
    assert has_thinking(response)


@pytest.mark.asyncio
async def test_a_thinking_only_turn_on_the_deployed_route_recovers_instead_of_hanging(workspace, monkeypatch):
    """The incident, replayed: thinking-only first response, gateway that
    swallows a prefill. The turn must never send a request ending on an
    assistant message, and must still deliver its answer -- promptly."""
    gateway = _Gateway()
    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", gateway)
    agent = _agent(workspace, _deployed_provider())

    started = time.monotonic()
    out = await agent._process_message(_turn(), session_key="s1", on_token_delta=_swallow)
    elapsed = time.monotonic() - started

    assert "assistant" not in gateway.last_roles, f"a prefill went to the gateway: {gateway.last_roles}"
    assert out is not None and out[0] == "real answer", out
    assert elapsed < 1.5, f"turn took {elapsed:.1f}s of a {BUDGET}s budget: it waited on a swallowed request"


@pytest.mark.asyncio
async def test_a_dead_stream_on_the_deployed_route_is_reported_at_the_idle_line(monkeypatch):
    """Two chunks, then silence on an open connection. The stall must end at
    the idle line, not at the whole request budget."""

    async def two_then_silence(**_kwargs: Any):
        async def gen():
            yield _text("Reading")
            yield _text(" the tree.")
            await asyncio.sleep(3600)
            yield _text(" never")

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", two_then_silence)
    provider = _deployed_provider(budget=BUDGET, idle=IDLE)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            pass
    elapsed = time.monotonic() - started

    assert elapsed < 1.5, f"stall lasted {elapsed:.1f}s: the whole {BUDGET}s budget, not the {IDLE}s idle line"


def test_the_deployed_config_arms_the_idle_line_by_default():
    """The deployed block names no ``streamIdleTimeout``; the default must still
    reach the provider that serves ACP, next to the 1800s request budget."""
    provider = make_provider(Config.model_validate(DEPLOYED_CONFIG))

    assert provider.generation.timeout == 1800
    assert provider.generation.stream_idle_timeout == 180
