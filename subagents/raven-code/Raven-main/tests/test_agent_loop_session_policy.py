"""A session policy: per-session generation overrides the loop reads once per turn.

An ACP client switches a session's mode over ``session/set_mode``; the methods
layer records what that mode moves as the session's policy on the loop. The loop
consults it at the start of each turn and threads the values into its own LLM
calls -- the streaming and non-streaming main calls and the exhaustion wrap-up --
as explicit arguments. A session with no policy passes nothing, so the provider
resolves its configured defaults exactly as it does today.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, StreamDelta, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

ABSENT = "<absent>"


class _RecordingProvider(LLMProvider):
    """Answers at once and records the effort each call arrived with.

    ``chat_stream`` records the raw keyword (or its absence) before handing off to
    the base fallback, which resolves omitted parameters from ``generation`` and
    calls ``chat``; so ``stream_calls`` shows what the loop passed and ``calls``
    what the wire would carry.
    """

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.generation = GenerationSettings(reasoning_effort="high", temperature=1.0)
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.on_chat = None

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
        self.calls.append({"tools": tools, "temperature": temperature, "reasoning_effort": reasoning_effort})
        if self.on_chat is not None:
            return await self.on_chat(len(self.calls))
        return LLMResponse(content="done", finish_reason="stop")

    async def chat_stream(self, messages, tools=None, model=None, **kwargs):
        self.stream_calls.append({"reasoning_effort": kwargs.get("reasoning_effort", ABSENT)})
        async for delta in super().chat_stream(messages, tools, model, **kwargs):
            yield delta

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _agent(workspace: Path, provider: LLMProvider, *, max_iterations: int = 3) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=max_iterations,
        restrict_to_workspace=True,
        runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
    )


def _request(text: str = "hi") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=text,
    )


async def _noop(_text: str) -> None:
    return None


class TestWithoutAPolicy:
    async def test_the_loop_passes_no_effort_and_the_provider_uses_its_own(self, workspace) -> None:
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)

        await agent._process_message(_request(), session_key="s1")

        assert provider.calls[0]["reasoning_effort"] == "high"

    async def test_the_streaming_path_passes_no_effort_either(self, workspace) -> None:
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)

        await agent._process_message(_request(), session_key="s1", on_token_delta=_noop)

        assert provider.stream_calls[0]["reasoning_effort"] == ABSENT
        assert provider.calls[0]["reasoning_effort"] == "high"

    def test_an_unknown_session_reads_as_the_empty_policy(self, workspace) -> None:
        agent = _agent(workspace, _RecordingProvider())
        assert agent.session_policy("never-seen").reasoning_effort is None


class TestWithAPolicy:
    async def test_the_effort_reaches_the_non_streaming_call(self, workspace) -> None:
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)
        agent.set_session_policy("s1", reasoning_effort="low")

        await agent._process_message(_request(), session_key="s1")

        assert provider.calls[0]["reasoning_effort"] == "low"

    async def test_the_effort_reaches_the_streaming_call(self, workspace) -> None:
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)
        agent.set_session_policy("s1", reasoning_effort="low")

        await agent._process_message(_request(), session_key="s1", on_token_delta=_noop)

        assert provider.stream_calls[0]["reasoning_effort"] == "low"
        assert provider.calls[0]["reasoning_effort"] == "low"

    async def test_the_policy_is_scoped_to_its_session(self, workspace) -> None:
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)
        agent.set_session_policy("s1", reasoning_effort="low")

        await agent._process_message(_request(), session_key="s2")

        assert provider.calls[0]["reasoning_effort"] == "high"

    async def test_clearing_the_policy_restores_the_configured_effort(self, workspace) -> None:
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)
        agent.set_session_policy("s1", reasoning_effort="low")
        agent.clear_session_policy("s1")

        await agent._process_message(_request(), session_key="s1")

        assert provider.calls[0]["reasoning_effort"] == "high"

    async def test_a_policy_with_no_effort_changes_nothing(self, workspace) -> None:
        """The default mode's policy: declared, but moving no knob."""
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)
        agent.set_session_policy("s1", reasoning_effort=None)

        await agent._process_message(_request(), session_key="s1")

        assert provider.calls[0]["reasoning_effort"] == "high"

    async def test_the_exhaustion_wrap_up_runs_under_the_policy(self, workspace) -> None:
        """The synthesis call after the iteration budget is the turn's own call
        and pays the same effort; a ``low`` session must not wrap up at ``high``."""
        provider = _RecordingProvider()

        async def keep_asking_for_tools(n: int) -> LLMResponse:
            if provider.calls[-1]["tools"] is None:
                return LLMResponse(content="partial summary", finish_reason="stop")
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"t{n}", name="no_such_tool", arguments={})],
                finish_reason="tool_calls",
            )

        provider.on_chat = keep_asking_for_tools
        agent = _agent(workspace, provider, max_iterations=2)
        agent.set_session_policy("s1", reasoning_effort="low")

        out = await agent._process_message(_request(), session_key="s1")

        assert out is not None and out[0] == "partial summary"
        synth = [c for c in provider.calls if c["tools"] is None]
        assert len(synth) == 1
        assert {c["reasoning_effort"] for c in provider.calls} == {"low"}

    async def test_a_switch_mid_turn_lands_on_the_next_turn(self, workspace) -> None:
        """Read once, at the turn's start: a switch while a turn runs leaves that
        turn on the profile it started with."""
        provider = _RecordingProvider()
        agent = _agent(workspace, provider, max_iterations=3)

        async def switch_then_finish(n: int) -> LLMResponse:
            if n == 1:
                agent.set_session_policy("s1", reasoning_effort="max")
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="t1", name="no_such_tool", arguments={})],
                    finish_reason="tool_calls",
                )
            return LLMResponse(content="done", finish_reason="stop")

        provider.on_chat = switch_then_finish
        await agent._process_message(_request(), session_key="s1")
        first_turn = [c["reasoning_effort"] for c in provider.calls]
        assert first_turn == ["high", "high"]

        provider.on_chat = None
        await agent._process_message(_request("again"), session_key="s1")
        assert provider.calls[-1]["reasoning_effort"] == "max"


class TestASubmittedTurnKeepsItsPolicy:
    async def test_a_switch_after_submission_lands_on_the_next_turn(self, workspace) -> None:
        """The prompt route pins the policy at submission; a mode switched in
        the submission-to-start window must not re-effort the submitted turn."""
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)
        agent.set_session_policy("s1", reasoning_effort="low")
        agent.pin_session_policy("s1")
        agent.set_session_policy("s1", reasoning_effort="max")
        await agent._process_message(_request(), session_key="s1")
        assert provider.calls[-1]["reasoning_effort"] == "low"

        await agent._process_message(_request("again"), session_key="s1")
        assert provider.calls[-1]["reasoning_effort"] == "max"

    async def test_a_pin_feeds_exactly_one_turn(self, workspace) -> None:
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)
        agent.set_session_policy("s1", reasoning_effort="low")
        agent.pin_session_policy("s1")
        agent.set_session_policy("s1", reasoning_effort="max")
        await agent._process_message(_request(), session_key="s1")
        await agent._process_message(_request("again"), session_key="s1")
        efforts = [c["reasoning_effort"] for c in provider.calls]
        assert efforts == ["low", "max"]

    async def test_an_unpinned_refusal_leaves_the_live_policy_in_charge(self, workspace) -> None:
        """A refused turn.send unpins, so the stale snapshot cannot outlive it."""
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)
        agent.set_session_policy("s1", reasoning_effort="low")
        agent.pin_session_policy("s1")
        agent.unpin_session_policy("s1")
        agent.set_session_policy("s1", reasoning_effort="max")
        await agent._process_message(_request(), session_key="s1")
        assert provider.calls[-1]["reasoning_effort"] == "max"

    async def test_clearing_the_policy_also_drops_the_pin(self, workspace) -> None:
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)
        agent.set_session_policy("s1", reasoning_effort="low")
        agent.pin_session_policy("s1")
        agent.clear_session_policy("s1")
        await agent._process_message(_request(), session_key="s1")
        assert provider.calls[-1]["reasoning_effort"] == "high"

    async def test_pins_are_scoped_to_their_session(self, workspace) -> None:
        provider = _RecordingProvider()
        agent = _agent(workspace, provider)
        agent.set_session_policy("s1", reasoning_effort="low")
        agent.pin_session_policy("s1")
        agent.set_session_policy("s2", reasoning_effort="max")
        await agent._process_message(_request(), session_key="s2")
        assert provider.calls[-1]["reasoning_effort"] == "max"
        await agent._process_message(_request("again"), session_key="s1")
        assert provider.calls[-1]["reasoning_effort"] == "low"


async def test_the_stream_helper_forwards_an_explicit_effort() -> None:
    """``_llm_call_stream`` hands the effort to ``chat_stream`` as a keyword, and
    passes nothing when it has nothing -- the provider's sentinel default must
    stay reachable."""
    from types import SimpleNamespace

    class _Stream:
        def __init__(self) -> None:
            self.kwargs: list[dict[str, Any]] = []

        async def chat_stream(self, **kwargs: Any):
            self.kwargs.append(kwargs)
            yield StreamDelta(content="ok")

    stream = _Stream()
    call = AgentLoop._llm_call_stream.__get__(SimpleNamespace(provider=stream))

    await call(messages=[], tools=None, model="m", on_token_delta=_noop)
    await call(messages=[], tools=None, model="m", on_token_delta=_noop, reasoning_effort="low")

    assert "reasoning_effort" not in stream.kwargs[0]
    assert stream.kwargs[1]["reasoning_effort"] == "low"
