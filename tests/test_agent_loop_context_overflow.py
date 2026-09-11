"""Context-overflow recovery: emergency shrink + retry instead of fatal error.

The structured classifier flags ``should_compress`` on a context-window
overflow; the loop elides older tool-result bodies and retries the iteration
rather than ending the turn with an error.

Also the in-turn transcript compaction layers gated on
``agents.defaults.compaction`` (factory-off): with the flag off no proactive
layer runs (pinned here) and the loop's in-turn shrinks are the standing image
window (``_window_images``) and the reactive elision above; with it on a
proactive threshold prunes and, when pruning is not enough, an LLM head summary
compacts the transcript before the window blows.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from loguru import logger

from raven.agent.loop import AgentLoop, compaction
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.loop.recovery import REASONING_EFFORT_LADDER
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.config.schema import AgentDefaults, CompactionConfig
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.providers.rates import resolve_max_output_tokens
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.utils.tokens import estimate_prompt_tokens

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


def test_the_summary_budget_fits_every_shipped_products_window():
    """The arithmetic the raised budget missed: what binds at the trigger is
    the window, not the model's output ceiling.

    Each product's own config, run through this module's helpers. The head
    reaches the summary at the trigger line less the tail the request drops,
    and the budget on top of it has to leave the window standing. raven-code
    derives every value and is the one that did not: 372000 of head and 32000
    asked for against a declared 400000 window.
    """
    ceiling = 32_768
    cases = {
        # name: (limit, configured reserve, configured tail, trigger ratio)
        "raven-code": (400_000, None, None, None),
        "raven-design": (262_144, 8_192, 52_429, 0.8),
        "raven-ppt": (1_000_000, None, None, 0.85),
    }
    asked_before, asked_now = {}, {}
    for name, (limit, reserve, tail_cfg, ratio) in cases.items():
        reserved = compaction.reserved_tokens(reserve, ceiling)
        tail = compaction.tail_budget(tail_cfg, limit, reserved)
        trigger = min(max(0, limit - reserved), int(ratio * limit)) if ratio else max(0, limit - reserved)
        assert compaction.should_compact(trigger, limit, reserved, ratio)
        assert not compaction.should_compact(trigger - 1, limit, reserved, ratio)
        head = trigger - tail
        asked_before[name] = head + min(compaction.SUMMARY_MAX_TOKENS, ceiling)
        asked_now[name] = head + compaction.summary_output_budget(limit, head, ceiling)
        assert asked_now[name] <= limit, f"{name} still asks for more than its window"

    assert asked_before["raven-code"] - 400_000 == 4_000, "the finding's figure, before the bound"
    assert 400_000 - asked_now["raven-code"] == 18_600
    # The two that had room keep the whole ceiling: the bound only bites where
    # the window is what is short.
    assert compaction.summary_output_budget(262_144, 157_286, ceiling) == compaction.SUMMARY_MAX_TOKENS
    assert compaction.summary_output_budget(1_000_000, 842_000, ceiling) == compaction.SUMMARY_MAX_TOKENS


def test_the_summary_budget_keeps_its_other_two_bounds():
    """The ceiling still wins for a small model, and an unknown window does not
    clamp a request to nothing."""
    assert compaction.summary_output_budget(400_000, 1_000, 4_096) == 4_096
    assert compaction.summary_output_budget(0, 999_999, 32_768) == min(compaction.SUMMARY_MAX_TOKENS, 32_768)
    assert compaction.summary_output_budget(400_000, 399_000, 32_768) == 0, (
        "a prompt that fills the window on its own leaves nothing to answer with"
    )


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

    def __init__(self, main_responses, summary_text=_SUMMARY_TEXT, fail_summary=False, cut_summary=False):
        super().__init__(api_key="test")
        self._main = list(main_responses)
        self._summary_text = summary_text
        self._fail_summary = fail_summary
        self._cut_summary = cut_summary
        self.main_calls: list[list[dict]] = []
        self.summary_calls: list[list[dict]] = []
        self.summary_models: list[str | None] = []
        self.summary_efforts: list[str | None] = []
        self.summary_budgets: list[int | None] = []
        self.main_efforts: list[str | None] = []

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
            self.summary_efforts.append(reasoning_effort)
            self.summary_budgets.append(max_tokens)
            if self._fail_summary:
                # The measured shape: a body spent entirely on thinking, which
                # the endpoint reports as an ordinary stop, not an error.
                return LLMResponse(content="", finish_reason="stop")
            if self._cut_summary:
                # A brief that ran into its budget. Non-empty, so nothing but
                # the finish reason says it stops mid-sentence.
                return LLMResponse(content=self._summary_text, finish_reason="length")
            return LLMResponse(content=self._summary_text, finish_reason="stop")
        self.main_calls.append([dict(m) for m in messages])
        self.main_efforts.append(reasoning_effort)
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


def _agent(workspace, provider, cfg=None, max_iterations=12, window=16_000) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=max_iterations),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            # Two constraints, and only the first used to be met. The seeded
            # head has to cross the trigger line, and the window has to hold
            # the summary request plus a reply to it -- the largest head these
            # tests seed renders to about 6500 tokens, and at 1000 of window
            # there was no room to answer in at all. The trigger line itself
            # stays where it was; see ``_cfg``.
            context_window_tokens=window,
            compaction_config=cfg,
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
        ),
    )


def _cfg(**overrides) -> CompactionConfig:
    # 15100 against the 16000 window above puts the trigger at 900, which is
    # where it sat when the window was 1000 and the reserve was 100 -- just
    # above what a compaction here produces, which is what gives the
    # compact-again assertion below its bite.
    values = {"enabled": True, "reserved_tokens": 15_100, "preserve_recent_tokens": 300}
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
async def test_the_head_summary_is_not_asked_to_think(workspace):
    """A handoff brief is a transcript read back, not a decision.

    Run at the turn's own effort, two measured summary calls (107 head messages
    / 45264 chars, then 112 / 54248) spent the whole budget thinking and came
    back with an empty body -- a reasoning model pays for thinking out of the
    same budget as the answer. Compaction then degraded to blind elision for
    the rest of that run. The turn keeps its effort; the summary takes the
    ladder floor.
    """
    provider = _CompactionScriptProvider([_tool_step(1, usage=_HUGE_USAGE), _answer()])
    agent = _agent(workspace, provider, cfg=_cfg(prune=False))
    agent.set_session_policy("", reasoning_effort="high")

    await agent._run_agent_loop(_initial(seed_rounds=6))

    assert provider.summary_efforts == [compaction.SUMMARY_REASONING_EFFORT]
    assert compaction.SUMMARY_REASONING_EFFORT == REASONING_EFFORT_LADDER[-1]
    assert provider.main_efforts and set(provider.main_efforts) == {"high"}, (
        "the turn's own calls keep the session's effort; only the summary is pinned"
    )


@pytest.mark.asyncio
async def test_a_compaction_does_not_produce_what_it_would_have_to_compact_again(workspace):
    """The property the old tail-cap relation was standing in for.

    SUMMARY_MAX_TOKENS was _TAIL_CAP so that a summary plus a tail was bounded
    at 16000, under the 20000 _RESERVED_CAP. At 32000 that inequality no longer
    holds on paper: a summary run to its ceiling beside an 8000 tail is 40000,
    and in this module's own fallback window (65536 of window, 20000 reserved,
    so a trigger at 45536) that would land back over the line.

    What holds it now is the window itself: the budget is what the window has
    left once the request is built, so a summary cannot ask for more than the
    turn it is compacting for can hold. It is not close in practice either, and
    the budget is a ceiling rather than a target. Measured against a real head in the size band of the two failures
    this fix comes from -- 158 messages, 46860 chars -- the brief came back at
    1466 completion tokens behind 25 reasoning tokens, 4.6% of the ceiling. And
    the pathological case is bounded regardless: _MAX_COMPRESS_RETRIES caps
    summary calls per turn, so a compaction that did land over the line costs
    one more call, not a loop.

    So what is pinned here is the property itself rather than an arithmetic
    stand-in for it: what a compaction produces sits below the line that
    triggered it, and the request that produced it fitted the window. The
    budget is still bounded by the model's own ceiling too, so raising it
    cannot turn a summary into a request a small model refuses.
    """
    provider = _CompactionScriptProvider([_tool_step(1, usage=_HUGE_USAGE), _answer()])
    agent = _agent(workspace, provider, cfg=_cfg(prune=False))

    await agent._run_agent_loop(_initial(seed_rounds=6))

    ceiling = resolve_max_output_tokens("stub", allow_fetch=False)
    (budget,) = provider.summary_budgets
    asked = estimate_prompt_tokens(provider.summary_calls[0]) + budget
    assert asked <= 16_000, f"the summary request has to fit the window it compacts for: {asked} of 16000"
    assert 0 < budget < min(compaction.SUMMARY_MAX_TOKENS, ceiling), (
        "with this much head in a 16000 window it is the window that binds, not the ceiling"
    )

    # The request the loop built from the compacted transcript, against the
    # same trigger arithmetic that fired: limit 16000, reserved 15100 (see _cfg).
    post = provider.main_calls[1]
    assert str(post[2]["content"]).startswith(compaction.SUMMARY_MARKER), "the head really was summarized"
    assert not compaction.should_compact(estimate_prompt_tokens(post), 16_000, 15_100), (
        "a compaction that lands over its own trigger line would compact again next iteration"
    )


@pytest.mark.asyncio
async def test_a_failed_head_summary_says_what_the_turn_does_instead(workspace):
    """The only trace of this failure was one warning, and the elisions that
    followed read like ordinary housekeeping -- so reconstructing why a run
    lost 61 transcript items meant reading the chain backwards. The failure
    names its consequence, and the elisions after it say they are what is left.
    """
    steps = [_tool_step(i, usage=_HUGE_USAGE, content=_BIG_ASSISTANT) for i in range(1, 5)]
    provider = _CompactionScriptProvider([*steps, _answer()], fail_summary=True)
    agent = _agent(workspace, provider, cfg=_cfg())

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        await agent._run_agent_loop(_initial(seed_rounds=6))
    finally:
        logger.remove(sink)

    failures = [line for line in lines if "Transcript head summary failed" in line]
    assert failures, "the failure is recorded"
    assert all("ERROR" in line for line in failures), "not a warning among warnings"
    assert all("falls back to eliding" in line for line in failures), "the line says what happens next"
    followups = [line for line in lines if "elided" in line and "head summary failed earlier" in line]
    assert followups, "an elision after a failed summary says it is what is left"


@pytest.mark.asyncio
async def test_a_summary_with_no_room_left_in_the_window_is_not_paid_for(workspace):
    """The other end of the bound. A head that fills the window on its own
    leaves nothing to answer with, and the request could only be refused -- so
    no call is made, the verdict is ``skipped`` rather than ``failed``, and the
    turn goes on with the transcript it had, which is what every verdict but
    ``changed`` does.

    Pruning off, so the head reaches the summary at the size that has no room:
    with it on, the prune shrinks the transcript first and there is room again.
    """
    provider = _CompactionScriptProvider([_tool_step(1, usage=_HUGE_USAGE), _answer()])
    agent = _agent(workspace, provider, cfg=_cfg(prune=False), window=1_000)

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        final, _used, _messages, _outcome = await agent._run_agent_loop(_initial(seed_rounds=6))
    finally:
        logger.remove(sink)

    assert final == "answer after compaction"
    assert provider.summary_calls == [], "a request the window cannot hold is not sent"
    no_room = [line for line in lines if "no room to answer in" in line]
    assert no_room and all("WARNING" in line for line in no_room), (
        "nothing was paid for, and the line can repeat per iteration -- so it is not an error"
    )
    assert not any(str(m.get("content", "")).startswith(compaction.SUMMARY_MARKER) for m in provider.main_calls[-1]), (
        "and nothing replaced the head"
    )


@pytest.mark.asyncio
async def test_a_brief_cut_at_its_budget_does_not_replace_the_head(workspace):
    """What the raised budget was buying, checked on the way back instead.

    A cut brief is non-empty, so it would be accepted and replace the head it
    stops halfway through -- worse than no brief, which leaves the elision path
    to drop the same items. ``finish_reason`` is what says so; the body cannot.
    """
    provider = _CompactionScriptProvider([_tool_step(1, usage=_HUGE_USAGE), _answer()], cut_summary=True)
    agent = _agent(workspace, provider, cfg=_cfg(prune=False))

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        final, _used, _messages, _outcome = await agent._run_agent_loop(_initial(seed_rounds=6))
    finally:
        logger.remove(sink)

    assert final == "answer after compaction"
    assert len(provider.summary_calls) == 1, "the call was made and paid for"
    cut = [line for line in lines if "was cut at its" in line]
    assert cut and all("ERROR" in line for line in cut)
    assert all("falls back to eliding" in line for line in cut), "the line says what happens next"
    assert not any(str(m.get("content", "")).startswith(compaction.SUMMARY_MARKER) for m in provider.main_calls[-1]), (
        "the head stands"
    )


@pytest.mark.asyncio
async def test_a_warm_cache_still_reads_as_a_full_window(workspace):
    """The trigger measures occupancy, not the bill. A provider that reports
    cache reads apart from ``prompt_tokens`` bills a fraction of a warm prompt
    while all of it still occupies the window; reading ``prompt_tokens`` alone
    let a live run send 124k tokens against a 120k window with the trigger
    seeing 4.7k. Same total as ``_HUGE_USAGE``, split across the cache."""
    cached = {
        "prompt_tokens": 90,
        "completion_tokens": 10,
        "cache_read_input_tokens": 4900,
        "prompt_tokens_include_cache": False,
    }
    provider = _CompactionScriptProvider([_tool_step(1, usage=cached), _answer()])
    agent = _agent(workspace, provider, cfg=_cfg())

    final, _used, _messages, _outcome = await agent._run_agent_loop(_initial(seed_rounds=6))

    assert final == "answer after compaction"
    assert provider.summary_calls, "a warm prompt that fills the window must still compact"
    assert any(str(m.get("content", "")).startswith(compaction.SUMMARY_MARKER) for m in provider.main_calls[-1])


@pytest.mark.asyncio
async def test_a_cache_inclusive_report_is_not_counted_twice(workspace):
    """The add-back is conditional on the provider's own declaration: where
    ``prompt_tokens`` already contains the cache reads (the default), adding
    them again would compact a window that is only half full."""
    inclusive = {"prompt_tokens": 400, "completion_tokens": 10, "cache_read_input_tokens": 380}
    provider = _CompactionScriptProvider([_tool_step(1, usage=inclusive), _answer()])
    agent = _agent(workspace, provider, cfg=_cfg())

    final, _used, _messages, _outcome = await agent._run_agent_loop(_initial(seed_rounds=6))

    assert final == "answer after compaction"
    assert provider.summary_calls == []
    assert not any(m.get("content") == _PLACEHOLDER for m in provider.main_calls[-1])


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
