"""Max-iteration exhaustion synthesizes a final answer instead of a canned string.

When the tool-call budget runs out, the loop makes one tools-disabled LLM call
asking the model to wrap up and deliver its best partial result. Covered:

- loop level: hitting max_iterations delivers the synthesized text, not the
  static apology, and the synthesis call is made with tools withheld
- unit level: _synthesize_final_on_exhaustion withholds tools, threads the
  fallback chain, and falls back to the static message on error/empty
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.main import _MAX_ITER_STATIC_FALLBACK, _MAX_ITER_SYNTHESIS_PROMPT
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# --------------------------------------------------------------------------- #
# Loop-level: max_iterations reached -> synthesized answer delivered           #
# --------------------------------------------------------------------------- #


class _ToolThenSynthProvider(LLMProvider):
    """Requests a tool while tools are offered; answers once they're withheld.

    The loop passes a tool list every iteration (``tools`` is a list), so this
    keeps requesting a (nonexistent) tool and never finishes on its own. The
    synthesis call passes ``tools=None`` — that branch returns final text.
    """

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.tool_iterations = 0
        self.synth_calls = 0

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
        if tools is None:
            self.synth_calls += 1
            return LLMResponse(
                content="Partial summary: did A and B; C is still pending.",
                finish_reason="stop",
            )
        self.tool_iterations += 1
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"t{self.tool_iterations}", name="no_such_tool", arguments={})],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


def _make_agent(workspace: Path, provider: LLMProvider) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        # Exhaustion always synthesizes now, checkpoint or not; checkpoint only
        # adds the recovery snapshot on top. Disable it here so the test stays
        # focused on the synthesize branch without a shadow-git dependency.
        runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
    )


@pytest.mark.asyncio
async def test_exhaustion_delivers_synthesized_answer(workspace):
    provider = _ToolThenSynthProvider()
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="do the thing",
        ),
        session_key="s1",
    )

    assert out is not None
    # Synthesis text is delivered, not the canned apology.
    assert out[0] == "Partial summary: did A and B; C is still pending."
    assert "maximum number of tool call iterations" not in out[0]
    # Exactly one tools-disabled synthesis call, after the budget was spent.
    assert provider.synth_calls == 1
    assert provider.tool_iterations == 2


@pytest.mark.asyncio
async def test_synthesized_reply_lands_in_history(workspace):
    """The wrap-up must enter the conversation, not just stream to the user.

    Persistence downstream (session save / after_turn / backend.store) reads
    only the ``messages`` list the loop returns. If the synthesized reply is
    not appended there, the next turn — especially an interrupted-turn resume —
    cannot see what was already summarized.
    """
    provider = _ToolThenSynthProvider()
    agent = _make_agent(workspace, provider)

    final, _used, messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "do the thing"}],
    )

    assert outcome.status == "interrupted"
    assert final == "Partial summary: did A and B; C is still pending."
    # The last message is the synthesized assistant reply, ready to persist.
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == final
    # The injected synthesis prompt stays local to the helper — it must not
    # leak into the persisted history.
    assert not any("used up the tool-calling budget" in (m.get("content") or "") for m in messages)


# --------------------------------------------------------------------------- #
# Unit-level: _synthesize_final_on_exhaustion behavior                         #
# --------------------------------------------------------------------------- #


class _RecordingProvider:
    """Records chat_with_retry kwargs and returns a scripted response."""

    def __init__(self, response=None, raises: Exception | None = None):
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._response


def _bind_synth(provider, max_iterations: int = 40):
    fake_self = SimpleNamespace(
        provider=provider,
        max_iterations=max_iterations,
        _strip_think=AgentLoop._strip_think,
    )
    return AgentLoop._synthesize_final_on_exhaustion.__get__(fake_self)


@pytest.mark.asyncio
async def test_synthesis_withholds_tools_and_threads_fallback_chain():
    provider = _RecordingProvider(
        response=LLMResponse(content="wrapped up", finish_reason="stop"),
    )
    synth = _bind_synth(provider)

    result = await synth([{"role": "user", "content": "hi"}], "primary", ["backup"])

    assert result == "wrapped up"
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["tools"] is None  # no tools -> no ask_user/tool at the cliff
    assert call["model"] == "primary"
    assert call["fallback_models"] == ["backup"]
    # The synthesis nudge is appended as a trailing user turn.
    assert call["messages"][-1]["role"] == "user"
    assert call["messages"][-1]["content"] == _MAX_ITER_SYNTHESIS_PROMPT


def test_synthesis_prompt_pins_reply_language():
    """The English nudge must not drag a non-English conversation into English.

    The prompt is injected as a trailing user turn, so without an explicit
    instruction the model tends to answer in the prompt's language — producing
    a Chinese-then-English split when the user wrote Chinese. Pin the reply to
    the user's language.
    """
    assert "same language as the user" in _MAX_ITER_SYNTHESIS_PROMPT


@pytest.mark.asyncio
async def test_synthesis_falls_back_to_static_on_error_finish():
    provider = _RecordingProvider(
        response=LLMResponse(content="503 overloaded", finish_reason="error"),
    )
    synth = _bind_synth(provider, max_iterations=40)
    result = await synth([], "m", None)
    assert result == _MAX_ITER_STATIC_FALLBACK.format(n=40)


@pytest.mark.asyncio
async def test_synthesis_falls_back_to_static_on_empty_content():
    provider = _RecordingProvider(
        response=LLMResponse(content="", finish_reason="stop"),
    )
    synth = _bind_synth(provider, max_iterations=7)
    result = await synth([], "m", None)
    assert result == _MAX_ITER_STATIC_FALLBACK.format(n=7)


@pytest.mark.asyncio
async def test_synthesis_falls_back_to_static_on_exception():
    provider = _RecordingProvider(raises=RuntimeError("boom"))
    synth = _bind_synth(provider, max_iterations=40)
    result = await synth([], "m", None)
    assert result == _MAX_ITER_STATIC_FALLBACK.format(n=40)
