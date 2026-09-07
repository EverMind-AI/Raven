"""A retryable model error outlasting the provider's ladder waits, then asks again.

The provider's own ladder is seconds long, which suits a dropped connection and
not a gateway that serves error pages for a few minutes. One measured deck build
had 62 minutes and 23.8M input tokens behind it when a 40-second OpenRouter outage
came back as a single error response and the turn ended on it. The loop now waits
out a second, longer ladder -- ``RecoveryLimits.llm_error_retry_delays`` -- before
giving the turn up, and the messages it asks with are the ones it asked with before.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.loop.recovery import RecoveryLimits, limits_from_defaults
from raven.providers.base import ErrorClassification, LLMProvider, LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.utils.images import is_image_part


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _FailsThenAnswers(LLMProvider):
    """Errors `failures` times with the given verdict, then answers."""

    def __init__(self, failures: int, verdict: ErrorClassification):
        super().__init__(api_key="test")
        self.failures = failures
        self.verdict = verdict
        self.calls = 0
        self.asked_with: list[int] = []

    def get_default_model(self) -> str:
        return "stub"

    _CHAT_RETRY_DELAYS = ()  # the provider's own ladder, exhausted at once

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **_):
        self.calls += 1
        self.asked_with.append(len(messages))
        if self.calls <= self.failures:
            return LLMResponse(
                content="Error calling LLM (unparsable_response@ppt): Unable to get json response",
                finish_reason="error",
                error_classification=self.verdict,
            )
        return LLMResponse(content="real answer", finish_reason="stop")

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover - the non-stream path is under test
        raise NotImplementedError


def _agent(workspace: Path, provider: LLMProvider, delays: tuple[float, ...]) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=10, empty_recovery=RecoveryLimits(llm_error_retry_delays=delays)),
        tools=ToolWiring(restrict_to_workspace=True),
    )


async def _turn(agent: AgentLoop):
    return await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="build the deck",
        ),
        session_key="s1",
    )


@pytest.mark.asyncio
async def test_a_retryable_error_is_waited_out_and_the_turn_finishes(workspace):
    provider = _FailsThenAnswers(2, ErrorClassification("unparsable_response", retryable=True, should_fallback=True))
    agent = _agent(workspace, provider, delays=(0.0, 0.0, 0.0))

    out = await _turn(agent)

    assert out is not None and out[0] == "real answer"
    assert provider.calls == 3
    assert len(set(provider.asked_with)) == 1, "the retried call asks with the same messages, nothing appended"


@pytest.mark.asyncio
async def test_the_ladder_is_the_budget(workspace):
    provider = _FailsThenAnswers(3, ErrorClassification("server", retryable=True, should_fallback=True))
    agent = _agent(workspace, provider, delays=(0.0, 0.0))

    out = await _turn(agent)

    assert provider.calls == 3, "two waits, three calls, then the turn ends on the third error"
    assert out is not None and "Error calling LLM" in (out[0] or "")


@pytest.mark.asyncio
async def test_a_non_retryable_error_is_not_asked_again(workspace):
    provider = _FailsThenAnswers(1, ErrorClassification("invalid_request", retryable=False))
    agent = _agent(workspace, provider, delays=(0.0, 0.0, 0.0))

    out = await _turn(agent)

    assert provider.calls == 1
    assert out is not None and "Error calling LLM" in (out[0] or "")


class _StallsThenAnswers(LLMProvider):
    """Streams a word, then stalls (the idle cap's TimeoutError); answers whole next time."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args, **kwargs):  # pragma: no cover - the stream path is under test
        raise NotImplementedError

    async def chat_stream(self, *args, **kwargs):
        from raven.providers.base import ChatDelta

        self.calls += 1
        if self.calls == 1:
            yield ChatDelta(content="partial")
            raise TimeoutError
        yield ChatDelta(content="answer")


def _streaming_agent(workspace: Path, provider: LLMProvider, *, retry_after_output: bool) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(
            max_iterations=10,
            empty_recovery=RecoveryLimits(llm_error_retry_delays=(0.0,), llm_retry_after_output=retry_after_output),
        ),
        tools=ToolWiring(restrict_to_workspace=True),
    )


async def _streamed_turn(agent: AgentLoop, seen: list[str]):
    async def on_delta(text: str) -> None:
        seen.append(text)

    return await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="build the deck",
        ),
        session_key="s1",
        on_token_delta=on_delta,
    )


@pytest.mark.asyncio
async def test_a_stall_after_streamed_output_is_not_retried_by_the_outer_ladder_unless_asked(workspace):
    """The whole loop, not the helper alone: a stream that produced a word and then
    stalled used to come back as a retryable error response, and the outer ladder
    asked again regardless of `llm_retry_after_output` -- an interactive client
    received the output of two attempts. Off, the turn fails on the first attempt;
    on, the retry's answer is the answer and the caller saw both, as agreed."""
    seen: list[str] = []
    with pytest.raises(TimeoutError):
        await _streamed_turn(_streaming_agent(workspace, _StallsThenAnswers(), retry_after_output=False), seen)
    assert seen == ["partial"]

    provider = _StallsThenAnswers()
    seen = []
    out = await _streamed_turn(_streaming_agent(workspace, provider, retry_after_output=True), seen)

    assert out is not None and out[0] == "answer"
    assert provider.calls == 2
    assert seen == ["partial", "answer"]


def test_the_ladder_comes_from_agents_defaults():
    class _Defaults:
        llm_error_retry_delays = [30, 60, 120]

    assert limits_from_defaults(_Defaults()).llm_error_retry_delays == (30.0, 60.0, 120.0)
    assert limits_from_defaults(object()).llm_error_retry_delays == (15.0, 30.0, 60.0)
    assert limits_from_defaults(object()).llm_retry_after_output is False

    class _Off:
        llm_error_retry_delays: list[float] = []

    assert limits_from_defaults(_Off()).llm_error_retry_delays == (), "an explicit [] turns the ladder off"

    class _Unattended:
        llm_retry_after_output = True

    assert limits_from_defaults(_Unattended()).llm_retry_after_output is True


def test_strip_images_takes_the_newest_picture_first_then_all() -> None:
    """The picture that just arrived is the one an upstream refused; older ones were
    accepted a call ago. Any role: a render reaches the model as a user message when
    the endpoint cannot carry one in a tool result."""
    picture = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    messages = [
        {"role": "user", "content": "build it"},
        {"role": "tool", "tool_call_id": "1", "content": [{"type": "text", "text": "page 3"}, picture]},
        {"role": "assistant", "content": "next"},
        {"role": "user", "content": [picture, picture], "_attached_image": True},
    ]

    newest, removed = AgentLoop._strip_images(messages)
    assert removed == 2
    assert not any(is_image_part(p) for p in newest[3]["content"])
    assert any(is_image_part(p) for p in newest[1]["content"]), "the older picture is left alone"
    assert "image omitted" in newest[3]["content"][-1]["text"]

    every, removed_all = AgentLoop._strip_images(messages, everything=True)
    assert removed_all == 3
    assert not any(is_image_part(p) for m in every if isinstance(m["content"], list) for p in m["content"])

    assert AgentLoop._strip_images([{"role": "user", "content": "no pictures"}]) == (
        [{"role": "user", "content": "no pictures"}],
        0,
    )


@pytest.mark.asyncio
async def test_an_image_refusal_with_nothing_to_strip_is_not_waited_out(workspace):
    """No picture in the conversation means the refusal was about something else, and
    `image_too_large` is not retryable: the turn ends at once rather than after the
    error ladder."""
    provider = _FailsThenAnswers(5, ErrorClassification("image_too_large", strip_images=True))
    agent = _agent(workspace, provider, delays=(0.0, 0.0, 0.0))

    out = await _turn(agent)

    assert provider.calls == 1
    assert out is not None and "Error calling LLM" in (out[0] or "")
