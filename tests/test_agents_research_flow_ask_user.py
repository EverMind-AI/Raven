"""dr@3.4-askuser: the clarify round at the turn boundary - gate, wrapper, tool.

The hook/tool half of the fork's ``test_agent_flow_ask_user.py``: everything that
drives ``AskUserGate``, ``ClarifyExemptHook``, ``is_prose_clarify`` or
``DRAskUserTool``. The pure text surface it stands on - ``own_text``,
``scaffold_language``, the two renderers, ``is_reply_to``, ``PendingClarify``'s
metadata round trip and the ContextVar accessors - is asserted in
``test_agents_research_ask_user_text.py``, so the two files can move apart
without either losing its subject.

[port] Three of the fork's surfaces have no counterpart on this build and their
assertions are dropped rather than weakened:

* the ``build_dr_flow`` assembly. The plugin's chain is ``research_flow.flow.
  build_chain``, which takes tool handles and a session store and prepends its
  own ``TurnFrame`` - so the fork's observer-name lists are not the same fact,
  and the registration locks they stood for are pinned end to end by
  ``test_agents_research_launcher.py`` instead;
* the segment shas and the identity rewrite, which belong to the prompt surface
  (``research_flow.prompts``) rather than to a gate. The clause is still read
  here wherever the point is that it and the tool description agree about one
  call - two prompt surfaces disagreeing makes a reading unattributable;
* ``terminal_state``: the trunk kernel has no trajectory export, so the gate's
  own namespace on ``ctx.metadata`` is the only observable left.

[port] The handoff and brief scaffolding is English source text rendered through
``raven.i18n.t_in``, so a test that read a Chinese sentence off the render now
asks the catalog for it. Chinese INPUT is still what exercises the language
switch and is spelled with escapes, the way ``ask_user.py`` spells its own CJK
range.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import AskUserConfig  # noqa: E402
from research_flow.gates.ask_user import (  # noqa: E402
    _HANDOFF_LEAD,
    AskUserGate,
    ClarifyExemptHook,
    PendingClarify,
    is_prose_clarify,
    parse_ask_user_args,
    render_handoff,
    set_chain_round,
    set_clarify_verdict,
    set_first_turn,
    set_turn_brief,
    take_pending_clarify,
)
from research_flow.gates.report_shape import render_reminder  # noqa: E402
from research_flow.prompts import render_parts  # noqa: E402
from research_flow.tools.ask_user import (  # noqa: E402
    _FALLBACK_NO_QUESTIONS,
    _FALLBACK_NOT_DELIVERED,
    DRAskUserTool,
)

from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision  # noqa: E402
from raven.i18n import t_in  # noqa: E402


def _seg(**kw) -> str:
    """The prompt text one config state renders, as the fork's segment built it.

    [port] ``DRModeSegmentBuilder`` became ``research_flow.prompts.render_parts``,
    which returns the identity and the contract rather than a ``Segment``; the
    join is the builder's own (the language directive between them is empty on
    every state read here).
    """
    identity, contract = render_parts(**kw)
    return f"{identity}\n\n{contract}"


# ---------------------------------------------------------------------------
# The tool definition - prompt text, and a live fallback path
# ---------------------------------------------------------------------------


def test_the_dr_tool_does_not_block() -> None:
    """``ToolRegistry.execute`` skips its timeout for blocking tools. Nothing here
    waits on a human: the gate turns the call into the turn's reply."""
    assert DRAskUserTool().blocking_interaction is False


def test_the_blocking_tool_is_untouched() -> None:
    """The gateway and the TUI RPC layer still drive ``AskUserTool``; this feature
    does not get to change its semantics. One name, two classes, two transports."""
    from raven.agent.tools.ask_user import AskUserTool

    assert AskUserTool.blocking_interaction is True
    assert AskUserTool().name == "ask_user" == DRAskUserTool().name


def test_the_schema_carries_no_top_level_required() -> None:
    """``"questions": []`` satisfies a JSON-Schema ``required``, so it cannot carry
    the guardrail - and a guardrail in two places is a guardrail in neither.
    ``AskUserGate`` owns it (phase 2)."""
    params = DRAskUserTool().parameters
    assert "required" not in params
    assert params["properties"]["questions"]["items"]["required"] == ["question"]


def test_the_schema_drops_the_fields_nothing_renders() -> None:
    """The blocking tool offers ``multiple`` and ``custom``. Rendering here is
    markdown text, so both would be promises no renderer keeps."""
    item = DRAskUserTool().parameters["properties"]["questions"]["items"]["properties"]
    assert set(item) == {"question", "options"}


def test_the_outline_parameter_follows_its_own_switch() -> None:
    assert "outline" in DRAskUserTool(outline=True).parameters["properties"]
    assert "outline" not in DRAskUserTool(outline=False).parameters["properties"]
    assert "outline" not in DRAskUserTool(outline=False).description


def test_the_description_avoids_the_blocking_tools_two_lies() -> None:
    """ "wait for their answer" is false under a handoff, and "gather a preference"
    is the personalizer's job - the sentence that makes the two clarify paths
    indistinguishable in a trajectory."""
    described = DRAskUserTool().description.lower()
    assert "wait for their answer" not in described
    assert "preference" not in described
    assert "end your turn" in described


def test_the_rendered_counts_cannot_go_negative() -> None:
    """Both counts are rendered INTO prompt text, so an unvalidated negative ships
    "Up to -3 questions" to the model. Clamped in the tool rather than constrained
    in the config: this file's config has two ``ge=`` uses in three thousand lines,
    and the value that matters is the one that reaches the schema."""
    tool = DRAskUserTool(max_questions=-3, max_outline_items=0)
    assert "-3" not in tool.parameters["properties"]["questions"]["description"]
    assert "Up to 1 questions" in tool.parameters["properties"]["questions"]["description"]
    assert "Up to 1 steps" in tool.parameters["properties"]["outline"]["description"]


def test_the_fallback_is_reachable_and_pushes_back_to_research() -> None:
    """``CompositeHook`` halts a phase only on ``short_circuit_result`` or
    ``rollback``. The outline-only guardrail returns neither, so the loop proceeds
    and ``execute`` runs: this string is read by the model. It has to send it back
    to work rather than read as a refusal."""
    result = asyncio.run(DRAskUserTool().execute(outline=[{"goal": "g", "evidence": "e"}]))
    assert result == _FALLBACK_NO_QUESTIONS
    assert "continue researching" in result
    assert "at least one question" in result


def test_a_call_that_asked_real_questions_is_not_told_it_asked_none() -> None:
    """Two reasons reach ``execute`` and only one is the model's doing: the
    outline-only guardrail, and no gate having handed the call off (every call, in
    phase 1). Telling a model that asked three good questions that "an outline
    alone is not one" is a false statement about its own last action - the class of
    prompt error that teaches the wrong lesson and cannot be seen in a sha."""
    result = asyncio.run(DRAskUserTool().execute(questions=[{"question": "which year?"}]))
    assert result == _FALLBACK_NOT_DELIVERED
    assert "at least one question" not in result
    assert "continue researching" in result


def test_execute_survives_a_call_with_no_questions_key_at_all() -> None:
    """No top-level ``required`` means ``ToolRegistry.execute`` validates and then
    calls ``execute(**params)`` without ``questions`` - the commonest shape on this
    path. A required positional would raise TypeError inside the registry."""
    tool = DRAskUserTool()
    assert tool.validate_params({"outline": [{"goal": "g", "evidence": "e"}]}) == []
    assert asyncio.run(tool.execute()) == _FALLBACK_NO_QUESTIONS
    # An empty list is the same case as an absent key, and it is the shape a
    # JSON-Schema ``required`` would have accepted.
    assert asyncio.run(tool.execute(questions=[])) == _FALLBACK_NO_QUESTIONS


# ---------------------------------------------------------------------------
# Fixtures and stand-ins
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_contextvars():
    """Every ContextVar this feature owns, reset around each test.

    They are process-wide within one asyncio context, and pytest gives every test
    the same one - a test that left ``chain_round`` at 1 would silently turn the
    next test's first question into ``chain_exhausted``. That is the same
    inheritance bug the production code sets them unconditionally to avoid.
    """
    from research_flow.gates.conversation import set_research_turn

    def _reset():
        set_research_turn(True)
        set_chain_round(0)
        set_turn_brief("")
        set_clarify_verdict(None)
        set_first_turn(False)
        take_pending_clarify()

    _reset()
    yield
    _reset()


class _Terminal:
    """A text-only response - the shape a prose clarify arrives in."""

    has_tool_calls = False

    def __init__(self, content: str = "") -> None:
        self.content = content
        self.tool_calls = []


class _Spy(AgentHook):
    """Stands in for a terminal gate: records the phase and bounces."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def name(self) -> str:
        return "Spy"

    async def after_iteration(self, ctx) -> HookDecision:
        self.seen.append("after_iteration")
        return HookDecision(rollback=True)


class _Call:
    def __init__(self, name="ask_user", arguments=None):
        self.name = name
        self.arguments = arguments if arguments is not None else {}


class _Response:
    def __init__(self, *calls):
        self.tool_calls = list(calls)


def _defs(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def _ctx(
    *,
    iteration=1,
    tools=("ask_user", "web_search", "web_fetch"),
    messages=None,
    turn_base=0,
    response=None,
    question="the original question",
):
    return AgentHookContext(
        session_key="cli:t",
        turn_question=question,
        turn_base=turn_base,
        iteration=iteration,
        messages=list(messages or [{"role": "user", "content": question}]),
        tools=_defs(*tools),
        response=response,
    )


def _flat(text: str) -> str:
    """One line, single-spaced. For assertions about what a clause SAYS - the wrap
    moves whenever a sentence is added, and chasing it turns every wording change
    into a diff in three unrelated tests. The one place the wrap itself is the
    subject asserts on it directly."""
    return " ".join((text or "").split())


def _q(text="which entity?", options=()):
    return {"question": text, "options": list(options)}


# ---------------------------------------------------------------------------
# ClarifyExemptHook - the prose path the short circuit cannot reach
# ---------------------------------------------------------------------------

_CLARIFY = "Before I research this, which sense do you mean?\n1. the architecture\n2. the device"
_REPORT = "## Answer\nx\n\n## Findings\ny\n\n## Limitations\nz"
_PARTIAL = "## Answer\nx\n\n## Findings\ny - and which sense did you mean?"
_FROM_MEMORY = "Transformers are a neural architecture from the 2017 paper. They use attention."


_UNSET = object()


def _terminal_ctx(content=_CLARIFY, *, iteration=1, ask_required=True, response=_UNSET):
    ctx = _ctx(iteration=iteration, response=_Terminal(content) if response is _UNSET else response)
    ctx.metadata["ask_user"] = {"ask_required": ask_required}
    return ctx


def test_the_prose_clarify_predicate_needs_all_four_conditions() -> None:
    """Structural, and none of the four reads the prose.

    The fourth one is what keeps this from being "skip review on a mandated
    turn": a full report written from memory with no retrieval is exactly what
    the reviewer exists to catch, and it is a plausible first-turn shape.
    """
    assert is_prose_clarify(_terminal_ctx()) is True
    # not mandated - ``when_needed``, or any turn after the first
    assert is_prose_clarify(_terminal_ctx(ask_required=False)) is False
    # research already started this turn
    assert is_prose_clarify(_terminal_ctx(iteration=3)) is False
    # a tool call is the tool path, which never reaches after_iteration anyway
    assert is_prose_clarify(_terminal_ctx(response=_Response(_Call()))) is False
    # a report, not a clarify: reviewed like any other draft
    assert is_prose_clarify(_terminal_ctx(_REPORT)) is False
    # a real draft that merely dropped a section - the bar's own stratum. The first
    # version of the check read "the bar would bounce it" and exempted this.
    assert is_prose_clarify(_terminal_ctx(_PARTIAL)) is False
    # no sections and no question: an answer written from memory, which is exactly
    # what the reviewer is for
    assert is_prose_clarify(_terminal_ctx(_FROM_MEMORY)) is False
    # answerless is ForcedFinalizeGate's, and it is not a clarify
    assert is_prose_clarify(_terminal_ctx("   ")) is False
    # ``ctx.response is None`` at the terminal seam must not raise
    assert is_prose_clarify(_terminal_ctx(response=None)) is False


def test_the_wrapper_stands_down_on_a_prose_clarify_and_says_so() -> None:
    """A suppression that leaves no trace reads exactly like a gate that ran and
    found nothing - the shape that has already cost this repo a batch."""
    spy = _Spy()
    ctx = _terminal_ctx()
    decision = asyncio.run(ClarifyExemptHook(spy).after_iteration(ctx))
    assert spy.seen == []
    assert decision.rollback is False
    assert ctx.metadata["ask_user"]["prose_clarify"] is True
    assert ctx.metadata["ask_user"]["clarify_chars"] == len(_CLARIFY)
    assert ctx.metadata["ask_user"]["exempted"] == "Spy"
    # The marker the loop already reads: it makes the turn non-answerless, which is
    # what closes ``terminal_answerless`` - a seam no wrapper here can reach.
    assert ctx.metadata["clarify_requested"] is True
    # Same flag as the tool path, DIFFERENT channel: no pending is stashed here and
    # no ``maxRounds`` round is spent, so a reader that buckets on
    # ``awaiting_user`` alone mixes an open chain with a turn that has none.
    assert ctx.metadata["clarify_source"] == "prose"


def test_the_wrapper_forwards_every_other_terminal() -> None:
    spy = _Spy()
    ctx = _terminal_ctx(_REPORT)
    decision = asyncio.run(ClarifyExemptHook(spy).after_iteration(ctx))
    assert spy.seen == ["after_iteration"]
    assert decision.rollback is True
    assert "prose_clarify" not in ctx.metadata["ask_user"]


# ---------------------------------------------------------------------------
# The gate: withholding the tool
# ---------------------------------------------------------------------------


def _gate(**kw) -> AskUserGate:
    return AskUserGate(**kw)


def _withheld(decision, ctx) -> bool:
    names = {(t.get("function") or {}).get("name") for t in (decision.modified_tools or ctx.tools)}
    return "ask_user" not in names


def test_the_tool_is_on_offer_on_the_first_iteration_of_a_research_turn() -> None:
    ctx = _ctx()
    decision = asyncio.run(_gate().before_iteration(ctx))
    assert decision.modified_tools is None
    assert ctx.metadata["ask_user"]["allowed_at"] == 1
    assert "withheld_reason" not in ctx.metadata["ask_user"]


@pytest.mark.parametrize(
    "reason,setup",
    [
        (
            "non_research_turn",
            lambda: __import__("research_flow.gates.conversation", fromlist=["x"]).set_research_turn(False),
        ),
        ("chain_exhausted", lambda: set_chain_round(1)),
    ],
)
def test_each_condition_withholds_the_tool_and_names_itself(reason, setup) -> None:
    """A withheld tool and the reason it was withheld are one fact. A counter that
    appears only on firing cannot distinguish "did not fire" from "not installed",
    which is the note ``fetch_gate`` left on the same shape."""
    setup()
    ctx = _ctx()
    decision = asyncio.run(_gate().before_iteration(ctx))
    assert _withheld(decision, ctx)
    assert ctx.metadata["ask_user"]["withheld_reason"] == reason
    assert ctx.metadata["ask_user"]["withheld"] == 1


def test_a_later_iteration_withholds_the_tool() -> None:
    ctx = _ctx(iteration=2)
    decision = asyncio.run(_gate().before_iteration(ctx))
    assert _withheld(decision, ctx)
    assert ctx.metadata["ask_user"]["withheld_reason"] == "not_first_iteration"


def test_a_search_this_turn_withholds_it_even_with_the_iteration_flag_off() -> None:
    """This is what makes the clause honest under ``firstIterationOnly: false``:
    it tells the model "after your first search it is gone", and without this
    condition that sentence would be false in that configuration."""
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "tool_calls": [{"function": {"name": "web_search"}}]},
        {"role": "tool", "name": "web_search", "content": "results"},
    ]
    ctx = _ctx(iteration=2, messages=messages)
    decision = asyncio.run(_gate(first_iteration_only=False).before_iteration(ctx))
    assert _withheld(decision, ctx)
    assert ctx.metadata["ask_user"]["withheld_reason"] == "searched"


def test_the_search_scan_starts_at_turn_base() -> None:
    """A full-list scan finds the PREVIOUS turn's searches and withholds the tool
    on every turn from the second on - and turn two onwards is the only place this
    feature can work at all. ``fetch_gate.py`` paid for this exact bug."""
    history = [
        {"role": "user", "content": "turn one"},
        {"role": "tool", "name": "web_search", "content": "results"},
        {"role": "assistant", "content": "answer"},
    ]
    ctx = _ctx(messages=history + [{"role": "user", "content": "turn two"}], turn_base=len(history))
    decision = asyncio.run(_gate(first_iteration_only=False).before_iteration(ctx))
    assert decision.modified_tools is None
    assert "withheld_reason" not in ctx.metadata["ask_user"]


def test_a_missing_tool_is_reported_rather_than_passed_over() -> None:
    """The switch is on and the schema has no ``ask_user`` - almost always
    ``tools.disabledTools``, the lock every bench profile pins. A rule whose action
    is a no-op reads downstream exactly like a rule that acted and did not help."""
    ctx = _ctx(tools=("web_search", "web_fetch"))
    decision = asyncio.run(_gate().before_iteration(ctx))
    assert decision.modified_tools is None
    assert ctx.metadata["ask_user"]["tool_absent"] is True


def test_a_quiet_turn_still_reports() -> None:
    """ "asked nothing" and "was not installed" are different states. Getting this
    wrong is what left 40 of 120 items with no ``fetch_floor`` record and turned a
    mean of 58.1 into 83.7."""
    ctx = _ctx()
    asyncio.run(_gate().before_iteration(ctx))
    state = ctx.metadata["ask_user"]
    assert state["asked"] is False
    assert state["withheld"] == 0
    assert state["chain_round"] == 0


# ---------------------------------------------------------------------------
# The gate: the handoff
# ---------------------------------------------------------------------------


def _ask(**payload):
    return _Response(_Call(arguments=payload))


def _scenario(fn):
    """Run a whole scenario inside ONE asyncio context.

    ``asyncio.run`` creates a fresh Task and a Task COPIES the current Context, so
    a ContextVar set inside one ``asyncio.run`` is invisible to the next call and
    to the caller. The production handoff works precisely because the gate and
    ``_persist_pending_clarify`` run in the same task - ``_run_agent_loop`` is
    awaited, never spawned - so a test that called ``asyncio.run`` per step would
    be asserting against a topology this code never has. Anything that reads
    ``take_pending_clarify`` therefore runs in here.
    """
    return asyncio.run(fn())


def test_a_call_with_questions_becomes_the_turns_reply() -> None:
    ctx = _ctx(response=_ask(questions=[_q("which entity?"), _q("which year?")]))
    gate = _gate()
    asyncio.run(gate.before_iteration(ctx))
    decision = asyncio.run(gate.before_execute_tools(ctx))
    assert decision.short_circuit_result is not None
    assert decision.short_circuit_result.startswith("I will start researching")
    assert "which entity?" in decision.short_circuit_result
    state = ctx.metadata["ask_user"]
    assert state["asked"] is True and state["n_questions"] == 2
    assert ctx.metadata["clarify_requested"] is True
    # The channel, beside the fact. Only this path stashes a pending and spends a
    # ``maxRounds`` round, so "awaiting_user implies an open chain" holds here and
    # not on the prose path - a distinction the boolean alone cannot carry.
    assert ctx.metadata["clarify_source"] == "tool"


def test_the_handoff_stashes_a_pending_for_the_loop_to_persist() -> None:
    """The gate cannot write it: ``AgentHookContext`` carries a ``session_key`` and
    not the ``Session``. Same split ``stash_turn_rows`` exists for."""
    ctx = _ctx(response=_ask(questions=[_q()]), question="the original question")
    gate = _gate()

    async def run():
        await gate.before_iteration(ctx)
        await gate.before_execute_tools(ctx)
        first = take_pending_clarify()
        # Read-once: a later turn that asked nothing must not re-persist this one.
        return first, take_pending_clarify()

    pending, second = _scenario(run)
    assert pending["original_question"] == "the original question"
    assert pending["chain_round"] == 1
    assert second is None


def test_an_outline_only_call_does_not_end_the_turn() -> None:
    """Guardrail 1. A question has a threshold only the user can clear; an outline
    has none, so a model that wants to look diligent can always produce one and a
    handoff on an outline puts a round trip in front of every research item.

    Not a halting decision: ``CompositeHook`` halts only on
    ``short_circuit_result`` or ``rollback``, so the loop proceeds and the model
    reads ``DRAskUserTool``'s fallback string."""
    ctx = _ctx(response=_ask(outline=[{"goal": "g", "evidence": "e"}]))
    gate = _gate()

    async def run():
        await gate.before_iteration(ctx)
        return await gate.before_execute_tools(ctx), take_pending_clarify()

    decision, pending = _scenario(run)
    assert decision.short_circuit_result is None
    assert decision.rollback is False
    assert ctx.metadata["ask_user"]["outline_only_refused"] is True
    assert ctx.metadata["ask_user"]["asked"] is False
    assert "clarify_requested" not in ctx.metadata
    assert pending is None


def test_a_mixed_call_still_hands_off_and_says_so() -> None:
    """The short circuit discards the whole response by construction, so the other
    calls cannot be kept without leaving dangling ``tool_calls`` a strict provider
    rejects. Asking wins; the loss is counted rather than hidden."""
    ctx = _ctx(
        response=_Response(
            _Call(arguments={"questions": [_q()]}),
            _Call(name="web_search", arguments={"query": "x"}),
        )
    )
    gate = _gate()
    asyncio.run(gate.before_iteration(ctx))
    decision = asyncio.run(gate.before_execute_tools(ctx))
    assert decision.short_circuit_result is not None
    assert ctx.metadata["ask_user"]["mixed_call"] is True


def test_a_call_on_a_withheld_iteration_is_not_honoured() -> None:
    """Withholding a tool from the schema does not stop a model from naming it.
    Honouring such a call would hand the turn away on an iteration this gate had
    closed - and on a non-research turn that means a formatting request ends in a
    question."""
    ctx = _ctx(iteration=3, response=_ask(questions=[_q()]))
    gate = _gate()

    async def run():
        await gate.before_iteration(ctx)
        return await gate.before_execute_tools(ctx), take_pending_clarify()

    decision, pending = _scenario(run)
    assert decision.short_circuit_result is None
    assert ctx.metadata["ask_user"]["called_when_withheld"] is True
    assert pending is None


def test_a_response_without_the_tool_is_left_alone() -> None:
    ctx = _ctx(response=_Response(_Call(name="web_search", arguments={"query": "x"})))
    decision = asyncio.run(_gate().before_execute_tools(ctx))
    assert decision.short_circuit_result is None
    assert decision.modified_tools is None


# ---------------------------------------------------------------------------
# The chain budget passes THROUGH the consume
# ---------------------------------------------------------------------------


def test_the_chain_count_passes_through_the_consume() -> None:
    """Zeroing on consume would make round two indistinguishable from round one
    and ``maxRounds > 1`` structurally unreachable - the contradiction the design's
    second draft carried.

    Round 1 asks with an incoming count of 0 and writes 1. The turn that answers
    carries 1 in; with ``maxRounds=2`` it may ask again and writes 2. The next turn
    carries 2 in and is refused."""
    gate = _gate(max_rounds=2)

    async def one_round(incoming):
        set_chain_round(incoming)
        ctx = _ctx(response=_ask(questions=[_q()]))
        withheld_decision = await gate.before_iteration(ctx)
        decision = await gate.before_execute_tools(ctx)
        return ctx, withheld_decision, decision, take_pending_clarify()

    async def run():
        return [await one_round(n) for n in (0, 1, 2)]

    rounds = _scenario(run)

    for i, incoming in enumerate((0, 1)):
        _ctx_i, _withheld_i, decision, pending = rounds[i]
        assert decision.short_circuit_result is not None
        assert pending["chain_round"] == incoming + 1

    ctx, withheld_decision, decision, pending = rounds[2]
    assert _withheld(withheld_decision, ctx)
    assert ctx.metadata["ask_user"]["withheld_reason"] == "chain_exhausted"
    assert pending is None


def test_a_closed_chain_gives_the_next_question_the_full_budget() -> None:
    """The scope is the chain, not the session. Counting per session would spend
    the budget on a conversation's first research question and refuse its second,
    unrelated one - and a session holding two questions is a convention this code
    cannot enforce, so the failure would be silent."""
    gate = _gate(max_rounds=1)

    async def run():
        set_chain_round(0)  # what the loop sets on a turn with no pending
        ctx = _ctx(response=_ask(questions=[_q()]))
        await gate.before_iteration(ctx)
        return await gate.before_execute_tools(ctx)

    assert _scenario(run).short_circuit_result is not None


# ---------------------------------------------------------------------------
# Regressions the first live acceptance run found
# ---------------------------------------------------------------------------

# The user's own words, in Chinese, as escapes: the switch has to be exercised
# with real CJK input, and a raw literal is what the repo's one catalog exists to
# keep out of every other module.
_ZH_QUESTION = (
    "\u5e2e\u6211\u67e5\u4e00\u4e0b\u6211\u4eec\u51e0\u4e2a\u4e3b\u8981\u7ade\u54c1"
    "\u6700\u8fd1\u7684\u5b9a\u4ef7\u7b56\u7565\uff0c\u505a\u4e2a\u5bf9\u6bd4\u3002"
)
_ZH_ASK = "\u54ea\u4e2a\u884c\u4e1a\uff1f"


def test_the_gate_stores_the_stripped_question() -> None:
    """The end-to-end version of the above: what the gate writes into the pending
    is what the brief and the language switch will read.

    [port] The lead-in is an English source string rendered through ``t_in``, so
    the Chinese scaffolding is asked of the catalog rather than spelled here.
    """
    question = _ZH_QUESTION
    ctx = _ctx(response=_ask(questions=[_q(_ZH_ASK)]), question=f"{question}\n\n{render_reminder()}")
    gate = _gate()

    async def run():
        await gate.before_iteration(ctx)
        decision = await gate.before_execute_tools(ctx)
        return decision, take_pending_clarify()

    decision, pending = _scenario(run)
    assert pending["original_question"] == question
    assert "report format reminder" not in pending["original_question"]
    # And therefore the scaffolding is Chinese, which is what the run got wrong.
    assert decision.short_circuit_result.startswith(t_in("zh", _HANDOFF_LEAD))


def test_the_withheld_reason_keeps_the_first_one_not_the_last() -> None:
    """Observed on the first live two-turn run: iteration 1 was withheld as
    ``chain_exhausted`` - the only informative reason on that turn - and
    iterations 2..10 overwrote it with ``not_first_iteration``, which after
    iteration 1 is true of every turn and therefore says nothing. Read with
    ``allowed_at``: absent means this reason is why the tool was never on offer."""
    gate = _gate(max_rounds=1)
    ctx = _ctx(iteration=1)

    async def run():
        set_chain_round(1)  # a chain already spent
        await gate.before_iteration(ctx)
        first = ctx.metadata["ask_user"]["withheld_reason"]
        for it in (2, 3, 4):
            ctx.iteration = it
            ctx.tools = _defs("ask_user", "web_search", "web_fetch")
            await gate.before_iteration(ctx)
        return first, ctx.metadata["ask_user"]

    first, state = _scenario(run)
    assert first == "chain_exhausted"
    assert state["withheld_reason"] == "chain_exhausted"
    assert state["withheld"] == 4
    assert "allowed_at" not in state


# ---------------------------------------------------------------------------
# askUser.mode - the product default asks on turn one
# ---------------------------------------------------------------------------


def test_both_prompt_surfaces_agree_about_the_mode() -> None:
    """The clause and the tool description are two prompt surfaces describing the
    same call. If they disagree the reading is unattributable - which of the two the
    model followed is not recoverable from the trajectory."""
    for mode, mandated in (("first_turn", True), ("when_needed", False)):
        clause = _seg(ask_user=True, ask_user_mode=mode)
        described = DRAskUserTool(mode=mode).description
        assert ("even when the question looks complete" in clause) is mandated
        assert ("even when the question looks complete" in described) is mandated
        # Both prohibitions survive in both modes, in both surfaces.
        for surface in (clause, described):
            assert "look up" in _flat(surface)
            assert "stop working" in _flat(surface)


def test_a_mandated_turn_records_whether_it_complied() -> None:
    """Nothing can force a model to emit a tool call, so ``mode="first_turn"`` is a
    REQUEST. ``asked`` on a mandated turn is therefore a compliance rate and on any
    other turn it is a preference - and one column cannot be both unless the turn
    says which it is. Enforcement is deliberately absent: bouncing a first turn that
    did not ask would re-sample turns, the strongest kind of distribution change,
    and the compliance rate has to be known before that trade can be priced."""
    gate = _gate(mode="first_turn")

    async def run(first):
        set_first_turn(first)
        ctx = _ctx()
        await gate.before_iteration(ctx)
        return ctx.metadata["ask_user"]

    async def both():
        return await run(True), await run(False)

    mandated, later = _scenario(both)
    assert (mandated["first_turn"], mandated["ask_required"]) == (True, True)
    assert (later["first_turn"], later["ask_required"]) == (False, False)
    assert mandated["mode"] == later["mode"] == "first_turn"
    # Both still record ``asked``, so the two populations are separable offline.
    assert mandated["asked"] is False and later["asked"] is False


def test_when_needed_never_marks_a_turn_as_mandated() -> None:
    gate = _gate(mode="when_needed")

    async def run():
        set_first_turn(True)
        ctx = _ctx()
        await gate.before_iteration(ctx)
        return ctx.metadata["ask_user"]

    state = _scenario(run)
    assert state["first_turn"] is True  # the fact is still recorded
    assert state["ask_required"] is False  # but nothing was mandated


def test_the_outline_is_told_to_start_after_the_answers() -> None:
    """The model reliably wrote the ask itself in as the outline's first step -
    "confirm which industry: the user tells me directly" - across three independent
    live runs. It is redundant on its face: the questions are in the same message,
    one section up. Left in, the reply reads as a plan that has not started yet,
    which undercuts the lead-in's promise that research begins on the answer.

    Fixed in the PROMPT, not by filtering the payload: a parse-layer filter would
    need to recognise "the user" in every language the product serves, and it would
    silently drop a step the model thought mattered rather than stop it being
    written."""
    for mode in ("when_needed", "first_turn"):
        clause = _seg(ask_user=True, ask_user_outline=True, ask_user_mode=mode)
        assert "begins AFTER their" in clause
        assert "never list asking them as one of its steps" in clause
        # Both prompt surfaces, or the two disagree about the same field.
        tool = DRAskUserTool(outline=True, mode=mode)
        assert "never list asking them as a step" in tool.description
        outline = tool.parameters["properties"]["outline"]
        assert "AFTER the questions above are answered" in outline["description"]
        evidence = outline["items"]["properties"]["evidence"]["description"]
        assert "a source, never the user" in evidence


def test_the_handoff_is_written_in_the_second_person() -> None:
    """The whole reply is addressed to the person reading it, and the model slipped
    into the third person inside the outline - "so the user understands the
    difference" - in a message spoken TO that user.

    The rule lives in the clause BODY, not in the outline slot, so it still holds
    when ``outline`` is off. And it states the form to USE rather than quoting the
    form to avoid: this repo's own detector doctrine is that a prohibition puts its
    phrase into the prompt, where the model then echoes it."""
    for mode in ("when_needed", "first_turn"):
        for outline in (True, False):
            clause = _seg(ask_user=True, ask_user_mode=mode, ask_user_outline=outline)
            assert "use the second person throughout" in clause
            # The rule does not quote what it forbids.
            assert 'never "the user"' not in clause
    described = DRAskUserTool().description
    assert "use the second person throughout" in described
    why = DRAskUserTool().parameters["properties"]["outline"]["items"]["properties"]["why"]
    assert "not who benefits from it" in why["description"]


def test_the_outline_renders_the_goal_and_nothing_else() -> None:
    """Rendering goal + evidence + why put 100+ characters on one step, and this
    section exists to be SCANNED for "is that the right plan" - three clauses per
    step is harder to scan, not more informative.

    The schema still asks for all three, and the two halves must not be
    "reconciled" in either direction: asking what evidence settles a step is what
    keeps the outline naming decisions rather than queries, so dropping the fields
    would make the goals vague. Both stay in the persisted pending, so the
    reasoning is still in the session record."""
    step = {"goal": "settle the entity", "evidence": "the annual report", "why": "the figure differs between them"}
    p = PendingClarify(original_question="which entity?", questions=[_q()], outline=[step])
    handoff = render_handoff(p)
    assert "1. settle the entity" in handoff
    assert "annual report" not in handoff
    assert "figure differs" not in handoff
    # Still asked for, still persisted.
    schema = DRAskUserTool().parameters["properties"]["outline"]["items"]["properties"]
    assert set(schema) == {"goal", "evidence", "why"}
    assert p.to_metadata()["outline"][0] == step


# ---------------------------------------------------------------------------
# askUser.delivery - the broker round trip
# ---------------------------------------------------------------------------


class _FakeBroker:
    """Records what the round trip sent and answers every question the same.

    [port] The trunk kernel's ``AskUserTool`` passes a deadline and the batch
    position beside the prompt, so the stand-in absorbs them; what it records is
    still the three fields the round trip is about.
    """

    def __init__(self, answer="the 2024 fiscal year"):
        self.calls = []
        self._answer = answer

    async def await_question(self, cid, *, prompt, choices, **kwargs):
        self.calls.append((cid, prompt, tuple(choices)))
        return self._answer


def _tool_delivery_tool(broker=None) -> DRAskUserTool:
    tool = DRAskUserTool(delivery="tool")
    if broker is not None:
        tool.set_broker(broker)
        tool.set_context("cli:t")
    return tool


def test_the_default_delivery_is_the_measured_handoff() -> None:
    """The knob's off state IS the default: every pinned sha above renders with
    ``delivery`` unset, so this equality is what lets them stand for the default.

    [port] The shas themselves belong to the prompt surface and are not read in
    this file; the equality they rest on is.
    """
    assert AskUserConfig().delivery == "handoff"
    for mode in ("when_needed", "first_turn"):
        assert _seg(ask_user=True, ask_user_mode=mode) == _seg(
            ask_user=True, ask_user_mode=mode, ask_user_delivery="handoff"
        )


def test_the_dr_tool_blocks_only_under_tool_delivery() -> None:
    """The registry skips its timeout on ``blocking_interaction``: a granted round
    trip waits on a human and must not be timer-killed, while the handoff default
    keeps the ordinary-tool treatment the measured arms were run with."""
    assert DRAskUserTool().blocking_interaction is False
    assert DRAskUserTool(delivery="tool").blocking_interaction is True


def test_round_trip_readiness_needs_the_knob_the_broker_and_the_context() -> None:
    tool = DRAskUserTool(delivery="tool")
    assert tool.round_trip_ready is False
    tool.set_broker(_FakeBroker())
    assert tool.round_trip_ready is False
    tool.set_context("cli:t")
    assert tool.round_trip_ready is True

    handoff = DRAskUserTool()
    handoff.set_broker(_FakeBroker())
    handoff.set_context("cli:t")
    assert handoff.round_trip_ready is False


def test_an_ungranted_call_never_reaches_the_broker() -> None:
    """The grant is the gate's authority, not the broker's availability: a call on
    a withheld iteration executes (the gate leaves it to read the fallback), and
    it must not spend a round trip however ready the broker is."""
    broker = _FakeBroker()
    tool = _tool_delivery_tool(broker)
    result = asyncio.run(tool.execute(questions=[_q("which year?")]))
    assert result == _FALLBACK_NOT_DELIVERED
    assert broker.calls == []


def test_a_granted_call_runs_the_round_trip_and_the_grant_is_read_once() -> None:
    """[port] A delivered round trip returns the kernel tool's ``ToolResult``, so
    the answer is read off ``model_text`` - the text the model is handed - rather
    than off a bare string."""
    broker = _FakeBroker()
    tool = _tool_delivery_tool(broker)

    async def run():
        tool.grant_round_trip()
        first = await tool.execute(questions=[_q("which year?", options=("2023", "2024"))])
        second = await tool.execute(questions=[_q("which market?")])
        return first, second

    first, second = _scenario(run)
    assert broker.calls == [("cli:t", "which year?", ("2023", "2024"))]
    assert "which year?" in first.model_text and "the 2024 fiscal year" in first.model_text
    assert second == _FALLBACK_NOT_DELIVERED


def test_the_gate_grants_the_round_trip_instead_of_short_circuiting() -> None:
    """The turn continues: no handoff reply, no pending, no ``clarify_requested``
    marker - the tool result carries the answers and the same turn researches."""
    broker = _FakeBroker()
    tool = _tool_delivery_tool(broker)
    ctx = _ctx(response=_ask(questions=[_q("which entity?")]))
    gate = _gate(delivery="tool", tool=tool)

    async def run():
        await gate.before_iteration(ctx)
        decision = await gate.before_execute_tools(ctx)
        result = await tool.execute(questions=[_q("which entity?")])
        return decision, result, take_pending_clarify()

    decision, result, pending = _scenario(run)
    assert decision.short_circuit_result is None
    assert "the 2024 fiscal year" in result.model_text
    assert pending is None
    assert "clarify_requested" not in ctx.metadata
    state = ctx.metadata["ask_user"]
    assert state["asked"] is True
    assert state["delivery"] == "tool"
    assert state["n_questions"] == 1


def test_the_gate_falls_back_to_the_handoff_without_a_broker() -> None:
    """ACP and ``raven agent -m`` wire no broker, so ``delivery="tool"`` there
    must not turn the mandated round into a fallback string: the measured
    handoff stays the transport, recorded as what actually happened."""
    tool = DRAskUserTool(delivery="tool")  # no broker, no conversation_id
    ctx = _ctx(response=_ask(questions=[_q("which entity?")]))
    gate = _gate(delivery="tool", tool=tool)

    async def run():
        await gate.before_iteration(ctx)
        decision = await gate.before_execute_tools(ctx)
        return decision, take_pending_clarify()

    decision, pending = _scenario(run)
    assert decision.short_circuit_result is not None
    assert "which entity?" in decision.short_circuit_result
    assert pending is not None
    assert ctx.metadata["clarify_requested"] is True
    assert ctx.metadata["ask_user"]["delivery"] == "handoff"


def test_the_tool_delivery_accepts_the_shapes_the_gate_grants() -> None:
    """The registry's strict schema check runs BETWEEN the gate's grant and the
    tool's own cleaning, and the gate keeps a bare-string question
    (``clean_questions``). Bounced at the registry, the broker is never called
    while the state already says ``asked`` - so the DR tool accepts there and
    cleans in ``execute``, and the granted question reaches the user."""
    from raven.agent.tools.registry import ToolRegistry

    broker = _FakeBroker()
    tool = _tool_delivery_tool(broker)
    registry = ToolRegistry()
    registry.register(tool)

    async def run():
        tool.grant_round_trip()
        return await registry.execute("ask_user", {"questions": ["which year?"]})

    result = _scenario(run)
    assert not result.startswith("Error")
    assert "the 2024 fiscal year" in result
    assert broker.calls == [("cli:t", "which year?", ())]


def test_every_payload_the_gate_grants_is_deliverable() -> None:
    """With ``validate_params`` open under the round trip, the registry no
    longer stands between the gate and the tool - so the gate's acceptance
    (``parse_ask_user_args``) and the tool's (``clean_questions`` in
    ``execute``) must stay ONE set. A payload granted but undeliverable would
    spend the round, write ``asked=True`` and ask nobody. Both sides share
    ``clean_questions`` today; this pins the invariant against a future
    divergence of either wrapper."""
    shapes = [
        {"questions": ["which year?"]},
        {"questions": [{"question": "q", "options": "not a list"}]},
        {"questions": [{"question": "q"}, "  ", {"question": ""}]},
        {"questions": "not a list"},
        {"questions": [None, 7]},
        {"outline": [{"goal": "g", "evidence": "e"}]},
    ]
    for raw in shapes:
        granted_by_gate = bool(parse_ask_user_args(raw).questions)
        broker = _FakeBroker()
        tool = _tool_delivery_tool(broker)

        async def run(raw=raw):
            tool.grant_round_trip()
            return await tool.execute(**raw)

        result = _scenario(run)
        if granted_by_gate:
            assert broker.calls, f"granted but undelivered: {raw!r}"
        else:
            assert result == _FALLBACK_NO_QUESTIONS
            assert broker.calls == []


def test_the_tool_delivery_never_asks_for_an_outline() -> None:
    """The outline is the handoff's affordance: rendered in the reply for the
    user to veto. The broker prompt carries questions only and the result
    returns answers only, so under ``delivery="tool"`` every prompt surface -
    clause, description, schema - drops the ask together, and the gate's state
    records the effective value rather than the configured one."""
    for mode in ("when_needed", "first_turn"):
        text = _flat(_seg(ask_user=True, ask_user_mode=mode, ask_user_delivery="tool", ask_user_outline=True))
        assert "with `outline`" not in text
        # The handoff clause keeps it, same knob.
        assert "with `outline`" in _flat(_seg(ask_user=True, ask_user_mode=mode, ask_user_outline=True))

    tool = DRAskUserTool(outline=True, delivery="tool")
    assert "outline" not in tool.parameters["properties"]
    assert "outline" not in tool.description.lower()

    ctx = _ctx()
    asyncio.run(_gate(delivery="tool", outline=True).before_iteration(ctx))
    assert ctx.metadata["ask_user"]["outline"] is False


def test_a_stale_grant_is_revoked_at_the_iteration_boundary() -> None:
    """A granted call can die between grant and execute (a cast or transport
    failure). The ticket must not survive for a later, ungranted call in the
    same turn to spend on the broker."""
    broker = _FakeBroker()
    tool = _tool_delivery_tool(broker)
    gate = _gate(delivery="tool", tool=tool)

    async def run():
        ctx = _ctx(response=_ask(questions=[_q("which entity?")]))
        await gate.before_iteration(ctx)
        await gate.before_execute_tools(ctx)  # grants; the call never executes
        ctx.iteration = 2
        await gate.before_iteration(ctx)
        return await tool.execute(questions=[_q("sneaky?")])

    result = _scenario(run)
    assert result == _FALLBACK_NOT_DELIVERED
    assert broker.calls == []


def test_the_withdrawal_after_a_round_trip_is_not_counted_as_withheld() -> None:
    """After a broker round trip the turn keeps going and the tool leaves the
    schema - the round's designed lifecycle, not a refusal. Counting it would
    make ``withheld`` (always 0 on an asking handoff turn, which short-circuits)
    read as a per-iteration refusal tally under the other delivery."""
    broker = _FakeBroker()
    tool = _tool_delivery_tool(broker)
    gate = _gate(delivery="tool", tool=tool)

    async def run():
        ctx = _ctx(response=_ask(questions=[_q("which entity?")]))
        await gate.before_iteration(ctx)
        await gate.before_execute_tools(ctx)
        await tool.execute(questions=[_q("which entity?")])
        ctx.iteration = 2
        decision = await gate.before_iteration(ctx)
        ctx.iteration = 3
        await gate.before_iteration(ctx)
        return ctx, decision

    ctx, decision = _scenario(run)
    assert _withheld(decision, ctx)
    state = ctx.metadata["ask_user"]
    assert state["withdrawn_after_ask"] is True
    assert state["withheld"] == 0
    assert "withheld_reason" not in state


def test_a_single_option_question_folds_to_free_form_and_still_reaches_the_broker() -> None:
    """[A-8] The fork's transport accepted a one-option question; the trunk tool
    rejects it as "not a decision" -- and a gate-granted round trip must not be
    spent on that rejection string. The lone suggestion folds into the question
    text and the entry goes free-form: the same question, in the kernel-legal
    shape. Duplicate labels count as one, the way the trunk counts them.
    """
    broker = _FakeBroker()
    tool = _tool_delivery_tool(broker)

    async def run():
        tool.grant_round_trip()
        return await tool.execute(questions=[_q("which entity?", options=("ACME Corp",))])

    result = _scenario(run)
    assert broker.calls == [("cli:t", "which entity? (suggested: ACME Corp)", ())]
    assert "rejected" not in str(result.model_text)

    broker2 = _FakeBroker()
    dup = _tool_delivery_tool(broker2)

    async def run_dup():
        dup.grant_round_trip()
        return await dup.execute(questions=[_q("which market?", options=("EU", "EU"))])

    _scenario(run_dup)
    assert broker2.calls == [("cli:t", "which market? (suggested: EU)", ())]
