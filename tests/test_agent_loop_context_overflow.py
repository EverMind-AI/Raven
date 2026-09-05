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
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
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
