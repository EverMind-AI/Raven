"""Context-overflow recovery: emergency shrink + retry instead of fatal error.

The structured classifier flags ``should_compress`` on a context-window
overflow; the loop elides older tool-result bodies and retries the iteration
rather than ending the turn with an error.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.tools.base import Tool
from raven.config.schema import CompactionConfig
from raven.providers.base import ErrorClassification, LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

_PLACEHOLDER = "[earlier tool output elided to fit the context window]"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# --------------------------------------------------------------------------- #
# unit: _emergency_shrink                                                      #
# --------------------------------------------------------------------------- #


def test_emergency_shrink_elides_all_but_recent_tool_results():
    msgs: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]})
        msgs.append({"role": "tool", "content": f"result {i}"})

    shrunk, elided = AgentLoop._emergency_shrink(msgs)

    assert elided == 3  # 6 tool results, keep most-recent 3
    tool_contents = [m["content"] for m in shrunk if m["role"] == "tool"]
    assert tool_contents == [_PLACEHOLDER] * 3 + ["result 3", "result 4", "result 5"]
    # non-tool messages untouched
    assert shrunk[0]["content"] == "sys" and shrunk[1]["content"] == "q"


def test_emergency_shrink_noop_when_few_tool_results():
    msgs = [{"role": "system", "content": "s"}, {"role": "tool", "content": "r0"}]
    shrunk, elided = AgentLoop._emergency_shrink(msgs)
    assert elided == 0 and shrunk is msgs


# --------------------------------------------------------------------------- #
# loop level: overflow -> shrink -> recover                                    #
# --------------------------------------------------------------------------- #


class _OverflowThenAnswerProvider(LLMProvider):
    """Accumulates tool results, overflows once, then answers after the shrink."""

    def __init__(self, tool_rounds: int = 5):
        super().__init__(api_key="test")
        self._tool_rounds = tool_rounds
        self._overflowed = False
        self.seen_messages: list[list[dict]] = []

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
        self.seen_messages.append([dict(m) for m in messages])
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        if n_tool < self._tool_rounds:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"t{n_tool}", name="no_such_tool", arguments={})],
                finish_reason="tool_calls",
            )
        if not self._overflowed:
            self._overflowed = True
            return LLMResponse(
                content="This model's maximum context length (8192 tokens) was exceeded",
                finish_reason="error",
            )
        return LLMResponse(content="answer after compaction", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_overflow_shrinks_and_recovers(workspace):
    provider = _OverflowThenAnswerProvider(tool_rounds=5)
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=12,
        restrict_to_workspace=True,
    )

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "answer after compaction"  # recovered, not the error
    assert provider._overflowed is True
    # the post-overflow (recovery) call saw elided placeholders, not 5 full results
    recovery_call = provider.seen_messages[-1]
    assert sum(1 for m in recovery_call if m.get("content") == _PLACEHOLDER) == 2  # 5 - keep 3


# --------------------------------------------------------------------------- #
# unit: _estimate_message_tokens                                              #
# --------------------------------------------------------------------------- #


def test_estimate_message_tokens_counts_str_list_and_reasoning():
    msgs = [
        {"role": "system", "content": "x" * 400},
        {"role": "tool", "content": ["a" * 200, "b" * 200]},
        # DSV4-style history keeps reasoning inline; missing it undercounts
        # the prompt by whole reasoning turns and defeats the near-window gate.
        {"role": "assistant", "content": None, "reasoning_content": "r" * 400, "tool_calls": [{"id": "t"}]},
    ]
    # (400 + 400 + 400) chars / 4
    assert AgentLoop._estimate_message_tokens(msgs) == 300


# --------------------------------------------------------------------------- #
# loop level: OPAQUE provider 400 (no "context length" text) still recovers   #
# --------------------------------------------------------------------------- #


class _OpaqueOverflowProvider(LLMProvider):
    """Overflows with an OpenRouter-style opaque 400 that carries no overflow
    wording, classified ``invalid_request`` (non-retryable) as in production --
    the near-window fallback must still shrink because the prompt is large."""

    def __init__(self, big_chars: int):
        super().__init__(api_key="test")
        self._overflowed = False
        self._big = "z" * big_chars
        self.seen_messages: list[list[dict]] = []

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
        self.seen_messages.append([dict(m) for m in messages])
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        if n_tool < 5:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"t{n_tool}", name="big", arguments={})],
                finish_reason="tool_calls",
            )
        if not self._overflowed:
            self._overflowed = True
            return LLMResponse(
                content="Error calling LLM: litellm.BadRequestError: OpenAIException - Provider returned error",
                finish_reason="error",
                error_classification=ErrorClassification("invalid_request"),
            )
        return LLMResponse(content="recovered from opaque overflow", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


class _BigTool(Tool):
    def __init__(self, payload: str):
        self._payload = payload

    @property
    def name(self):
        return "big"

    @property
    def description(self):
        return "returns a large payload"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return self._payload


@pytest.mark.asyncio
async def test_opaque_400_near_window_shrinks_and_recovers(workspace):
    # window 10k; each tool result ~2k tokens (8k chars) so 5 rounds ~ over 0.8*window
    provider = _OpaqueOverflowProvider(big_chars=0)
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=12,
        restrict_to_workspace=True,
        context_window_tokens=10_000,
    )
    agent.tools.register(_BigTool("q" * 8_000))

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "recovered from opaque overflow"
    assert provider._overflowed is True
    # recovery came from the loop-level shrink, not a provider-level blind
    # retry: the post-shrink request must carry an elided placeholder
    assert any(m.get("content") == _PLACEHOLDER for call in provider.seen_messages for m in call)


@pytest.mark.asyncio
async def test_opaque_400_small_prompt_does_not_shrink(workspace):
    # A genuine invalid_request on a small prompt must NOT trigger the
    # overflow shrink: no tool result is ever elided to a placeholder.
    provider = _OpaqueOverflowProvider(big_chars=0)
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=12,
        restrict_to_workspace=True,
        context_window_tokens=10_000_000,  # huge -> prompt never near window
    )
    agent.tools.register(_BigTool("q" * 100))

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )
    # the shrink path never ran, so no request ever carried an elided placeholder
    assert not any(m.get("content") == _PLACEHOLDER for call in provider.seen_messages for m in call)


class _DenseOverflowProvider(LLMProvider):
    """Reports a near-window billed context while the message text itself is
    tiny -- the chars//4 estimate stays far below the threshold, mirroring
    token-dense content (disassembly, C code) that runs at ~2 chars/token."""

    def __init__(self):
        super().__init__(api_key="test")
        self._overflowed = False
        self.seen_messages: list[list[dict]] = []

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
        self.seen_messages.append([dict(m) for m in messages])
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        if n_tool < 5:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"t{n_tool}", name="big", arguments={})],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 9_000, "completion_tokens": 500},
            )
        if not self._overflowed:
            self._overflowed = True
            return LLMResponse(
                content="Error calling LLM: litellm.BadRequestError: OpenAIException - Provider returned error",
                finish_reason="error",
                error_classification=ErrorClassification("invalid_request"),
            )
        return LLMResponse(content="recovered from dense overflow", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_opaque_400_dense_content_shrinks_via_billed_usage(workspace):
    # Tool payloads are tiny, so the char estimate never crosses 0.8*window;
    # only the billed usage from earlier successful calls says the context is
    # nearly full. The shrink must still fire on the opaque 400.
    #
    # The proactive tier is off here on purpose: this stub reports 9.5k billed
    # against a 10k window on every successful call, so proactive compaction
    # would fire each iteration and consume the prunable tool results before
    # the 400 ever arrives -- which is what the proactive tests cover
    # (test_agent_loop_compaction.py). This one is about the reactive path.
    provider = _DenseOverflowProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=12,
        restrict_to_workspace=True,
        context_window_tokens=10_000,
        compaction=CompactionConfig(auto=False),
    )
    agent.tools.register(_BigTool("q" * 40))

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "recovered from dense overflow"
    assert provider._overflowed is True
    assert any(m.get("content") == _PLACEHOLDER for call in provider.seen_messages for m in call)


# --------------------------------------------------------------------------- #
# unit: _emergency_shrink elides accumulated reasoning too                     #
# --------------------------------------------------------------------------- #


def test_emergency_shrink_elides_old_reasoning_content():
    # 8 assistant turns each carrying big reasoning, few tool results: eliding
    # tool bodies alone frees almost nothing; reasoning is the real bulk.
    msgs: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
    for i in range(8):
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": f"long reasoning {i} " * 50,
                "tool_calls": [{"id": f"t{i}"}],
            }
        )
        msgs.append({"role": "tool", "content": f"r{i}"})

    shrunk, elided = AgentLoop._emergency_shrink(msgs)

    kept = [m for m in shrunk if m.get("reasoning_content")]
    assert len(kept) == 4  # _SHRINK_KEEP_RECENT_REASONING
    # the kept ones are the most recent
    assert kept[0]["reasoning_content"].startswith("long reasoning 4")
    # older assistant turns lost reasoning but keep their tool_calls (pairing)
    stripped = [m for m in shrunk if m.get("role") == "assistant" and not m.get("reasoning_content")]
    assert stripped and all(m.get("tool_calls") for m in stripped)
    assert elided > 0


def test_emergency_shrink_strips_thinking_blocks_too():
    msgs: list[dict] = [{"role": "system", "content": "s"}]
    for i in range(6):
        msgs.append(
            {"role": "assistant", "content": "", "thinking_blocks": [{"t": i}], "tool_calls": [{"id": f"t{i}"}]}
        )
    shrunk, elided = AgentLoop._emergency_shrink(msgs)
    with_blocks = [m for m in shrunk if m.get("thinking_blocks")]
    assert len(with_blocks) == 4 and elided == 2
