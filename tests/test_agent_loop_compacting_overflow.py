"""A turn whose own tool loop overflows the window, under the compacting strategy.

The overflow path hands the strategy the loop's whole working list. On any turn
but the first, the turn's question sits between the session's opening exchange
and the tail the retention planner protects, which is the range a reclaim
empties from the front -- so unless it is held, every request after the
refusal goes out answering nothing, and nothing in the request or the log says
so. This drives a real turn through ``_process_message`` with a provider that
refuses any request over its window, and checks that each request after the
refusal still carries the question and that the filed turn is whole.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.contracts.tool import Tool
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.utils.tokens import estimate_prompt_tokens

WINDOW = 60_000
QUESTION = "THE-TURN-QUESTION: list every failing module in the dump"
TOOL_ROUNDS = 13


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _Dump(Tool):
    """Answers each call with a different body of about 4.6k tokens."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "dump"

    @property
    def description(self) -> str:
        return "dumps a module report"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"part": {"type": "integer"}}}

    async def execute(self, part: int = 0, **_) -> str:
        self.calls += 1
        return "\n".join(f"part {part} module m{part}_{line} status ok elapsed {line * 7} ms" for line in range(330))


class _WindowedProvider(LLMProvider):
    """Refuses any request over ``WINDOW`` tokens; otherwise works the tool loop,
    then answers. Keeps every request it was sent."""

    _CHAT_RETRY_DELAYS = ()

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.seen: list[list[dict]] = []
        self.refused: list[int] = []
        self.asked = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **_):
        self.seen.append([dict(m) for m in messages])
        if estimate_prompt_tokens(messages, tools) > WINDOW:
            self.refused.append(len(self.seen) - 1)
            return LLMResponse(
                content=f"This model's maximum context length ({WINDOW} tokens) was exceeded",
                finish_reason="error",
            )
        if self.asked < TOOL_ROUNDS:
            part = self.asked
            self.asked += 1
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"c{part}", name="dump", arguments={"part": part})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="the failing modules are listed above", finish_reason="stop")


def _carries_question(request: list[dict]) -> bool:
    return any(m.get("role") == "user" and QUESTION in str(m.get("content")) for m in request)


@pytest.mark.asyncio
async def test_every_request_after_a_refusal_still_asks_the_turns_question(workspace):
    provider = _WindowedProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=40),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(context_window_tokens=WINDOW),
    )
    dump = _Dump()
    agent.tools.register(dump)
    session = agent.sessions.get_or_create("s1")
    session.add_message("user", "an earlier, unrelated question 0")
    session.add_message("assistant", "an earlier answer 0")
    agent.sessions.save(session)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text=QUESTION,
        ),
        session_key="s1",
    )

    assert out is not None and out[0] == "the failing modules are listed above"
    assert provider.refused, "the fixture must overflow the window at least once"
    first_refusal = provider.refused[0]
    after = provider.seen[first_refusal + 1 :]
    assert after, "the turn must have retried after the refusal"
    assert len(after[0]) < len(provider.seen[first_refusal]), "the strategy, not the elision, reclaimed"
    for index, request in enumerate(after, start=first_refusal + 1):
        assert _carries_question(request), f"request {index} went out without the turn's question"
        assert HistoryTrimmer.structural_errors(request) == []

    filed = agent.sessions.get_or_create("s1").messages
    assert HistoryTrimmer.structural_errors(filed) == []
    turn = filed[2:]
    assert turn[0]["role"] == "user" and QUESTION in str(turn[0]["content"])
    assert sum(1 for m in turn if m.get("role") == "tool") == TOOL_ROUNDS == dump.calls
    assert turn[-1]["role"] == "assistant" and turn[-1]["content"] == "the failing modules are listed above"
