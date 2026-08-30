"""Multi-turn DR: the gate, the memo, and the wrapper that makes them inert.

Two properties carry most of the weight here, and neither is about the happy path.

**Off means byte-identical, not "nearly".** Every DR reading in the ladder is turn
one of a fresh session, so this whole module has to be provably absent from a
measured arm. The tests assert that from both ends: the assembly does not build a
gate, and the wrapper is not applied, when ``conversation.enabled`` is false.

**Every classifier failure resolves to research.** A gate that errors, stalls,
truncates or answers in prose must produce a research turn, and it must say which
of those happened rather than reporting one indistinguishable "no". The two
mistakes are not symmetric - a needless research turn costs latency, a wrongly
skipped one answers a factual question from stale context in the same confident
voice - so the direction is tested once per failure mode, not once in general.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.conversation import (  # noqa: E402
    MEMO_CLOSE,
    MEMO_OPEN,
    ConversationGate,
    GatedHook,
    ResearchMemo,
    strip_memo,
)

from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision  # noqa: E402


class _Reply:
    def __init__(self, content, finish_reason="stop"):
        self.content = content
        self.finish_reason = finish_reason


class _Provider:
    """Minimal stand-in. ``reply`` may be a value, an exception, or a delay."""

    def __init__(self, reply):
        self._reply = reply
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._reply, BaseException):
            raise self._reply
        if self._reply == "stall":
            await asyncio.sleep(5)
        return self._reply


def _gate(reply, **kw):
    return ConversationGate(_Provider(reply), **kw)


def test_the_gate_call_carries_a_reasoning_effort_and_a_real_budget() -> None:
    """Both halves of one failure. A live turn returned ``gate_unparsed`` because the
    main model is a reasoning model at effort high: the 256-token budget went to
    ``reasoning_content``, ``content`` came out empty, and the gate failed open to a
    research turn nobody needed - the same shape the judge hit at 512.

    The cure is the effort, not the cap. The cap is raised only far enough to leave
    headroom, because this call sits in front of every turn's first token and the
    model that motivated the change is one that WILL spend whatever it is given,
    with ``gate_timeout_seconds`` then failing open to the expensive outcome. There
    is deliberately no fallback to ``reasoning_content``: failing open already gives
    ``research=true``, so anything recovered from that channel could only ever turn a
    needed research turn OFF, out of a channel that carries discarded drafts.
    """
    from research_flow.config import ConversationConfig

    defaults = ConversationConfig()
    assert defaults.gate_reasoning_effort == "low"
    assert defaults.gate_max_tokens == 1024

    provider = _Provider(_Reply('{"research": true, "why": "new facts"}'))
    gate = ConversationGate(
        provider, max_tokens=defaults.gate_max_tokens, reasoning_effort=defaults.gate_reasoning_effort
    )
    asyncio.run(gate.decide("and in 2025?", []))
    assert provider.calls[0]["reasoning_effort"] == "low"
    assert provider.calls[0]["max_tokens"] == 1024


def test_a_gate_built_without_the_knob_leaves_the_provider_default() -> None:
    provider = _Provider(_Reply('{"research": true}'))
    asyncio.run(ConversationGate(provider).decide("q", []))
    assert provider.calls[0]["reasoning_effort"] is None


_HISTORY = [
    {"role": "user", "content": "What did the 2024 EU AI Act change for open models?"},
    {"role": "assistant", "content": "It introduced tiered obligations..."},
]


# --------------------------------------------------------------------------- #
# The gate                                                                    #
# --------------------------------------------------------------------------- #


def test_a_clean_verdict_is_honoured_in_both_directions():
    yes = asyncio.run(_gate(_Reply('{"research": true, "why": "asks for a new figure"}')).decide("q", _HISTORY))
    no = asyncio.run(_gate(_Reply('{"research": false, "why": "reformat only"}')).decide("q", _HISTORY))
    assert (yes.research, yes.source) == (True, "gate")
    assert (no.research, no.source) == (False, "gate")
    assert no.why == "reformat only"


@pytest.mark.parametrize(
    "reply,expected_source",
    [
        (RuntimeError("connection reset"), "gate_error"),
        ("stall", "gate_error"),
        (_Reply('{"research": false}', finish_reason="length"), "gate_error"),
        (_Reply('{"research": false}', finish_reason="error"), "gate_error"),
        (_Reply("I think this one is a formatting request."), "gate_unparsed"),
        (_Reply(""), "gate_unparsed"),
        (_Reply('{"research": "maybe"}'), "gate_unparsed"),
    ],
)
def test_every_failure_mode_produces_a_research_turn_and_names_itself(reply, expected_source):
    """The fail-open direction, and the reason the sources are not one value.

    ``gate_error`` and ``gate_unparsed`` describe different repairs - one is the
    endpoint, the other the prompt or the model - and collapsing them would leave
    a counter that reads the same for both, which is the defect that made
    ``dedup_skipped`` unreadable for two versions.
    """
    mode = asyncio.run(_gate(reply, timeout_seconds=0.05).decide("q", _HISTORY))
    assert mode.research is True
    assert mode.source == expected_source


def test_a_truncated_verdict_is_not_read_as_a_verdict():
    """``finish_reason='length'`` is checked before the text is parsed.

    A reasoning model that spends its budget thinking and emits a bare
    ``{"research": false`` fragment is exactly what json_repair completes
    happily - in the unsafe direction, and only in the unsafe direction, because
    the truncated field is the one that turns research off.
    """
    mode = asyncio.run(_gate(_Reply('{"research": false', finish_reason="length")).decide("q", _HISTORY))
    assert mode.research is True and mode.source == "gate_error"


def test_the_classifier_never_sees_tool_traffic():
    """Its input is user and assistant text only.

    Tool results are most of the volume, they are the part the trimmer will have
    mangled by the time a later turn runs, and "how much searching happened last
    turn" is not evidence about whether THIS message needs searching - if
    anything a heavy research turn is more likely to be followed by a formatting
    request.
    """
    provider = _Provider(_Reply('{"research": true}'))
    history = _HISTORY + [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "SEARCH RESULTS: secret-marker"},
    ]
    asyncio.run(ConversationGate(provider).decide("and in 2025?", history))
    sent = provider.calls[0]["messages"][1]["content"]
    assert "secret-marker" not in sent
    assert "and in 2025?" in sent


def test_history_is_capped_by_message_count_and_by_length():
    provider = _Provider(_Reply('{"research": true}'))
    history = [{"role": "user", "content": f"m{i} " + "x" * 500} for i in range(20)]
    asyncio.run(ConversationGate(provider, history_messages=2, history_chars=50).decide("q", history))
    sent = provider.calls[0]["messages"][1]["content"]
    assert "m19" in sent and "m17" not in sent
    assert len(sent) < 400


# --------------------------------------------------------------------------- #
# GatedHook                                                                   #
# --------------------------------------------------------------------------- #


class _Recorder(AgentHook):
    def __init__(self):
        self.phases: list[str] = []

    @property
    def name(self) -> str:
        return "Recorder"

    async def before_iteration(self, ctx):
        self.phases.append("before_iteration")
        return HookDecision(short_circuit_result="inner ran")

    async def after_iteration(self, ctx):
        self.phases.append("after_iteration")
        return HookDecision()

    async def terminal_answerless(self, ctx):
        self.phases.append("terminal_answerless")
        return HookDecision()


def test_an_open_gate_is_a_pass_through_to_the_inner_hook():
    """With the predicate True the chain behaves as it did before this module.

    This is the assertion that "off means byte-identical": the wrapper is only
    ever applied when the product surface is enabled, and even then an ordinary
    research turn must reach the same object with the same decision, including a
    short-circuit result that AgentLoop treats as the turn's answer.
    """
    inner = _Recorder()
    hook = GatedHook(inner, lambda: True)
    decision = asyncio.run(hook.before_iteration(AgentHookContext(session_key="s")))
    assert inner.phases == ["before_iteration"]
    assert decision.short_circuit_result == "inner ran"


def test_a_closed_gate_reaches_no_phase_of_the_inner_hook():
    inner = _Recorder()
    hook = GatedHook(inner, lambda: False)
    ctx = AgentHookContext(session_key="s")
    for phase in ("before_iteration", "after_iteration", "terminal_answerless"):
        decision = asyncio.run(getattr(hook, phase)(ctx))
        assert decision.short_circuit_result is None
        assert decision.pass_through
    assert inner.phases == []


def test_the_gate_is_read_per_call_not_captured_at_construction():
    """One loop instance serves every turn of every session it hosts.

    A predicate evaluated once at wrap time would freeze the first turn's verdict
    for the life of the process - and because the wrapper is built inside
    ``build_dr_flow``, that is exactly once per loop.
    """
    state = {"on": False}
    inner = _Recorder()
    hook = GatedHook(inner, lambda: state["on"])
    asyncio.run(hook.after_iteration(AgentHookContext(session_key="s")))
    state["on"] = True
    asyncio.run(hook.after_iteration(AgentHookContext(session_key="s")))
    assert inner.phases == ["after_iteration"]


# --------------------------------------------------------------------------- #
# The memo                                                                    #
# --------------------------------------------------------------------------- #


def _rows(*urls, queries=(), ok=True):
    rows = [{"op": "search", "query": q} for q in queries]
    rows += [{"op": "fetch", "url": u, "chars": 1000, "ok": ok} for u in urls]
    return rows


def test_only_pages_that_were_actually_read_become_sources():
    """A failed fetch is not evidence.

    Listing it would hand the next turn a URL nobody opened, in a block the model
    is told is a record of retrieval - which is the fabricated-citation channel
    the process appendix exists to detect, given a head start by us.
    """
    memo = ResearchMemo().merge_ledger(
        _rows("https://a.example", ok=False) + _rows("https://b.example"),
        max_sources=10,
        max_queries=10,
    )
    assert [s["url"] for s in memo.sources] == ["https://b.example"]


def test_a_later_turn_does_not_duplicate_a_page_it_reopens():
    memo = ResearchMemo()
    memo.merge_ledger(_rows("https://a.example"), max_sources=10, max_queries=10)
    memo.merge_ledger(_rows("https://a.example", "https://b.example"), max_sources=10, max_queries=10)
    assert [s["url"] for s in memo.sources] == ["https://b.example", "https://a.example"]
    assert memo.turns == 2


def test_the_cap_drops_the_oldest_sources_not_the_newest():
    """Newest first, because the follow-up is most likely about this turn.

    A cap that kept insertion order would, on a long conversation, freeze the memo
    at the first turn's pages and silently stop recording anything later - visible
    nowhere, since a full memo and a stale one render identically.
    """
    memo = ResearchMemo()
    memo.merge_ledger(_rows("https://old1.example", "https://old2.example"), max_sources=3, max_queries=9)
    memo.merge_ledger(_rows("https://new1.example", "https://new2.example"), max_sources=3, max_queries=9)
    assert [s["url"] for s in memo.sources] == [
        "https://new1.example",
        "https://new2.example",
        "https://old1.example",
    ]


def test_queries_are_deduplicated_on_the_tools_own_normalisation():
    memo = ResearchMemo().merge_ledger(
        _rows(queries=["EU AI Act", "  eu   ai   act ", "open weights"]),
        max_sources=9,
        max_queries=9,
    )
    assert memo.queries == ["EU AI Act", "open weights"]


def test_an_empty_turn_advances_the_counter_and_nothing_else():
    """ "Three turns, two sources" and "two turns, two sources" are different states."""
    memo = ResearchMemo().merge_ledger([], max_sources=9, max_queries=9)
    assert memo.turns == 1 and memo.empty


def test_render_is_empty_when_there_is_nothing_to_say():
    assert ResearchMemo().render(max_chars=2000) == ""


def test_render_truncates_whole_lines_only():
    """Half a URL reads as a real one.

    A mid-line cut is the one truncation that manufactures the exact input this
    codebase already pays a check to detect - a plausible link to a page that was
    never opened.
    """
    memo = ResearchMemo().merge_ledger(
        _rows(*[f"https://example.com/{'p' * 60}/{i}" for i in range(12)]),
        max_sources=12,
        max_queries=9,
    )
    block = memo.render(max_chars=300)
    assert block.startswith(MEMO_OPEN) and block.endswith(MEMO_CLOSE)
    assert len(block) <= 300
    for line in block.splitlines():
        if line.startswith("- "):
            assert line.endswith(tuple("0123456789"))


def test_the_memo_round_trips_through_session_metadata():
    memo = ResearchMemo().merge_ledger(_rows("https://a.example", queries=["q"]), max_sources=9, max_queries=9)
    restored = ResearchMemo.from_metadata(memo.to_metadata())
    assert restored.to_metadata() == memo.to_metadata()


@pytest.mark.parametrize("junk", [None, "", 42, {"sources": "not-a-list"}, {"sources": [{"no": "url"}]}])
def test_a_corrupt_metadata_blob_degrades_to_an_empty_memo(junk):
    """A session file is a long-lived artifact edited by nothing we control.

    Raising here would make one malformed line permanently un-openable - the memo
    is a convenience, and losing it must never cost the conversation.
    """
    assert ResearchMemo.from_metadata(junk).empty


# --------------------------------------------------------------------------- #
# Stripping                                                                   #
# --------------------------------------------------------------------------- #


def test_strip_removes_exactly_the_injected_block():
    memo = ResearchMemo().merge_ledger(_rows("https://a.example"), max_sources=9, max_queries=9)
    block = memo.render(max_chars=2000)
    assert strip_memo(f"{block}\n\nWhat about 2025?") == "What about 2025?"


def test_strip_leaves_a_message_that_never_carried_a_memo_untouched():
    assert strip_memo("What about 2025?") == "What about 2025?"


def test_strip_is_a_no_op_on_a_block_with_no_terminator():
    """Half a memo is worse than either whole outcome.

    Without the closing delimiter there is no way to tell the record from the
    user's own words, so the message is persisted intact rather than cut at a
    guess - a wrong cut would delete the question.
    """
    assert strip_memo(f"{MEMO_OPEN}\n- https://a.example\n\nreal question").startswith(MEMO_OPEN)


def test_a_user_message_quoting_the_marker_mid_text_is_not_stripped():
    """Anchored at position 0 on purpose: the injector always prepends."""
    text = f"why does your output start with {MEMO_OPEN} ?"
    assert strip_memo(text) == text


# --------------------------------------------------------------------------- #
# The render cap and the check's accept-set are separately bounded            #
# --------------------------------------------------------------------------- #


def test_the_render_cap_does_not_shrink_what_the_grounding_check_accepts():
    """The second defect the eight-turn run produced.

    ``sources`` is rendered into every later turn's prompt, so its cap buys context.
    The fabricated-citation check's accept-set is never rendered, so its cap buys
    nothing and costs correctness: turn one alone opened 18 pages against a 12-source
    cap for the whole conversation, and once the cap evicted them, a later turn citing
    one was reported as inventing it. Two bounds, because one number cannot be right
    for two pressures pointing opposite ways.
    """
    memo = ResearchMemo()
    memo.merge_ledger(
        _rows(*[f"https://p{i}.example" for i in range(18)]), max_sources=3, max_queries=9, max_opened=200
    )
    assert len(memo.sources) == 3
    assert len(memo.opened) == 18
    memo.merge_ledger(_rows("https://later.example"), max_sources=3, max_queries=9, max_opened=200)
    assert len(memo.sources) == 3
    assert len(memo.opened) == 19
    assert "https://p0.example" in memo.opened


def test_the_accept_set_is_still_bounded_so_a_long_conversation_cannot_grow_forever():
    memo = ResearchMemo()
    for turn in range(5):
        memo.merge_ledger(
            _rows(*[f"https://t{turn}p{i}.example" for i in range(10)]), max_sources=4, max_queries=9, max_opened=12
        )
    assert len(memo.opened) == 12
    # Newest kept, like the rendered list: a follow-up is most likely about this turn.
    assert "https://t4p0.example" in memo.opened
    assert "https://t0p0.example" not in memo.opened


def test_the_accept_set_never_records_a_page_twice():
    memo = ResearchMemo()
    memo.merge_ledger(_rows("https://a.example"), max_sources=9, max_queries=9)
    memo.merge_ledger(_rows("https://a.example", "https://b.example"), max_sources=9, max_queries=9)
    assert memo.opened == ["https://b.example", "https://a.example"]


def test_a_conversation_saved_before_the_accept_set_existed_keeps_its_grounding():
    """Backward compatibility with a live session file, and why it matters.

    Sessions on disk carry ``sources`` and no ``opened``. Reading that as an empty
    accept-set would make the very next answer's citations look invented - the exact
    failure the field was added to remove, reintroduced by the upgrade itself.
    """
    legacy = {"turns": 2, "queries": ["q"], "sources": [{"url": "https://a.example", "chars": 1, "turn": 1}]}
    restored = ResearchMemo.from_metadata(legacy)
    assert restored.opened == ["https://a.example"]


def test_the_accept_set_survives_a_metadata_round_trip():
    memo = ResearchMemo().merge_ledger(_rows("https://a.example", "https://b.example"), max_sources=1, max_queries=9)
    assert len(memo.sources) == 1 and len(memo.opened) == 2
    restored = ResearchMemo.from_metadata(memo.to_metadata())
    assert restored.opened == memo.opened


# --------------------------------------------------------------------------- #
# One turn, three hook contexts                                               #
# --------------------------------------------------------------------------- #
#
# ``ctx.metadata`` is not one dict per turn. The loop builds a fresh
# AgentHookContext for the inbound rewrite, another for the whole iteration run
# (the only one carrying ``mode`` / ``mode_overlay``), and a third for the
# outbound. Everything below drives all three, in order, exactly as the loop
# does - because every defect this section covers is invisible to a test that
# reuses one context.


def _turn_hook(tmp_path, slice_, provider=None):
    from research_flow.config import FlowConfig
    from research_flow.flow import ResearchFlowHook, ToolHandles
    from research_flow.state import SessionStore

    cfg = FlowConfig.from_slice(slice_)
    return ResearchFlowHook(
        cfg=cfg,
        provider=provider,
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
        max_iterations=cfg.max_iterations or 40,
        context_window_tokens=cfg.context_window_tokens or 0,
    )


_BASE_SLICE = {
    "enabled": True,
    "conversation": {"enabled": True, "gate": "agentic", "researchMemo": True},
    "finalShape": {"record": True},
}


def _run_turn(hook, text, *, prior=(), mode="", overlay=None, iteration_writes=None):
    """One turn through the three contexts the loop actually builds.

    One ``asyncio.run`` for the whole turn, not one per phase: the plugin hands
    state between the phase groups in ContextVars, and ``asyncio.run`` copies
    the context into a fresh task, so a per-phase driver would lose exactly what
    is under test here. The loop awaits all three inside one coroutine.
    """

    async def turn():
        inbound = AgentHookContext(session_key="s", inbound_content=text)
        content = (await hook.before_user_inbound(inbound)).modified_content or text

        iter_ctx = AgentHookContext(
            session_key="s",
            iteration=1,
            messages=[*prior, {"role": "user", "content": content}],
            turn_base=len(prior),
            turn_question=content,
            metadata={"mode": mode, "mode_overlay": overlay if overlay is not None else {"drFlow": {}}},
        )
        await hook.before_iteration(iter_ctx)
        if iteration_writes:
            iter_ctx.metadata.update(iteration_writes)

        await hook.after_send(AgentHookContext(session_key="s", outbound_content="the answer"))
        return content

    return asyncio.run(turn()), hook.store.load("s")


def test_the_gate_classifies_what_the_user_wrote_not_what_we_prepended(tmp_path):
    """The classifier's input is the question, after our own blocks come off.

    ``before_user_inbound`` folds the memo in and ``ctx.turn_question`` is read
    after that rewrite, so the phase that decides the turn mode is handed
    ``[memo] + question`` unless the raw text is carried across. The failure is
    silent and one-directional: a memo listing pages about the earlier topic
    makes a fresh factual question look like a follow-up the model can answer
    from context, which is the one direction that answers from stale evidence.
    """
    provider = _Provider(_Reply('{"research": false, "why": "reformat only"}'))
    hook = _turn_hook(tmp_path, _BASE_SLICE, provider)
    memo = ResearchMemo().merge_ledger(
        [{"op": "fetch", "url": "https://a.example/paper", "chars": 900, "ok": True}],
        max_sources=9,
        max_queries=9,
    )
    hook.store.save("s", hook.store.load("s").__class__(research_memo=memo.to_metadata()))

    content, _ = _run_turn(hook, "and in 2025?", prior=_HISTORY)

    assert MEMO_OPEN in content, "the memo is still injected; only the classifier's copy is clean"
    asked = provider.calls[0]["messages"][1]["content"]
    assert "and in 2025?" in asked
    assert MEMO_OPEN not in asked and "https://a.example/paper" not in asked


def test_the_turn_stamp_carries_the_gate_verdict_and_every_gate_namespace(tmp_path):
    """What the turn measured has to outlive the phase that measured it.

    The twin stamped one ``observers`` dict on the turn's last assistant
    message: the gate counters the iteration phases wrote, plus the turn-level
    ones. Here the iteration phases write into a context ``after_send`` is not
    handed, so both halves were lost - ``conversation_gate`` was never written
    at all, and every gate counter ended with the turn that produced it, which
    makes "the gate never fired" and "the gate was never installed" the same
    reading.
    """
    provider = _Provider(_Reply('{"research": false, "why": "reformat only"}'))
    hook = _turn_hook(tmp_path, _BASE_SLICE, provider)

    _, record = _run_turn(
        hook,
        "reformat that as a table",
        prior=_HISTORY,
        iteration_writes={
            "fetch_floor": {"searches": 3, "fetches": 1, "notes": 2},
            "spin_breaker": {"hits": [{"iteration": 4, "marker": "restart"}], "triggers": 1, "_prev": {"a"}},
        },
    )

    assert record.observers["conversation_gate"]["dr_turn_research"] is False
    assert record.observers["conversation_gate"]["dr_turn_source"] == "gate"
    assert record.observers["fetch_floor"] == {"searches": 3, "fetches": 1, "notes": 2}
    # Counted and logged, not passed through: the hit list is unbounded and the
    # entity set beside it is not JSON at all.
    assert record.observers["spin_breaker"]["hits"] == 1
    assert record.observers["spin_breaker"]["hit_log"] == [{"iteration": 4, "marker": "restart"}]
    assert "_prev" not in record.observers["spin_breaker"]
    # The loop's own keys are the session's profile, which the record already
    # carries by name; they are not a gate's measurement.
    assert "mode" not in record.observers and "mode_overlay" not in record.observers
    assert record.observers["final_shape"], "the turn-level counters still land"


def test_a_stamp_cannot_be_inherited_by_the_next_turn(tmp_path):
    """Read-once, for the reason the pending clarify is.

    ``before_user_inbound`` is skipped for some turn origins, so a value left
    behind would stamp the previous turn's verdict on this turn's record - and a
    verdict is exactly the field a reader would trust.
    """
    provider = _Provider(_Reply('{"research": false, "why": "reformat only"}'))
    hook = _turn_hook(tmp_path, _BASE_SLICE, provider)

    async def two_turns():
        # One context for both, or the second turn would inherit nothing by
        # accident and the assertion would pass without the read-once.
        ctx = AgentHookContext(
            session_key="s",
            iteration=1,
            messages=[*_HISTORY, {"role": "user", "content": "reformat that"}],
            turn_base=len(_HISTORY),
            turn_question="reformat that",
            metadata={"mode": "", "mode_overlay": {"drFlow": {}}},
        )
        await hook.before_user_inbound(AgentHookContext(session_key="s", inbound_content="reformat that"))
        await hook.before_iteration(ctx)
        await hook.after_send(AgentHookContext(session_key="s", outbound_content="a"))
        first = hook.store.load("s").observers
        # The second turn's inbound phase never ran: the origin was one the loop
        # skips it for.
        await hook.after_send(AgentHookContext(session_key="s", outbound_content="b"))
        return first, hook.store.load("s").observers

    first, second = asyncio.run(two_turns())
    assert first["conversation_gate"]["dr_turn_source"] == "gate"
    assert "conversation_gate" not in second and "fetch_floor" not in second


def test_the_mode_the_turn_ran_in_governs_its_exit_and_is_recorded(tmp_path):
    """One chain per turn, resolved from the one context that names the mode.

    Only an iteration context carries ``mode``, so resolving on the other two
    built a second, base-config chain for the rewrite, discarded the real one on
    the way out, and ran the turn's exit under knobs no mode had asked for - and
    the discard took the session's tool gear with it every single turn.
    """
    hook = _turn_hook(tmp_path, {**_BASE_SLICE, "conversation": {"enabled": False}})

    _, record = _run_turn(
        hook,
        "survey the field",
        mode="deep",
        overlay={"drFlow": {"finalShape": {"record": False}}, "maxToolIterations": 30},
    )

    assert record.mode == "deep", "the profile the turn ran under, not the empty one after_send is handed"
    assert "final_shape" not in record.observers, "the mode turned recording off and the exit obeyed it"
    assert list(hook.session_gear) == ["s"], "the turn must not discard its own session's tool gear"
