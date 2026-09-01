"""Context-overflow recovery: emergency shrink + retry instead of fatal error.

The structured classifier flags ``should_compress`` on a context-window
overflow; the loop elides older tool-result bodies and retries the iteration
rather than ending the turn with an error.

Also the in-turn transcript compaction layers gated on
``agents.defaults.compaction`` (factory-off): with the flag off the reactive
elision above stays the loop's only in-turn shrink (pinned here), and with it
on a proactive threshold prunes and, when pruning is not enough, an LLM head
summary compacts the transcript before the window blows.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop, compaction
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.config.schema import AgentDefaults, CompactionConfig
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
        policy=TurnPolicy(max_iterations=12),
        tools=ToolWiring(restrict_to_workspace=True),
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
# unit: compaction trigger and budget arithmetic                               #
# --------------------------------------------------------------------------- #


def test_should_compact_base_trigger_is_window_minus_reserved():
    assert compaction.should_compact(910, 1000, 100) is True
    assert compaction.should_compact(899, 1000, 100) is False
    assert compaction.should_compact(1, 0, 100) is False


def test_should_compact_trigger_ratio_lowers_the_line():
    assert compaction.should_compact(500, 1000, 100, trigger_ratio=0.5) is True
    assert compaction.should_compact(499, 1000, 100, trigger_ratio=0.5) is False
    # A ratio above the base line never raises it.
    assert compaction.should_compact(910, 1000, 100, trigger_ratio=0.99) is True


def test_reserved_tokens_derivation_caps_at_20k():
    assert compaction.reserved_tokens(None, 16_384) == 16_384
    assert compaction.reserved_tokens(None, 64_000) == 20_000
    assert compaction.reserved_tokens(123, 64_000) == 123


def test_tail_budget_derivation_is_quarter_of_usable_clamped():
    assert compaction.tail_budget(None, 100_000, 20_000) == 8_000  # cap
    assert compaction.tail_budget(None, 10_000, 8_000) == 2_000  # floor
    assert compaction.tail_budget(None, 20_000, 4_000) == 4_000  # 25% of 16k
    assert compaction.tail_budget(555, 100_000, 20_000) == 555


# --------------------------------------------------------------------------- #
# loop level: the compaction layers behind agents.defaults.compaction          #
# --------------------------------------------------------------------------- #

_BIG_ASSISTANT = "Working through the accumulated evidence. " * 200
_SUMMARY_TEXT = "Compacted handoff brief. " * 60
_HUGE_USAGE = {"prompt_tokens": 4990, "completion_tokens": 10}


class _CompactionScriptProvider(LLMProvider):
    """Scripted main-loop responses; answers head-summary requests separately."""

    def __init__(self, main_responses, summary_text=_SUMMARY_TEXT, fail_summary=False):
        super().__init__(api_key="test")
        self._main = list(main_responses)
        self._summary_text = summary_text
        self._fail_summary = fail_summary
        self.main_calls: list[list[dict]] = []
        self.summary_calls: list[list[dict]] = []
        self.summary_models: list[str | None] = []

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
        if messages and messages[0].get("content") == compaction.SUMMARY_INSTRUCTIONS:
            self.summary_calls.append([dict(m) for m in messages])
            self.summary_models.append(model)
            if self._fail_summary:
                return LLMResponse(content="", finish_reason="error")
            return LLMResponse(content=self._summary_text, finish_reason="stop")
        self.main_calls.append([dict(m) for m in messages])
        index = min(len(self.main_calls) - 1, len(self._main) - 1)
        return self._main[index]

    def get_default_model(self) -> str:
        return "stub"


def _tool_step(i: int, usage: dict | None = None, content: str = "") -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=[ToolCallRequest(id=f"c{i}", name="no_such_tool", arguments={})],
        finish_reason="tool_calls",
        usage=dict(usage or {}),
    )


def _answer(text: str = "answer after compaction") -> LLMResponse:
    return LLMResponse(content=text, finish_reason="stop")


def _overflow_error() -> LLMResponse:
    return LLMResponse(
        content="This model's maximum context length (8192 tokens) was exceeded",
        finish_reason="error",
    )


def _seed_rounds(n: int, body) -> list[dict]:
    """n completed tool rounds; ``body(i)`` writes each tool result."""
    rounds: list[dict] = []
    for i in range(n):
        rounds.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"s{i}", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}}
                ],
            }
        )
        rounds.append({"role": "tool", "tool_call_id": f"s{i}", "content": body(i)})
    return rounds


def _seed_body(i: int) -> str:
    return f"seed evidence {i} " * 40


def _agent(workspace, provider, cfg=None, max_iterations=12) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=max_iterations),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            context_window_tokens=1000,
            compaction_config=cfg,
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
        ),
    )


def _cfg(**overrides) -> CompactionConfig:
    values = {"enabled": True, "reserved_tokens": 100, "preserve_recent_tokens": 300}
    values.update(overrides)
    return CompactionConfig(**values)


def _initial(seed_rounds: int = 0, body=_seed_body) -> list[dict]:
    return [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "the task statement"},
        *_seed_rounds(seed_rounds, body),
    ]


@pytest.mark.asyncio
async def test_factory_default_runs_no_proactive_layer(workspace):
    """Off is byte-identical to today: usage far over any threshold moves
    nothing -- no summary call, no pruning, no extra or repeated iterations."""
    provider = _CompactionScriptProvider(
        [_tool_step(1, usage=_HUGE_USAGE), _tool_step(2, usage=_HUGE_USAGE), _answer()]
    )
    agent = _agent(workspace, provider, cfg=None)
    episodes: list[int] = []

    async def on_episode(i: int) -> None:
        episodes.append(i)

    final, _used, messages, outcome = await agent._run_agent_loop(_initial(seed_rounds=6), on_episode_start=on_episode)

    assert final == "answer after compaction"
    assert outcome.status == "completed"
    assert provider.summary_calls == []
    assert episodes == [0, 1, 2]
    last_call = provider.main_calls[-1]
    assert not any(m.get("content") == _PLACEHOLDER for m in last_call)
    assert not any(str(m.get("content", "")).startswith(compaction.SUMMARY_MARKER) for m in last_call)
    assert any("seed evidence 0" in str(m.get("content", "")) for m in last_call)


@pytest.mark.asyncio
async def test_disabled_overflow_with_nothing_to_elide_stays_fatal(workspace):
    """The reactive completion layer does not exist while the flag is off."""
    seeds = _seed_rounds(5, lambda i: _PLACEHOLDER) + _seed_rounds(1, lambda i: "tail body " * 100)
    provider = _CompactionScriptProvider([_tool_step(1), _overflow_error(), _answer()])
    agent = _agent(workspace, provider, cfg=None)

    final, _used, _messages, outcome = await agent._run_agent_loop([*_initial(), *seeds])

    assert outcome.status == "error"
    assert final != "answer after compaction"
    assert provider.summary_calls == []


@pytest.mark.asyncio
async def test_proactive_prune_alone_can_clear_the_threshold(workspace):
    """Deterministic pruning runs first; when its projected savings clear the
    trigger line no summary call is paid for."""
    usage = {"prompt_tokens": 900, "completion_tokens": 10}
    provider = _CompactionScriptProvider([_tool_step(1, usage=usage), _answer()])
    agent = _agent(workspace, provider, cfg=_cfg())

    final, _used, _messages, _outcome = await agent._run_agent_loop(_initial(seed_rounds=6))

    assert final == "answer after compaction"
    assert provider.summary_calls == []
    recovery_call = provider.main_calls[-1]
    assert sum(1 for m in recovery_call if m.get("content") == _PLACEHOLDER) > 0


@pytest.mark.asyncio
async def test_proactive_summary_after_prune_preserves_prefix_and_tail(workspace):
    """Over the trigger even after pruning, the head becomes one summary while
    the system prefix, the first user message and a recent tail stay verbatim;
    the proactive iteration is billed (no episode repeats)."""
    provider = _CompactionScriptProvider([_tool_step(1, usage=_HUGE_USAGE), _answer()])
    agent = _agent(workspace, provider, cfg=_cfg())
    episodes: list[int] = []

    async def on_episode(i: int) -> None:
        episodes.append(i)

    final, _used, _messages, _outcome = await agent._run_agent_loop(
        _initial(seed_rounds=6), on_episode_start=on_episode
    )

    assert final == "answer after compaction"
    assert len(provider.summary_calls) == 1
    # Prune ran before the summary: the head handed to the summary request
    # already carries elided bodies.
    transcript = provider.summary_calls[0][1]["content"]
    assert _PLACEHOLDER in transcript
    post = provider.main_calls[1]
    assert post[0]["content"] == "sys prompt"
    assert post[1]["content"] == "the task statement"
    assert str(post[2]["content"]).startswith(compaction.SUMMARY_MARKER)
    assert _SUMMARY_TEXT.strip() in str(post[2]["content"])
    # The most recent tool evidence survives verbatim, past the summary.
    assert any("seed evidence 5" in str(m.get("content", "")) for m in post[3:])
    # Proactive compaction consumes no retry exemption: iterations only move
    # forward (the unbilled repeat stays an overflow-retry privilege).
    assert episodes == [0, 1]


@pytest.mark.asyncio
async def test_prune_false_goes_straight_to_summary(workspace):
    provider = _CompactionScriptProvider([_tool_step(1, usage=_HUGE_USAGE), _answer()])
    agent = _agent(workspace, provider, cfg=_cfg(prune=False))

    final, _used, _messages, _outcome = await agent._run_agent_loop(_initial(seed_rounds=6))

    assert final == "answer after compaction"
    assert len(provider.summary_calls) == 1
    transcript = provider.summary_calls[0][1]["content"]
    assert _PLACEHOLDER not in transcript
    assert "seed evidence 0" in transcript
    assert not any(m.get("content") == _PLACEHOLDER for m in provider.main_calls[1])


@pytest.mark.asyncio
async def test_summary_calls_share_the_overflow_retry_budget(workspace):
    """A turn that stays over the trigger pays at most _MAX_COMPRESS_RETRIES
    summary calls, however many iterations keep crossing the line."""
    steps = [_tool_step(i, usage=_HUGE_USAGE, content=_BIG_ASSISTANT) for i in range(1, 5)]
    provider = _CompactionScriptProvider([*steps, _answer()])
    agent = _agent(workspace, provider, cfg=_cfg(prune=False))

    final, _used, _messages, _outcome = await agent._run_agent_loop(_initial())

    assert final == "answer after compaction"
    assert len(provider.summary_calls) == AgentLoop._MAX_COMPRESS_RETRIES
    assert len(provider.main_calls) == 5


@pytest.mark.asyncio
async def test_failed_proactive_summary_degrades_to_uncompacted_turn(workspace):
    """A summary endpoint that fails costs its bounded attempts and nothing
    else: the turn proceeds uncompacted to its normal answer."""
    steps = [_tool_step(i, usage=_HUGE_USAGE, content=_BIG_ASSISTANT) for i in range(1, 5)]
    provider = _CompactionScriptProvider([*steps, _answer("done anyway")], fail_summary=True)
    agent = _agent(workspace, provider, cfg=_cfg(prune=False))

    final, _used, _messages, _outcome = await agent._run_agent_loop(_initial())

    assert final == "done anyway"
    assert len(provider.summary_calls) == AgentLoop._MAX_COMPRESS_RETRIES
    assert not any(
        str(m.get("content", "")).startswith(compaction.SUMMARY_MARKER) for call in provider.main_calls for m in call
    )


@pytest.mark.asyncio
async def test_reactive_summary_recovers_an_overflow_nothing_left_to_elide(workspace):
    """The completion of the reactive path: elision finds nothing, the summary
    retries instead of surfacing a fatal error, and the overflowed call stays
    unbilled (the episode repeats under a tight iteration budget)."""
    seeds = [*_seed_rounds(5, lambda i: _PLACEHOLDER), *_seed_rounds(1, lambda i: "recent tail body " * 100)]
    provider = _CompactionScriptProvider([_tool_step(1), _overflow_error(), _answer()])
    agent = _agent(workspace, provider, cfg=_cfg(), max_iterations=2)
    episodes: list[int] = []

    async def on_episode(i: int) -> None:
        episodes.append(i)

    final, _used, messages, outcome = await agent._run_agent_loop([*_initial(), *seeds], on_episode_start=on_episode)

    assert final == "answer after compaction"
    assert outcome.status == "completed"
    assert len(provider.summary_calls) == 1
    assert episodes == [0, 1, 1]
    assert any(str(m.get("content", "")).startswith(compaction.SUMMARY_MARKER) for m in messages)


@pytest.mark.asyncio
async def test_failed_reactive_summary_leaves_todays_fatal_outcome(workspace):
    """When the summary itself fails, the overflow surfaces exactly as it does
    with the flag off: degraded, never worse than today."""
    seeds = [*_seed_rounds(5, lambda i: _PLACEHOLDER), *_seed_rounds(1, lambda i: "recent tail body " * 100)]
    script = [_tool_step(1), _overflow_error(), _answer()]

    off_provider = _CompactionScriptProvider(list(script))
    off_final, _u, _m, off_outcome = await _agent(workspace, off_provider, cfg=None)._run_agent_loop(
        [*_initial(), *seeds]
    )
    on_provider = _CompactionScriptProvider(list(script), fail_summary=True)
    on_final, _u, _m, on_outcome = await _agent(workspace, on_provider, cfg=_cfg())._run_agent_loop(
        [*_initial(), *seeds]
    )

    assert len(on_provider.summary_calls) == 1
    assert (on_final, on_outcome.status) == (off_final, off_outcome.status)
    assert on_outcome.status == "error"


@pytest.mark.asyncio
async def test_a_config_slice_with_compaction_enabled_activates_the_layers(workspace):
    """The mechanism the product slices ride: a rendered config fragment with
    ``compaction.enabled: true`` -- wired through the same
    ``agents.defaults.compaction`` address the runtime uses -- switches the
    layers on with no code of its own."""
    defaults = AgentDefaults.model_validate(
        {"compaction": {"enabled": True, "reservedTokens": 100, "preserveRecentTokens": 300}}
    )
    assert defaults.compaction.enabled is True
    seeds = [*_seed_rounds(5, lambda i: _PLACEHOLDER), *_seed_rounds(1, lambda i: "recent tail body " * 100)]
    provider = _CompactionScriptProvider([_tool_step(1), _overflow_error(), _answer()])
    agent = _agent(workspace, provider, cfg=defaults.compaction)

    final, _used, _messages, outcome = await agent._run_agent_loop([*_initial(), *seeds])

    assert final == "answer after compaction"
    assert outcome.status == "completed"
    assert len(provider.summary_calls) == 1


# --------------------------------------------------------------------------- #
# ported from the fork's tests/test_agent_loop_compaction.py (cp3): the       #
# fork-only pins that hold on the landed w99 faces. The breaker trio, the     #
# growth-projection trio and the opaque-400 density pin are deliberately not  #
# ported (w99 ruling; cp3 deviations 17-19).                                  #
# --------------------------------------------------------------------------- #


def _history(rounds: int, result_chars: int = 40) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i in range(rounds):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]})
        msgs.append({"role": "tool", "content": f"result {i} " + "x" * result_chars})
    return msgs


def _char_estimate(msgs: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in msgs)


def test_should_compact_trigger_ratio_lowers_line():
    # Base line is 180k (window - reserved). A 0.6 ratio pulls it down to 120k.
    assert compaction.should_compact(130_000, 200_000, 20_000) is False  # below base, no ratio
    assert compaction.should_compact(130_000, 200_000, 20_000, 0.6) is True  # 130k >= 0.6*200k
    assert compaction.should_compact(119_999, 200_000, 20_000, 0.6) is False
    # Ratio never raises the line above the base trigger.
    assert compaction.should_compact(185_000, 200_000, 20_000, 0.95) is True
    # Out-of-range ratios are ignored (fall back to base trigger).
    assert compaction.should_compact(130_000, 200_000, 20_000, 0.0) is False
    assert compaction.should_compact(130_000, 200_000, 20_000, 1.0) is False


def test_prune_is_idempotent_on_placeholders():
    msgs = _history(6)
    once, _ = AgentLoop._emergency_shrink(msgs)
    twice, elided = AgentLoop._emergency_shrink(once)
    assert elided == 0 and twice == once


def test_split_protects_system_and_first_user():
    msgs = _history(8)
    split = compaction.select_split(msgs, budget=200, estimate=_char_estimate)
    assert split is not None
    assert split > 2  # system + first user never enter the summarized head


def test_split_never_starts_tail_on_a_tool_result():
    msgs = _history(8)
    for budget in (50, 120, 300, 900):
        split = compaction.select_split(msgs, budget=budget, estimate=_char_estimate)
        if split is None:
            continue
        assert msgs[split].get("role") != "tool"


def test_split_none_when_history_too_short_to_summarize():
    msgs = _history(1)
    assert compaction.select_split(msgs, budget=10_000, estimate=_char_estimate) is None


def test_build_compacted_structure():
    msgs = _history(8)
    split = compaction.select_split(msgs, budget=200, estimate=_char_estimate)
    out = compaction.build_compacted(msgs, split, "SUMMARY OF WORK")
    assert out[0]["role"] == "system" and out[1]["content"] == "task"
    assert out[2]["role"] == "user" and compaction.SUMMARY_MARKER in out[2]["content"]
    assert "SUMMARY OF WORK" in out[2]["content"]
    assert out[3:] == msgs[split:]


@pytest.mark.asyncio
async def test_summary_always_uses_the_session_model(workspace):
    """Summaries must ride the turn's own model: a pinned summary model would
    outlive a model switch and route every compaction to a retired endpoint
    (the fork's incident; the trunk config accepts and ignores the legacy
    ``model`` key, pinned in test_config_schema)."""
    provider = _CompactionScriptProvider([_tool_step(1, usage=_HUGE_USAGE), _answer()])
    agent = _agent(workspace, provider, cfg=_cfg(prune=False))

    final, _used, _messages, _outcome = await agent._run_agent_loop(_initial(seed_rounds=6))

    assert final == "answer after compaction"
    assert provider.summary_models and all(m == "stub" for m in provider.summary_models)
