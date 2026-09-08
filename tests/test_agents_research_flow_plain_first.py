"""The plain-first reply: what is withheld, what is accepted, and where escalation lands.

An experiment on a prior nobody can check against evidence, so the tests pin the parts
that keep it honest: the web tools are gone for exactly one call, a plain answer never
ships without the judge (when there is one), every failure direction is research, and
an escalated draft survives as a hypothesis rather than vanishing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.flow import ToolHandles, build_chain  # noqa: E402
from research_flow.gates.ask_user import set_first_turn  # noqa: E402
from research_flow.gates.plain_first import (  # noqa: E402
    FIRST_REPLY_NOTE,
    PLAIN_TURN_SOURCE,
    REQUEST_RESEARCH_TOOL,
    RESEARCH_MARKER,
    PlainFirstGate,
    PlainScopedReview,
    PlainTurnGate,
    is_plain_turn,
    set_plain_turn,
)
from research_flow.state import SessionStore  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext, HookDecision  # noqa: E402

TOOLS = [{"function": {"name": n}} for n in ("web_search", "web_fetch", "ask_user", "read_file")]
QUESTION = [{"role": "user", "content": "At what temperature does water boil at sea level?"}]


class _Call:
    def __init__(self, name: str, arguments=None) -> None:
        self.name = name
        self.arguments = arguments


class _Response:
    def __init__(self, content: str, has_tool_calls: bool = False, tool_calls=None) -> None:
        self.content = content
        self.has_tool_calls = has_tool_calls or bool(tool_calls)
        self.tool_calls = list(tool_calls or [])
        self.finish_reason = "stop"
        self.reasoning_content = None


class _Judge:
    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kw):
        self.calls.append(kw)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, _Response) else _Response(reply)


def _ctx(meta: dict, *, iteration: int = 1, response=None, messages=None) -> AgentHookContext:
    return AgentHookContext(
        session_key="t",
        iteration=iteration,
        messages=list(messages or QUESTION),
        tools=list(TOOLS),
        response=response,
        metadata=meta,
        turn_base=0,
    )


def _before(gate, meta, **kw) -> HookDecision:
    return asyncio.run(gate.before_iteration(_ctx(meta, **kw)))


def _after(gate, meta, response) -> HookDecision:
    return asyncio.run(gate.after_iteration(_ctx(meta, response=response)))


OK = '{"plain_ok": true, "sound": true, "reason": "settled", "issues": []}'


def setup_function(_):
    set_first_turn(True)
    set_plain_turn(False)


def teardown_function(_):
    set_first_turn(False)
    set_plain_turn(False)


def test_the_first_call_of_the_first_turn_swaps_the_web_tools_for_the_request():
    meta: dict = {}
    d = _before(PlainFirstGate(_Judge()), meta)
    assert [t["function"]["name"] for t in d.modified_tools] == ["ask_user", "read_file", REQUEST_RESEARCH_TOOL]
    assert d.append_note == FIRST_REPLY_NOTE and meta["plain_first"]["withheld"] is True


def test_the_request_tool_escalates_before_anything_executes():
    """The proposal is popped whole (rollback), so no tool result and no dangling call;
    the reason the model gave travels into the note; the re-sample is not withheld."""
    gate = PlainFirstGate(_Judge())
    meta: dict = {}
    _before(gate, meta)
    resp = _Response("", tool_calls=[_Call(REQUEST_RESEARCH_TOOL, '{"reason": "depends on the current rate"}')])
    d = asyncio.run(gate.before_execute_tools(_ctx(meta, response=resp)))
    assert d.rollback is True and d.short_circuit_result is None
    assert [m["role"] for m in d.rollback_inject] == ["user"]
    assert "depends on the current rate" in d.rollback_inject[0]["content"]
    assert meta["plain_first"]["outcome"] == "escalated_tool" and meta["plain_first"]["mixed_call"] is False
    assert _before(gate, meta).modified_tools is None
    # after_iteration never runs on a rolled-back proposal; if it did, it must not re-decide.
    assert asyncio.run(gate.after_iteration(_ctx(meta, response=resp))).rollback is False


def test_a_late_request_research_call_is_answered_once_and_then_left_to_the_registry():
    """The first-reply note names the tool and persists in history, so a model may call
    it again after the gate has decided and the tool is gone. The first such call is
    popped with a note saying the web tools are there; a second goes through to the
    registry's own unknown-tool error, so the rollback budget is not spent on it."""
    gate = PlainFirstGate(_Judge())
    meta: dict = {}
    _before(gate, meta)
    meta["plain_first"]["outcome"] = "escalated_tool"
    late = _Response("", tool_calls=[_Call(REQUEST_RESEARCH_TOOL, '{"reason": "still unsure"}')])
    first = asyncio.run(gate.before_execute_tools(_ctx(meta, iteration=3, response=late)))
    assert first.rollback is True and "web tools are available now" in first.rollback_inject[0]["content"]
    assert meta["plain_first"]["outcome"] == "escalated_tool", "the decision already taken is not re-decided"
    second = asyncio.run(gate.before_execute_tools(_ctx(meta, iteration=4, response=late)))
    assert second.rollback is False and second.pass_through is True


def test_other_tool_calls_on_the_first_reply_pass_the_proposal_hook_untouched():
    gate = PlainFirstGate(_Judge())
    meta: dict = {}
    _before(gate, meta)
    resp = _Response("", tool_calls=[_Call("ask_user", {})])
    d = asyncio.run(gate.before_execute_tools(_ctx(meta, response=resp)))
    assert d.rollback is False and d.pass_through is True and "outcome" not in meta["plain_first"]


def test_later_iterations_and_later_turns_are_untouched():
    gate = PlainFirstGate(_Judge())
    assert _before(gate, {}, iteration=2).modified_tools is None
    set_first_turn(False)
    assert _before(gate, {}, iteration=1).modified_tools is None


def test_the_marker_escalates_and_the_tools_come_back_on_the_resample():
    """The marker response is popped (rollback) and a user note says research is on;
    the re-sampled iteration 1 must NOT withhold again, or the turn loops."""
    gate = PlainFirstGate(_Judge())
    meta: dict = {}
    _before(gate, meta)
    d = _after(gate, meta, _Response(f"  {RESEARCH_MARKER} "))
    assert d.rollback is True
    assert [m["role"] for m in d.rollback_inject] == ["user"] and "Research is required" in d.rollback_inject[0][
        "content"
    ]
    assert meta["plain_first"]["outcome"] == "escalated_marker"
    again = _before(gate, meta)
    assert again.modified_tools is None and again.append_note is None


def test_a_plain_answer_the_judge_accepts_stands():
    judge = _Judge('{"plain_ok": true, "sound": true, "reason": "boiling point is a settled constant", "issues": []}')
    gate = PlainFirstGate(judge)
    meta: dict = {}
    _before(gate, meta)
    d = _after(gate, meta, _Response("100 degrees Celsius at one atmosphere. No sources were consulted."))
    assert d.rollback is False and d.rollback_inject is None
    assert meta["plain_first"]["outcome"] == "accepted" and meta["plain_first"]["judge_reason"].startswith(
        "boiling point"
    )
    assert (
        "Question:" in judge.calls[0]["messages"][1]["content"]
        and "Draft answer" in judge.calls[0]["messages"][1]["content"]
    )


def test_a_plain_answer_the_judge_refuses_becomes_a_hypothesis():
    """Rejected drafts are not thrown away: the model researches its own claim."""
    gate = PlainFirstGate(_Judge('{"plain_ok": false, "sound": true, "reason": "depends on the current rate"}'))
    meta: dict = {}
    _before(gate, meta)
    d = _after(gate, meta, _Response("The Fed funds rate is 5.25-5.50%."))
    assert d.rollback is True
    assert [m["role"] for m in d.rollback_inject] == ["assistant", "user"]
    assert d.rollback_inject[0]["content"] == "The Fed funds rate is 5.25-5.50%."
    assert (
        "depends on the current rate" in d.rollback_inject[1]["content"]
        and "hypothesis" in d.rollback_inject[1]["content"]
    )
    assert meta["plain_first"]["outcome"] == "escalated_judge"


def test_every_judge_failure_escalates_to_research():
    # A reply that came back without the keys is asked once more; a failed call or
    # an empty reply is not, since neither is the model misreading the schema. The
    # re-ask is recorded whether or not the second call produced a verdict, so the
    # ledger counts the failed re-asks too.
    cases = [
        (_Judge(RuntimeError("boom")), 1, False),
        (_Judge(_Response("")), 1, False),
        (_Judge("not json", "still not json"), 2, True),
        (_Judge('{"plain_ok": true, "reason": "no sound key"}', '{"correct": true}'), 2, True),
        (_Judge('{"correct": true}', RuntimeError("boom")), 2, True),
    ]
    for judge, calls, reasked in cases:
        gate = PlainFirstGate(judge, judge_timeout_seconds=5)
        meta: dict = {}
        _before(gate, meta)
        d = _after(gate, meta, _Response("Some confident answer."))
        assert d.rollback is True and meta["plain_first"]["outcome"] == "escalated_judge_failed", judge.replies
        assert len(judge.calls) == calls, judge.calls
        assert meta["plain_first"]["judge_reasked"] is reasked, judge.calls


def test_a_reply_with_the_wrong_keys_is_asked_once_more_for_the_schema():
    # Reproduced on deepseek-v4-flash at low effort: about one call in ten answers
    # {"correct": true, ...} after almost no reasoning. One re-ask recovers the verdict.
    first = '{"correct": true, "reason": "the founding year is right", "issues": []}'
    judge = _Judge(first, OK)
    gate = PlainFirstGate(judge)
    meta: dict = {}
    _before(gate, meta)
    d = _after(gate, meta, _Response("The Summer Palace was founded in 1750."))
    assert d.rollback is False and meta["plain_first"]["outcome"] == "accepted"
    assert meta["plain_first"]["judge_reasked"] is True and meta["plain_first"]["judge_sound"] is True
    assert len(judge.calls) == 2 and judge.calls[1]["reasoning_effort"] == "low"
    follow_up = judge.calls[1]["messages"]
    assert follow_up[:2] == judge.calls[0]["messages"]
    assert follow_up[2] == {"role": "assistant", "content": first}
    assert follow_up[3]["role"] == "user" and "plain_ok, sound" in follow_up[3]["content"]

    clean = _Judge(OK)
    meta = {}
    _before(PlainFirstGate(clean), meta)
    _after(PlainFirstGate(clean), meta, _Response("Canberra."))
    assert meta["plain_first"]["judge_reasked"] is False and len(clean.calls) == 1


def test_without_a_judge_a_plain_answer_is_accepted_and_says_so():
    gate = PlainFirstGate(None)
    meta: dict = {}
    _before(gate, meta)
    d = _after(gate, meta, _Response("Canberra."))
    assert d.rollback is False and meta["plain_first"]["outcome"] == "accepted_unjudged"


def test_a_non_web_tool_call_on_the_first_reply_is_neither_accepted_nor_escalated():
    gate = PlainFirstGate(_Judge())
    meta: dict = {}
    _before(gate, meta)
    d = _after(gate, meta, _Response("", has_tool_calls=True))
    assert d.rollback is False and meta["plain_first"]["outcome"] == "bypassed_tool_call"
    assert _before(gate, meta, iteration=2).modified_tools is None


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


class _StubProvider:
    async def chat_with_retry(self, **kwargs):
        raise AssertionError("assembly must not call the provider")


def _inner(hook):
    while hasattr(hook, "inner"):
        hook = hook.inner
    return hook


def test_off_by_default_and_first_in_the_chain_when_on(tmp_path):
    kw = dict(max_iterations=40, context_window_tokens=65536, tools=ToolHandles(), store=SessionStore(tmp_path))
    off = build_chain(FlowConfig(enabled=True), _StubProvider(), **kw)
    assert not any(isinstance(_inner(o), PlainFirstGate) for o in off)

    cfg = FlowConfig(enabled=True)
    cfg.plain_first.enabled = True
    on = build_chain(cfg, _StubProvider(), **kw)
    kinds = [type(_inner(o)) for o in on]
    assert kinds[1] is PlainFirstGate, "right after the TurnFrame, ahead of every terminal gate"
    assert _inner(on[1])._provider is not None, "the judge is on by default"

    cfg.plain_first.judge = False
    unjudged = build_chain(cfg, _StubProvider(), **kw)
    assert _inner(unjudged[1])._provider is None


def test_the_knob_is_reachable_from_an_overlay():
    flow = FlowConfig(enabled=True).with_overlay(
        {"conversation": {"enabled": True}, "plainFirst": {"enabled": True, "judgeTimeoutSeconds": 30}}
    )
    assert flow.plain_first.enabled is True and flow.plain_first.judge_timeout_seconds == 30


# --------------------------------------------------------------------------
# The merged judge: class and soundness in one call
# --------------------------------------------------------------------------


def test_common_knowledge_wrongly_stated_is_escalated_with_the_issues():
    judge = _Judge(
        '{"plain_ok": true, "sound": false, "reason": "settled topic", "issues": ["water boils at 90 C is wrong"]}'
    )
    gate = PlainFirstGate(judge)
    meta: dict = {}
    _before(gate, meta)
    d = _after(gate, meta, _Response("Water boils at 90 C at sea level."))
    assert d.rollback is True and [m["role"] for m in d.rollback_inject] == ["assistant", "user"]
    assert "90 C is wrong" in d.rollback_inject[1]["content"] and "hypothesis" in d.rollback_inject[1]["content"]
    assert meta["plain_first"]["outcome"] == "escalated_review"
    assert meta["plain_first"]["judge_plain_ok"] is True and meta["plain_first"]["judge_sound"] is False


def test_the_judge_is_asked_both_questions_in_one_call():
    judge = _Judge(OK)
    gate = PlainFirstGate(judge)
    meta: dict = {}
    _before(gate, meta)
    _after(gate, meta, _Response("Canberra."))
    assert len(judge.calls) == 1
    system = judge.calls[0]["messages"][0]["content"]
    assert "plain_ok" in system and "sound" in system and "do not fault the draft" in system
    assert meta["plain_first"]["outcome"] == "accepted" and meta["plain_first"]["judge_sound"] is True


def test_the_judge_runs_at_low_effort_unless_told_otherwise():
    # Measured on FreshQA drafts: at the provider default the judge ran 20-500s per
    # call and failed toward research on a quarter of the plain drafts; at low
    # effort the same drafts were judged the same with the tail cut.
    judge = _Judge(OK)
    meta: dict = {}
    _before(PlainFirstGate(judge), meta)
    _after(PlainFirstGate(judge), meta, _Response("Canberra."))
    assert judge.calls[0]["reasoning_effort"] == "low"

    quiet = _Judge(OK)
    meta = {}
    gate = PlainFirstGate(quiet, judge_reasoning_effort=None)
    _before(gate, meta)
    _after(gate, meta, _Response("Canberra."))
    assert "reasoning_effort" not in quiet.calls[0]


# --------------------------------------------------------------------------
# Plain turns after the first
# --------------------------------------------------------------------------


def test_a_gate_classed_plain_turn_is_withheld_like_a_first_turn():
    set_first_turn(False)
    set_plain_turn(True)
    meta: dict = {}
    d = _before(PlainFirstGate(_Judge()), meta)
    assert d.modified_tools is not None and REQUEST_RESEARCH_TOOL in [t["function"]["name"] for t in d.modified_tools]
    assert meta["plain_first"]["entry"] == "plain_turn"
    set_plain_turn(False)
    assert _before(PlainFirstGate(_Judge()), {}).modified_tools is None, "a context or research turn is untouched"


def _plain_gate(reply):
    class _P:
        async def chat_with_retry(self, **kw):
            return _Response(reply)

    return PlainTurnGate(_P(), timeout_seconds=5)


def test_the_plain_turn_gate_reads_three_verdicts_and_the_legacy_shape():
    plain = asyncio.run(_plain_gate('{"turn": "plain", "why": "new settled topic"}').decide("q", []))
    assert (plain.research, plain.source, plain.why) == (True, PLAIN_TURN_SOURCE, "new settled topic")
    ctx = asyncio.run(_plain_gate('{"turn": "context", "why": "reformat"}').decide("q", []))
    assert (ctx.research, ctx.source) == (False, "gate")
    res = asyncio.run(_plain_gate('{"turn": "research"}').decide("q", []))
    assert (res.research, res.source) == (True, "gate")
    legacy = asyncio.run(_plain_gate('{"research": false, "why": "old shape"}').decide("q", []))
    assert (legacy.research, legacy.source) == (False, "gate")
    junk = asyncio.run(_plain_gate('{"turn": "sometimes"}').decide("q", []))
    assert (junk.research, junk.source) == (True, "gate_unparsed"), "an unknown verdict is a research turn"


def test_the_plain_turn_gate_asks_a_three_way_question_and_the_parent_does_not():
    from research_flow.gates.conversation import ConversationGate

    class _P:
        async def chat_with_retry(self, **kw):
            _P.system = kw["messages"][0]["content"]
            return _Response('{"turn": "research"}')

    asyncio.run(PlainTurnGate(_P()).decide("q", []))
    assert '"plain"' in _P.system
    asyncio.run(ConversationGate(_P()).decide("q", []))
    assert '"plain"' not in _P.system and '"research": true|false' in _P.system


def test_the_turn_frame_hands_a_plain_verdict_to_the_plain_first_gate(tmp_path):
    """Second turn, new common-knowledge topic: the gate says plain, the frame sets the
    flag, and the plain-first gate one hook later withholds the web tools."""
    from research_flow.flow import ResearchFlowHook

    class _P:
        async def chat_with_retry(self, **kw):
            return _Response('{"turn": "plain", "why": "new settled topic"}')

    cfg = FlowConfig(enabled=True).with_overlay(
        {"conversation": {"enabled": True, "gate": "agentic"}, "plainFirst": {"enabled": True}}
    )
    hook = ResearchFlowHook(
        cfg=cfg,
        provider=_P(),
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
        max_iterations=40,
        context_window_tokens=0,
    )
    prior = [
        {"role": "user", "content": "Will the Fed cut in Q4?"},
        {"role": "assistant", "content": "Unlikely, because..."},
    ]

    async def turn():
        meta: dict = {}
        await hook.before_user_inbound(
            AgentHookContext(session_key="s", inbound_content="What is the Pythagorean theorem?", metadata=meta)
        )
        meta.update({"mode": "", "mode_overlay": {"drFlow": {}}})
        ctx = AgentHookContext(
            session_key="s",
            iteration=1,
            messages=[*prior, {"role": "user", "content": "What is the Pythagorean theorem?"}],
            tools=list(TOOLS),
            turn_base=len(prior),
            turn_question="What is the Pythagorean theorem?",
            metadata=meta,
        )
        d = await hook.before_iteration(ctx)
        return d, meta, is_plain_turn()

    d, meta, plain = asyncio.run(turn())
    assert plain is True and meta["dr_turn_mode"].source == PLAIN_TURN_SOURCE and meta["dr_turn_mode"].research is True
    names = [t["function"]["name"] for t in d.modified_tools]
    assert "web_search" not in names and REQUEST_RESEARCH_TOOL in names
    assert meta["plain_first"]["entry"] == "plain_turn"


def test_without_plain_first_the_frame_builds_the_two_way_gate(tmp_path):
    from research_flow.gates.conversation import ConversationGate

    kw = dict(max_iterations=40, context_window_tokens=65536, tools=ToolHandles(), store=SessionStore(tmp_path))
    cfg = FlowConfig(enabled=True).with_overlay({"conversation": {"enabled": True, "gate": "agentic"}})
    frame = build_chain(cfg, _StubProvider(), **kw)[0]
    assert type(frame._gate) is ConversationGate
    cfg.plain_first.enabled = True
    frame = build_chain(cfg, _StubProvider(), **kw)[0]
    assert type(frame._gate) is PlainTurnGate


# --------------------------------------------------------------------------
# Scoped review
# --------------------------------------------------------------------------


class _Inner:
    """Stands in for the evidence reviewer: records whether it was consulted."""

    name = "DraftReviewerGate"

    def __init__(self) -> None:
        self.seen = 0

    async def after_iteration(self, ctx):
        self.seen += 1
        return HookDecision(notes=["inner reviewed"])


PAGE_OK = {"role": "tool", "name": "web_fetch", "content": '{"url": "https://x", "status": 200, "text": "body"}'}
PAGE_BAD = {"role": "tool", "name": "web_fetch", "content": '{"url": "https://x", "error": "timeout"}'}


def _accepted_meta() -> dict:
    return {"plain_first": {"withheld": True, "outcome": "accepted", "judge_sound": True}}


def _scoped_after(wrapper, meta, response, messages=None) -> HookDecision:
    return asyncio.run(wrapper.after_iteration(_ctx(meta, response=response, messages=messages)))


def test_a_judged_plain_draft_ships_without_consulting_the_reviewer():
    inner = _Inner()
    meta = _accepted_meta()
    d = _scoped_after(PlainScopedReview(inner), meta, _Response("Canberra."))
    assert inner.seen == 0 and d.rollback is False
    assert meta["plain_first"]["review"] == "skipped_judged"
    assert meta["verify_gate"]["scoped"] == "plain" and meta["verify_gate"]["reviews"] == 0


def test_the_reviewer_is_consulted_whenever_the_predicate_fails():
    for meta, messages, response in (
        ({"plain_first": {"withheld": True, "outcome": "escalated_judge"}}, None, _Response("draft")),
        ({"plain_first": {"withheld": True, "outcome": "escalated_review"}}, None, _Response("draft")),
        ({}, None, _Response("draft")),
        (_accepted_meta(), [*QUESTION, PAGE_OK], _Response("draft")),
        (_accepted_meta(), None, _Response("", has_tool_calls=True)),
    ):
        inner = _Inner()
        _scoped_after(PlainScopedReview(inner), meta, response, messages)
        assert inner.seen == 1, (meta, messages)
        assert "scoped" not in meta.get("verify_gate", {})


def test_a_failed_fetch_is_not_evidence_so_the_scope_still_applies():
    inner = _Inner()
    _scoped_after(PlainScopedReview(inner), _accepted_meta(), _Response("draft"), [*QUESTION, PAGE_BAD])
    assert inner.seen == 0


def test_the_reviewer_is_wrapped_only_when_plain_first_is_on_and_review_is_skip(tmp_path):
    from research_flow.gates.verify import DraftReviewerGate

    kw = dict(max_iterations=40, context_window_tokens=65536, tools=ToolHandles(), store=SessionStore(tmp_path))

    def reviewer_of(cfg):
        for o in build_chain(cfg, _StubProvider(), **kw):
            if isinstance(_inner(o), DraftReviewerGate):
                return o
        raise AssertionError("no reviewer in the chain")

    off = FlowConfig(enabled=True)
    assert not any(isinstance(h, PlainScopedReview) for h in _wrappers(reviewer_of(off)))

    on = FlowConfig(enabled=True)
    on.plain_first.enabled = True
    assert sum(isinstance(h, PlainScopedReview) for h in _wrappers(reviewer_of(on))) == 1, "skip by default"

    on.plain_first.review = "full"
    assert not any(isinstance(h, PlainScopedReview) for h in _wrappers(reviewer_of(on)))


def _wrappers(hook):
    out = [hook]
    while hasattr(hook, "inner"):
        hook = hook.inner
        out.append(hook)
    return out


def test_the_review_knob_is_reachable_and_validated():
    import pytest

    flow = FlowConfig(enabled=True).with_overlay(
        {"conversation": {"enabled": True}, "plainFirst": {"enabled": True, "review": "full"}}
    )
    assert flow.plain_first.review == "full"
    assert flow.plain_first.judge_reasoning_effort == "low"
    quiet = FlowConfig(enabled=True).with_overlay({"plainFirst": {"judgeReasoningEffort": None}})
    assert quiet.plain_first.judge_reasoning_effort is None
    with pytest.raises(Exception, match="judge"):
        FlowConfig(enabled=True).with_overlay(
            {"conversation": {"enabled": True}, "plainFirst": {"enabled": True, "judge": False}}
        )
    # Off with the judge off is a legal, inert shape.
    assert FlowConfig(enabled=True).with_overlay({"plainFirst": {"judge": False}}).plain_first.enabled is False
    with pytest.raises(Exception):
        FlowConfig(enabled=True).with_overlay({"plainFirst": {"review": "light"}})


def test_plain_first_refuses_to_load_without_the_conversation_frame():
    """The gate fires only on a turn ``TurnFrame`` has marked first or plain, and the
    frame marks nothing when the conversation surface is off. The pair would install a
    gate that never produces an outcome; the config refuses it instead."""
    import pytest

    with pytest.raises(Exception, match="conversation"):
        FlowConfig(enabled=True).with_overlay({"plainFirst": {"enabled": True}})
    # A disabled flow describes a distribution nothing will run, so it still loads.
    off = FlowConfig(enabled=False).with_overlay({"plainFirst": {"enabled": True}})
    assert off.plain_first.enabled is True and off.conversation.enabled is False


def test_plain_first_and_the_evidence_floor_refuse_to_load_together():
    """An accepted plain answer rests on zero pages; the floor would bounce it twice
    and release it, so the pair can never ship a plain answer."""
    import pytest

    with pytest.raises(Exception, match="evidenceFloor"):
        FlowConfig(enabled=True).with_overlay(
            {"conversation": {"enabled": True}, "plainFirst": {"enabled": True}, "evidenceFloor": {"enabled": True}}
        )
