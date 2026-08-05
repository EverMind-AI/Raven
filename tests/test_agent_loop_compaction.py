"""In-turn context compaction: proactive threshold trigger, prune tier, summary tier.

Long agentic turns append tool results until the model's real context window
overflows. The compaction module prunes old tool output cheaply and, when that
is not enough, replaces the transcript head with an LLM summary while keeping
the recent tail verbatim.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.compaction import (
    PLACEHOLDER,
    SUMMARY_MARKER,
    build_compacted,
    projected_context_used,
    prune_old_tool_results,
    reserved_tokens,
    select_split,
    should_compact,
    tail_budget,
)
from raven.config.schema import CompactionConfig
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _history(rounds: int, result_chars: int = 40) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i in range(rounds):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]})
        msgs.append({"role": "tool", "content": f"result {i} " + "x" * result_chars})
    return msgs


# --------------------------------------------------------------------------- #
# unit: threshold math                                                          #
# --------------------------------------------------------------------------- #


def test_reserved_defaults_to_provider_budget_capped_at_20k():
    assert reserved_tokens(None, 32_768) == 20_000
    assert reserved_tokens(None, 8_192) == 8_192
    assert reserved_tokens(None, None) == 4_096
    assert reserved_tokens(5_000, 32_768) == 5_000


def test_tail_budget_default_quarter_of_usable_capped():
    assert tail_budget(None, 200_000, 20_000) == 8_000  # 25% of 180k, capped at 8k
    assert tail_budget(None, 24_000, 20_000) == 2_000  # 25% of 4k, floored at 2k
    assert tail_budget(3_000, 200_000, 20_000) == 3_000


def test_should_compact_at_threshold():
    assert should_compact(180_000, 200_000, 20_000) is True
    assert should_compact(179_999, 200_000, 20_000) is False
    assert should_compact(1, 0, 20_000) is False  # unknown window: never


# --------------------------------------------------------------------------- #
# unit: prune tier                                                              #
# --------------------------------------------------------------------------- #


def test_prune_keeps_recent_tool_results():
    msgs = _history(6)
    pruned, elided = prune_old_tool_results(msgs, keep_recent=3)
    assert elided == 3
    tool_contents = [m["content"] for m in pruned if m["role"] == "tool"]
    assert tool_contents[:3] == [PLACEHOLDER] * 3
    assert all("result" in c for c in tool_contents[3:])


def test_prune_is_idempotent_on_placeholders():
    msgs = _history(6)
    once, _ = prune_old_tool_results(msgs, keep_recent=3)
    twice, elided = prune_old_tool_results(once, keep_recent=3)
    assert elided == 0 and twice is once


# --------------------------------------------------------------------------- #
# unit: split selection + rebuild                                               #
# --------------------------------------------------------------------------- #


def _char_estimate(msgs: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in msgs)


def test_split_protects_system_and_first_user():
    msgs = _history(8)
    split = select_split(msgs, budget=200, estimate=_char_estimate)
    assert split is not None
    assert split > 2  # system + first user never enter the summarized head


def test_split_never_starts_tail_on_a_tool_result():
    msgs = _history(8)
    for budget in (50, 120, 300, 900):
        split = select_split(msgs, budget=budget, estimate=_char_estimate)
        if split is None:
            continue
        assert msgs[split].get("role") != "tool"


def test_split_none_when_history_too_short_to_summarize():
    msgs = _history(1)
    assert select_split(msgs, budget=10_000, estimate=_char_estimate) is None


def test_build_compacted_structure():
    msgs = _history(8)
    split = select_split(msgs, budget=200, estimate=_char_estimate)
    out = build_compacted(msgs, split, "SUMMARY OF WORK")
    assert out[0]["role"] == "system" and out[1]["content"] == "task"
    assert out[2]["role"] == "user" and SUMMARY_MARKER in out[2]["content"]
    assert "SUMMARY OF WORK" in out[2]["content"]
    assert out[3:] == msgs[split:]


# --------------------------------------------------------------------------- #
# config                                                                        #
# --------------------------------------------------------------------------- #


def test_compaction_config_defaults():
    cfg = CompactionConfig()
    assert cfg.auto is True and cfg.prune is True
    assert cfg.reserved_tokens is None and cfg.preserve_recent_tokens is None
    assert cfg.model is None


# --------------------------------------------------------------------------- #
# loop level                                                                    #
# --------------------------------------------------------------------------- #


class _CompactionProvider(LLMProvider):
    """Reports huge prompt usage to trip the proactive threshold, answers a
    summary request when asked, and finishes once compaction happened."""

    def __init__(self, window: int = 65_536):
        super().__init__(api_key="test")
        self._window = window
        self.summary_calls: list[list[dict]] = []
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
        if not tools and any("compacting an agent" in str(m.get("content", "")) for m in messages):
            self.summary_calls.append([dict(m) for m in messages])
            return LLMResponse(content="COMPACT-SUMMARY: goal, facts, progress", finish_reason="stop")
        self.seen_messages.append([dict(m) for m in messages])
        if any(SUMMARY_MARKER in str(m.get("content", "")) for m in messages):
            return LLMResponse(content="done after compaction", finish_reason="stop")
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"t{n_tool}", name="no_such_tool", arguments={})],
            finish_reason="tool_calls",
            usage={"prompt_tokens": self._window, "completion_tokens": 10},
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_proactive_compaction_triggers_before_overflow(workspace):
    provider = _CompactionProvider()
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
    assert out[0] == "done after compaction"
    assert len(provider.summary_calls) >= 1
    compacted_call = provider.seen_messages[-1]
    assert any(SUMMARY_MARKER in str(m.get("content", "")) for m in compacted_call)


class _HardOverflowNoToolsProvider(LLMProvider):
    """Overflows with too few tool results for pruning to help: only the
    summary tier can recover."""

    def __init__(self):
        super().__init__(api_key="test")
        self._overflowed = False
        self.summary_calls = 0

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
        if not tools and any("compacting an agent" in str(m.get("content", "")) for m in messages):
            self.summary_calls += 1
            return LLMResponse(content="summary of two long turns", finish_reason="stop")
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        if n_tool < 2:
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
        return LLMResponse(content="recovered via summary", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_reactive_overflow_uses_summary_when_prune_cannot_help(workspace):
    provider = _HardOverflowNoToolsProvider()
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
    assert out[0] == "recovered via summary"
    assert provider.summary_calls >= 1


class _FailingSummaryProvider(LLMProvider):
    """Every summary request fails, and the reported usage keeps the trigger
    armed — so without a breaker each iteration would pay for another one."""

    def __init__(self, window: int = 65_536):
        super().__init__(api_key="test")
        self._window = window
        self.summary_calls = 0
        self.main_calls = 0

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
        if not tools and any("compacting an agent" in str(m.get("content", "")) for m in messages):
            self.summary_calls += 1
            return LLMResponse(content="", finish_reason="error")
        self.main_calls += 1
        if self.main_calls >= 8:
            return LLMResponse(content="gave up on compaction", finish_reason="stop")
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"t{n_tool}", name="no_such_tool", arguments={})],
            finish_reason="tool_calls",
            usage={"prompt_tokens": self._window, "completion_tokens": 10},
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_summary_failures_trip_the_circuit_breaker(workspace):
    """A failed summary frees nothing, so the threshold stays crossed. Without
    the breaker the loop retries the summary on every remaining iteration."""
    provider = _FailingSummaryProvider()
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
    assert provider.summary_calls == AgentLoop._COMPACT_SUMMARY_MAX_FAILURES
    assert provider.main_calls > provider.summary_calls  # the turn kept going


@pytest.mark.asyncio
async def test_summary_breaker_resets_between_turns(workspace):
    agent = AgentLoop(
        provider=_FailingSummaryProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
    )
    agent._compact_summary_failures.set(AgentLoop._COMPACT_SUMMARY_MAX_FAILURES)
    await agent._run_agent_loop([{"role": "user", "content": "hi"}])
    assert agent._compact_summary_failures.get() == 0


@pytest.mark.asyncio
async def test_summary_breaker_is_isolated_between_concurrent_turns(workspace):
    """The AgentLoop is a long-lived singleton shared across sessions, so a
    concurrent turn's success must not reset another turn's failure streak --
    that would keep the breaker from ever tripping."""
    agent = AgentLoop(
        provider=_FailingSummaryProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
    )

    async def turn_a() -> int:
        agent._compact_summary_failures.set(2)
        await asyncio.sleep(0)  # yield so turn_b runs in between
        return agent._compact_summary_failures.get()

    async def turn_b() -> None:
        agent._compact_summary_failures.set(0)

    seen, _ = await asyncio.gather(turn_a(), turn_b())
    assert seen == 2, "a concurrent turn clobbered this turn's failure streak"


def test_projected_used_adds_growth_the_server_has_not_counted():
    """The reported usage predates the tool results appended after it; the
    trigger must add those or the next request overshoots by a whole round of
    tool output. 28k reported is under the 30k line, 28k + this round is not."""
    msgs = _history(4, result_chars=200)
    counted = len(msgs) - 2  # the last assistant + tool pair arrived after the report
    projected = projected_context_used(28_000, msgs, counted, _char_estimate)
    assert projected > 28_000
    assert not should_compact(28_000, 32_000, 2_000)
    assert should_compact(projected, 32_000, 2_000) == (projected >= 30_000)


def test_projected_used_is_a_noop_without_growth():
    msgs = _history(3)
    assert projected_context_used(1_234, msgs, len(msgs), _char_estimate) == 1_234


def test_projected_used_is_a_noop_without_a_report():
    assert projected_context_used(0, _history(3), 0, _char_estimate) == 0
