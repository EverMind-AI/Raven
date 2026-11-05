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

from raven.agent.loop import TURN_SYNTHESIS_KEY, AgentLoop, TurnSynthesisPolicy
from raven.agent.loop._shared import (
    _MAX_ITER_STATIC_FALLBACK,
    _MAX_ITER_SYNTHESIS_PROMPT,
    _TOOL_MARKUP_REPAIR_PROMPT,
    tool_markup_only,
)
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
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
        # Exhaustion always synthesizes now, checkpoint or not; checkpoint only
        # adds the recovery snapshot on top. Disable it here so the test stays
        # focused on the synthesize branch without a shadow-git dependency.
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never"))),
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


@pytest.mark.asyncio
async def test_exhaustion_repairs_a_product_report_before_streaming_or_persisting(workspace):
    class Provider(_ToolThenSynthProvider):
        async def chat(self, messages, tools=None, **kwargs):
            if tools is not None:
                return await super().chat(messages, tools=tools, **kwargs)
            self.synth_calls += 1
            content = (
                "Partial summary: did A and B; C is still pending."
                if self.synth_calls == 1
                else "## Answer\nA and B are done.\n\n## Findings\nA and B.\n\n## Limitations\nC is pending."
            )
            return LLMResponse(content=content, finish_reason="stop")

    provider = Provider()
    agent = _make_agent(workspace, provider)
    streamed = []

    async def on_token_delta(delta):
        streamed.append(delta)

    policy = TurnSynthesisPolicy(
        guidance="Write the partial result under Answer, Findings, and Limitations.",
        repair_prompt=lambda text: "Rewrite into three sections." if "## Answer" not in text else None,
    )
    final, _used, messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "do the thing"}],
        hook_metadata={TURN_SYNTHESIS_KEY: policy},
        on_token_delta=on_token_delta,
    )

    assert outcome.status == "interrupted"
    assert provider.synth_calls == 2
    assert streamed == [final]
    assert final.startswith("## Answer")
    assert messages[-1]["content"] == final


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
        _repair_tool_markup=AgentLoop._repair_tool_markup,
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


@pytest.mark.asyncio
async def test_the_wrap_up_is_labelled_the_main_line_it_continues():
    """The wrap-up sends the turn's own conversation plus one instruction, so
    the trajectory must read it as a continuation of the main line, not as a
    side question that starts a conversation of its own."""
    from raven.observability.purpose import current

    labels: list[str | None] = []

    class Labelling(_RecordingProvider):
        async def chat_with_retry(self, **kwargs):
            labels.append(current())
            return await super().chat_with_retry(**kwargs)

    response = LLMResponse(content="wrapped up", finish_reason="stop")
    await _bind_synth(Labelling(response=response))([{"role": "user", "content": "hi"}], "primary", None)
    policy = TurnSynthesisPolicy(guidance="Use three sections.", repair_prompt=lambda text: "again")
    await _bind_synth(Labelling(response=response))([], "primary", None, synthesis_policy=policy)

    assert labels == ["main", "main", "main"]
    assert current() is None


@pytest.mark.asyncio
async def test_product_synthesis_formats_the_static_fallback():
    provider = _RecordingProvider(raises=RuntimeError("unavailable"))
    synth = _bind_synth(provider)
    policy = TurnSynthesisPolicy(
        guidance="Use three sections.",
        format_fallback=lambda reason: f"## Answer\n{reason}\n\n## Findings\nNone\n\n## Limitations\nInterrupted",
    )

    result = await synth([], "primary", None, synthesis_policy=policy)

    assert result.startswith("## Answer\n")
    assert "maximum number of tool call iterations" in result
    assert provider.calls[0]["tools"] is None
    assert provider.calls[0]["messages"][-1]["content"].endswith("Use three sections.")


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


# --------------------------------------------------------------------------- #
# A wrap-up that writes a tool call as text is asked once for a plain answer   #
# --------------------------------------------------------------------------- #

_QWEN_CALL = "<tool_call>\n<function=web_search>\n<parameter=query>\nwho won\n</parameter>\n</function>\n</tool_call>"
_HERMES_CALL = '<tool_call>\n{"name": "web_search", "arguments": {"query": "who won"}}\n</tool_call>'


class _Replies(_RecordingProvider):
    """Answers each call with the next reply in a list."""

    def __init__(self, *replies: str) -> None:
        super().__init__(response=None)
        self._replies = list(replies)

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(content=self._replies.pop(0), finish_reason="stop")


@pytest.mark.parametrize(
    ("text", "markup"),
    [
        (_QWEN_CALL, True),
        (_QWEN_CALL + "\n" + _QWEN_CALL, True),
        (_HERMES_CALL, True),
        ("<tool_call>\n<function=web_fetch>\n<parameter=url>\nhttps://x", True),  # cut off mid-call
        ("Alice won.\n\n" + _QWEN_CALL, False),
        ("The model emits `<tool_call>` tags when it wants a tool.", False),
        ("", False),
        (None, False),
    ],
)
def test_what_counts_as_a_tool_call_written_out_as_text(text, markup):
    assert tool_markup_only(text) is markup


@pytest.mark.parametrize("policy", [None, TurnSynthesisPolicy(guidance="Name your best guess.")])
@pytest.mark.asyncio
async def test_a_wrap_up_that_calls_a_tool_as_text_is_asked_once_for_a_plain_answer(policy):
    provider = _Replies(_QWEN_CALL, "Alice won, on the evidence gathered.")
    result = await _bind_synth(provider)([{"role": "user", "content": "who won?"}], "m", None, synthesis_policy=policy)

    assert result == "Alice won, on the evidence gathered."
    assert len(provider.calls) == 2
    second = provider.calls[1]
    assert second["tools"] is None
    assert second["messages"][-2] == {"role": "assistant", "content": _QWEN_CALL}
    assert second["messages"][-1] == {"role": "user", "content": _TOOL_MARKUP_REPAIR_PROMPT}


@pytest.mark.parametrize("policy", [None, TurnSynthesisPolicy(guidance="Name your best guess.")])
@pytest.mark.asyncio
async def test_markup_twice_falls_back_rather_than_shipping_a_tool_call(policy):
    provider = _Replies(_QWEN_CALL, _HERMES_CALL)
    result = await _bind_synth(provider, max_iterations=9)([], "m", None, synthesis_policy=policy)

    assert result == _MAX_ITER_STATIC_FALLBACK.format(n=9)
    assert len(provider.calls) == 2, "asked once, not until it complies"


@pytest.mark.asyncio
async def test_a_plain_wrap_up_is_not_asked_again():
    provider = _Replies("Alice won.\n\n" + _QWEN_CALL)
    result = await _bind_synth(provider)([], "m", None)

    assert result.startswith("Alice won.")
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_a_streamed_wrap_up_streams_the_plain_answer_after_the_markup():
    provider = _Replies("Alice won.")
    deltas: list[str] = []

    async def _stream(**kwargs):
        await kwargs["on_token_delta"](_QWEN_CALL)
        return LLMResponse(content=_QWEN_CALL, finish_reason="stop")

    async def _on_delta(text: str) -> None:
        deltas.append(text)

    fake_self = SimpleNamespace(
        provider=provider,
        max_iterations=40,
        _strip_think=AgentLoop._strip_think,
        _repair_tool_markup=AgentLoop._repair_tool_markup,
        _llm_call_stream=_stream,
    )
    synth = AgentLoop._synthesize_final_on_exhaustion.__get__(fake_self)

    result = await synth([], "m", None, on_token_delta=_on_delta)

    assert result == "Alice won."
    assert deltas == [_QWEN_CALL, "Alice won."]
