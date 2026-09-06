"""dr@3.4-askuser: how the flow assembles the clarify round.

The assembly half of the fork's ``test_agent_flow_ask_user.py``: what the two
knobs resolve to, what ``research_flow.flow.build_chain`` wires when they are
on, what ``research_flow.prompts`` renders into the identity and the contract,
and what ``TurnFrame`` does with an open round at the two turn boundaries. The
gate and tool behaviour this stands on is driven from
``test_agents_research_flow_ask_user.py``; the pure text surface from
``test_agents_research_ask_user_text.py``.

[port] The fork read all of this off one ``build_dr_flow`` assembly object. Six
of its surfaces have no counterpart on this build and their assertions are
adapted or dropped rather than weakened:

* ``DRModeSegmentBuilder``. The plugin renders the same bytes through
  ``research_flow.prompts.render_parts`` and the product's launcher seeds them
  into the workspace as ``soul.md`` + ``agent.md`` (``agents/raven-research/
  run.py``), so a fork assertion about the segment TEXT is read off the
  renderer, and the four measured shas still hold to the byte;
* ``tools_allowlist``. The plugin does no allowlist slimming - the product
  shapes its tool face with ``tools.disabledTools`` - so the fork's three
  allowlist tests are dropped rather than rewritten against a different lock;
* the tool REGISTRY. The trunk registers the plugin's contribution, so what the
  fork asserted about ``AgentLoop.tools`` is pinned end to end by
  ``test_agents_research_launcher.py``; what survives here is the plugin's own
  half of it - the factory contributes the tool exactly when the clause names
  it, and declines otherwise;
* ``AgentLoop``'s turn seams. The consume / persist / inject work is
  ``TurnFrame``'s, and is driven as such below; the persistence STRIP is the
  trunk loop's ``_save_turn`` and is not the plugin's to assert;
* the bench tooling around the prompt - the segment/tool stamp script and the
  ``examples/*.json`` arm profiles it stamped. Neither came across: the product
  ships one profile, ``agents/raven-research/config.json``, and what that
  profile opens is pinned end to end by ``test_agents_research_launcher.py``;
* the personalizer precedence. The fork's loop asked its assembly whether
  ask_user superseded the built-in clarify path; the trunk's personalizer sits
  behind a loop-level switch no plugin can read, so that arbitration has no
  seam on this build.

[port] The turn-scoped state crosses a phase boundary the fork never had: the
trunk builds one ``AgentHookContext`` per phase, so ``ctx.metadata`` does not
carry from ``before_user_inbound`` to ``before_iteration`` and the clarify
verdict reaches the turn-mode decision through its ContextVar instead. The
scenarios below therefore build a context per phase, the way the loop does.

[port] Chinese INPUT is what exercises the reply/new-request split, and it is
spelled with escapes: ``raven/i18n/`` is the only place in this repo that
carries CJK text.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow import prompts  # noqa: E402
from research_flow.config import AskUserConfig, FlowConfig  # noqa: E402
from research_flow.flow import ToolHandles, TurnFrame, build_chain  # noqa: E402
from research_flow.gates.ask_user import (  # noqa: E402
    AskUserGate,
    PendingClarify,
    set_chain_round,
    set_clarify_verdict,
    set_first_turn,
    set_turn_brief,
    take_pending_clarify,
)
from research_flow.gates.conversation import set_research_turn  # noqa: E402
from research_flow.prompts import render_identity_and_contract, render_parts  # noqa: E402
from research_flow.state import SessionRecord, SessionStore  # noqa: E402
from research_flow.tools.ask_user import DRAskUserTool  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402

# The reply/new-request split, in the language the live traffic arrived in.
_ZH_ORIGINAL = "\u54ea\u4e00\u5bb6\u8bfa\u57fa\u4e9a\u5b9e\u4f53\u7684\u8425\u6536\u66f4\u9ad8\uff1f"
_ZH_QUESTION = "\u4f60\u6307\u7684\u662f\u624b\u673a\u4e1a\u52a1\u8fd8\u662f\u901a\u4fe1\u7f51\u7edc\u4e1a\u52a1\uff1f"
_ZH_NEW_REQUEST = (
    "\u7b97\u4e86\uff0c\u5e2e\u6211\u67e5\u4e00\u4e0b\u6b27\u76df\u65b0\u7535\u6c60"
    "\u6cd5\u89c4\u7684\u5408\u89c4\u622a\u6b62\u65e5\u671f\u90fd\u6709\u54ea\u4e9b\u3002"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _seg(**kw) -> str:
    """The prompt text one config state renders, as the fork's segment built it.

    [port] ``DRModeSegmentBuilder`` became ``research_flow.prompts.render_parts``,
    which returns the identity and the contract rather than a ``Segment``; the
    join is the builder's own (the language directive between them is empty on
    every state read here).
    """
    identity, contract = render_parts(**kw)
    return f"{identity}\n\n{contract}"


def _flat(text: str) -> str:
    """One line, single-spaced. For assertions about what a clause SAYS - the wrap
    moves whenever a sentence is added, and chasing it turns every wording change
    into a diff in three unrelated tests."""
    return " ".join((text or "").split())


def _q(text="which entity?", options=()):
    return {"question": text, "options": list(options)}


class _StubProvider:
    """Never called: these tests only build the chain and read what it holds."""

    async def chat_with_retry(self, **kwargs):  # pragma: no cover - construction only
        raise AssertionError("no generation in these tests")


def _cfg(**overrides) -> FlowConfig:
    """The on state: both knobs, which is what ``ask_user_on`` resolves against."""
    payload = {"enabled": True, "ask_user": {"enabled": True}, "conversation": {"enabled": True}}
    payload.update(overrides)
    return FlowConfig(**payload)


def _chain(cfg: FlowConfig, tmp_path, *, tools=None, provider=None) -> list:
    """The fork's ``build_dr_flow(cfg, None, 20, 200_000)``, as one hook chain."""
    return build_chain(
        cfg,
        provider,
        max_iterations=20,
        context_window_tokens=200_000,
        tools=tools if tools is not None else ToolHandles(ask_user=DRAskUserTool()),
        store=SessionStore(tmp_path),
    )


def _gates(chain: list) -> list:
    return [o for o in chain if isinstance(o, AskUserGate)]


def _contributed_ask_user(tmp_path, slice_: dict):
    """What the plugin's ``ask_user`` factory contributes for one config slice.

    The fork read registration off ``AgentLoop.tools``; here the host registers
    what the factory returns, so the plugin's own decision is the returned
    instance - or ``None``, which leaves the trunk's blocking tool in place.
    """
    from research_flow import plugin as flow_plugin
    from research_flow.support.ledger import set_ledger_dir

    from raven.plugins.context import PluginContext, ServiceLocator

    # A provider is lent because the plugin contributes as one piece: without
    # one it declines the hook, and with the hook it declines the tools too,
    # since they are per-session only through it.
    ctx = PluginContext(
        config=dict(slice_),
        services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a", provider=object()),
    )
    try:
        return flow_plugin.make_ask_user(ctx)
    finally:
        # The activation cache is keyed by workspace and the ledger directory is
        # a module global; both are activation state, and a test that left
        # either behind would hand the next one another config's answer.
        flow_plugin._SHARED.pop(str(tmp_path), None)
        set_ledger_dir(None)


def _scenario(fn):
    """Run a whole scenario inside ONE asyncio context.

    ``asyncio.run`` creates a fresh Task and a Task COPIES the current Context, so
    a ContextVar set inside one ``asyncio.run`` is invisible to the next call and
    to the caller. The production handoff works precisely because the gate, the
    turn-mode decision and the persist step run in the same task, so a test that
    called ``asyncio.run`` per step would be asserting against a topology this
    code never has.
    """
    return asyncio.run(fn())


def _warnings(fn) -> list[str]:
    """Run ``fn`` with a WARNING sink attached and return what it logged."""
    from loguru import logger

    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
    try:
        fn()
    finally:
        logger.remove(sink)
    return seen


@pytest.fixture(autouse=True)
def _clean_contextvars():
    """Every ContextVar this feature owns, reset around each test.

    They are process-wide within one asyncio context, and pytest gives every test
    the same one - a test that left ``chain_round`` at 1 would silently turn the
    next test's first question into ``chain_exhausted``. That is the same
    inheritance bug the production code sets them unconditionally to avoid.
    """

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


# ---------------------------------------------------------------------------
# Resolution: two knobs, one state
# ---------------------------------------------------------------------------


def test_ask_user_needs_the_conversation_surface(tmp_path) -> None:
    """Without a second turn the handoff has nowhere to land, so the feature
    resolves off however its own knob is written - and the tool is not admitted.

    [port] "not admitted" is the plugin's factory declining rather than an
    allowlist that never names it; the assertion is the same switch.
    """
    cfg = FlowConfig(enabled=True, ask_user={"enabled": True})
    assert cfg.ask_user_on is False
    assert _gates(_chain(cfg, tmp_path)) == []
    assert _contributed_ask_user(tmp_path, {"enabled": True, "askUser": {"enabled": True}}) is None
    assert "`ask_user`" not in render_identity_and_contract(cfg)[1]


def test_the_off_state_leaves_no_trace_on_the_assembly(tmp_path) -> None:
    """[port] The fork's four off-state fields are three surfaces here: the
    resolution, the chain, and the rendered prompt - which must come back
    byte-identical to the state the anchor was measured on."""
    cfg = FlowConfig(enabled=True)
    assert cfg.ask_user_on is False
    assert _gates(_chain(cfg, tmp_path)) == []
    assert TurnFrame(cfg, SessionStore(tmp_path), None)._ask_user_brief is False
    assert render_identity_and_contract(cfg) == render_parts(ask_user=False)


def test_the_on_state_registers_and_admits_the_tool_together(tmp_path) -> None:
    """C2 in one assertion: a prompt that names the tool and a surface that
    carries it are the same switch. What the other case costs is a hallucinated
    call to a tool never registered.

    [port] The switch is ``FlowConfig.ask_user_on``, and it is read in three
    places that must agree - the factory that contributes the tool, the chain
    that installs the gate holding it, and the renderer that names it.
    """
    slice_ = {"enabled": True, "askUser": {"enabled": True}, "conversation": {"enabled": True}}
    cfg = FlowConfig.from_slice(slice_)
    assert cfg.ask_user_on is True

    tool = _contributed_ask_user(tmp_path, slice_)
    assert tool is not None
    assert tool.name == "ask_user"

    gates = _gates(_chain(cfg, tmp_path, tools=ToolHandles(ask_user=tool)))
    assert len(gates) == 1
    assert gates[0]._tool is tool
    assert "`ask_user`" in _seg(ask_user=True)


def test_a_non_dr_loop_keeps_the_blocking_tool(tmp_path) -> None:
    """The gateway and TUI paths run without the research flow and must be
    unaffected.

    [port] There is no flow-off assembly to read: with the plugin's own switch
    off every factory declines, so nothing replaces the trunk's blocking tool -
    which is asserted here to still be the blocking one.
    """
    from raven.agent.tools.ask_user import AskUserTool

    off = {"enabled": False, "askUser": {"enabled": True}, "conversation": {"enabled": True}}
    assert _contributed_ask_user(tmp_path, off) is None
    assert AskUserTool.blocking_interaction is True
    assert AskUserTool().name == "ask_user" == DRAskUserTool().name


# ---------------------------------------------------------------------------
# Prompt bytes
# ---------------------------------------------------------------------------


def test_the_four_on_state_shas_are_pinned() -> None:
    """The acceptance artifact for this feature's prompt, same role the segment
    sha plays for every other clause.

    These are NOT pure appends to the off state, unlike the marker and report
    clauses: the identity is rewritten as well, which is exactly why the on state
    needs its own sha rather than a diff.

    [port] The same four values the fork stamped. The port is a text move, so a
    sha that moved would mean the text moved with it - which is the one thing the
    port may not do.
    """
    # mode x outline. ``when_needed`` keeps the bytes it had before the mode knob
    # existed, which is what makes it the byte-identical way back from the default.
    assert _sha(_seg(ask_user=True, ask_user_outline=True, ask_user_mode="when_needed")) == "f5ffda547fb5cff5"
    assert _sha(_seg(ask_user=True, ask_user_outline=False, ask_user_mode="when_needed")) == "1e45c78cb003dfa7"
    assert _sha(_seg(ask_user=True, ask_user_outline=True, ask_user_mode="first_turn")) == "2434cb68b0262b88"
    assert _sha(_seg(ask_user=True, ask_user_outline=False, ask_user_mode="first_turn")) == "49e85c6acad4460b"


def test_the_outline_ask_is_the_only_difference_between_the_two_on_states() -> None:
    with_outline = _seg(ask_user=True, ask_user_outline=True, ask_user_mode="when_needed")
    without = _seg(ask_user=True, ask_user_outline=False, ask_user_mode="when_needed")
    assert "`outline`" in with_outline
    assert "`outline`" not in without
    assert with_outline.replace(prompts._DR_ASK_USER_OUTLINE_ASK, "") == without


def test_every_new_prompt_line_stays_inside_the_files_wrap() -> None:
    """The identity is hand-wrapped at 80. A substitution that spans a line break
    is easy to get right by accident and wrong by accident, and a 190-char line
    is the visible symptom of a slot dropped mid-sentence."""
    off_lines = set(_seg(ask_user=False).splitlines())
    for outline in (True, False):
        new = [line for line in _seg(ask_user=True, ask_user_outline=outline).splitlines() if line not in off_lines]
        assert new, "the on state added no lines at all"
        assert max(len(line) for line in new) <= 80


def test_the_rewritten_bullet_keeps_its_hanging_indent() -> None:
    """The identity's bullets are hand-wrapped with a two-space hanging indent, and
    a substitution whose replacement carries its own newline has to reproduce it.
    A <=80 check passes a continuation line sitting at column 0, so the shape is
    asserted directly."""
    on = _seg(ask_user=True)
    lines = on.splitlines()
    bullet = next(i for i, line in enumerate(lines) if "Treat everything you retrieve" in line)
    for line in lines[bullet + 1 :]:
        if not line.strip():
            break
        assert line.startswith("  ") and not line.startswith("   "), repr(line)
    assert "Simply do not comply, and never" in on


def test_the_clause_exempts_the_asking_turn_from_the_answer_shape() -> None:
    """The identity says the questions are the whole reply; the contract's shape
    clauses say an answer marker and three sections are present "every time", and
    ``finalShape.reportReminder`` repeats the template at the END of the current
    user message - the highest-recency position in the prompt. Two of the three
    pull against a reply that is a question, so the clause states the exemption in
    its own words.

    Numberless on purpose: the clause numbers come from one ``enumerate`` over
    independently switchable clauses, and cross-references between them are
    forbidden for that reason - "clauses 6 and 7 do not apply" would be wrong in
    three of the four switch states."""
    for kw in (
        {},
        {"require_answer_marker": False},
        {"report_structure": False},
        {"require_answer_marker": False, "report_structure": False},
    ):
        text = _seg(ask_user=True, **kw)
        assert "the questions are the whole reply: no answer tags, no" in text
        # No clause number is quoted, in any state.
        for n in range(1, 10):
            assert f"clause {n}" not in text and f"rule {n}" not in text


# ---------------------------------------------------------------------------
# What the assembly warns about
# ---------------------------------------------------------------------------


def test_the_known_bad_reminder_combination_warns(tmp_path) -> None:
    """outline on + reportStructure on + reportReminder off: the stratum this
    feature writes into is exactly the one measured at 2/9 well-formed against
    8/14. Warn and let it run - "reminder x outline" is the ablation worth having,
    so refusing the combination would block the measurement that prices it."""
    bad = _cfg(
        ask_user={"enabled": True, "outline": True},
        final_shape={"report_structure": True, "report_reminder": False},
    )
    ok = _cfg(
        ask_user={"enabled": True, "outline": True},
        final_shape={"report_structure": True, "report_reminder": True},
    )
    seen = _warnings(lambda: (_chain(bad, tmp_path), _chain(ok, tmp_path)))

    assert any("reportReminder" in m for m in seen), seen
    assert len([m for m in seen if "reportReminder" in m]) == 1
    assert ok.final_shape.report_reminder is True


def test_a_contract_override_warns_that_the_clause_never_rendered(tmp_path) -> None:
    """``promptSectionOverride`` owns the whole contract, so none of the optional
    clauses render - while the tool is contributed anyway. That is the mismatch the
    clause exists to prevent, arriving through a knob that says nothing about
    ask_user. Warn, the way the fetch gate warns when its tool is absent: a rule
    that is a no-op reads downstream exactly like a rule that ran and did not help.
    """
    cfg = _cfg(prompt_section_override="# Contract\n\n1. Answer the question.")
    tool = DRAskUserTool()
    chain: list = []
    seen = _warnings(lambda: chain.extend(_chain(cfg, tmp_path, tools=ToolHandles(ask_user=tool))))

    assert any("promptSectionOverride" in m and "ask_user" in m for m in seen), seen
    # The tool is still admitted, so the model is not shown a name it cannot call -
    # only the clause asking for it is missing.
    gates = _gates(chain)
    assert len(gates) == 1
    assert gates[0]._tool is tool
    contract = render_identity_and_contract(cfg)[1]
    assert contract == "# Contract\n\n1. Answer the question."
    assert "Before your first search" not in contract
    assert "On the first turn of a conversation" not in contract


# ---------------------------------------------------------------------------
# Identity rewrite
# ---------------------------------------------------------------------------


def test_every_identity_substitution_still_matches() -> None:
    """``str.replace`` returns its input unchanged when it misses, so a stale
    entry here ships a prompt that says "no user" with the tool registered. Two of
    these span a line break, and the draft of this feature carried one that could
    never match for exactly that reason.
    """
    for old, _new in prompts._ASK_USER_IDENTITY_SUBS:
        assert old in prompts._DR_IDENTITY, f"substitution no longer matches: {old!r}"


def test_a_stale_substitution_raises_at_assembly() -> None:
    with pytest.raises(ValueError, match="no longer matches"):
        prompts._apply_ask_user_identity("an identity that says none of those things")


def test_an_identity_override_warns_instead_of_crashing_the_assembly() -> None:
    """Every ``old`` in the substitution list is a fact about ``_DR_IDENTITY``, and
    ``identityOverride`` is a documented knob that hands the text to the operator.
    Asserting against an override turns that knob into a render-time ValueError
    whose message points at the wrong file, while the contract override only
    warns. Same knob class, same treatment.

    [port] The product's own launcher takes this path on every boot: it seeds
    ``soul.md`` as ``identityOverride``, so a raise here would be a refusal to
    start rather than a bad prompt.
    """
    cfg = _cfg(identity_override="You are a research agent. Answer the question.")
    rendered: list = []
    seen = _warnings(lambda: rendered.extend(render_identity_and_contract(cfg)))

    assert any("identityOverride" in m for m in seen), seen
    assert cfg.ask_user_on is True
    identity, contract = rendered
    assert "You are a research agent. Answer the question." in identity
    # The clause still renders: the override owns the identity, not the contract.
    # Asserted on a sentence both mode variants carry, so this stays true of the
    # default rather than of one wording.
    assert "the questions are the whole reply" in contract


def test_the_identity_stops_declaring_there_is_no_user() -> None:
    on = _seg(ask_user=True)
    for gone in (
        "nobody to consult",
        "no user",
        "exactly two",
        "Nothing else exists here",
        "There is nobody to check with",
        "First line: the answer itself and nothing else. Then",
    ):
        assert gone not in on, f"on-state identity still says {gone!r}"


# ---------------------------------------------------------------------------
# Chain placement
# ---------------------------------------------------------------------------


def test_the_reviewer_and_the_bar_are_wrapped_only_when_ask_user_is_on(tmp_path) -> None:
    """The off-state is the object graph this build had before the feature: with
    the knob off there is no wrapper to reason about, which is the same guarantee
    ``GatedHook`` makes about ``conversation.enabled``.

    [port] The chain carries one hook the fork's observer list did not: the
    ``TurnFrame`` that does the turn-boundary work the fork's loop did, first and
    never wrapped, because it SETS the flag every wrap reads.
    """
    shape = {"report_structure": True, "report_bounce": True}
    off_gates = {"spin_breaker": {"enabled": False}, "fetch_floor": {"enabled": False}}
    provider = _StubProvider()
    on = _chain(
        _cfg(final_shape=shape, force_finalize={"enabled": True}, **off_gates),
        tmp_path,
        provider=provider,
    )
    assert [o.name for o in on] == [
        "TurnFrame",
        "Gated(BudgetNoteObserver)",
        "Gated(ClarifyExempt(ForcedFinalizeGate))",
        "Gated(ClarifyExempt(DraftReviewerGate))",
        "ClarifyExempt(ReportShapeGate)",
        "AskUserGate",
    ]
    off = _chain(
        FlowConfig(
            enabled=True,
            conversation={"enabled": True},
            final_shape=shape,
            force_finalize={"enabled": True},
            **off_gates,
        ),
        tmp_path,
        provider=provider,
    )
    assert [o.name for o in off] == [
        "TurnFrame",
        "Gated(BudgetNoteObserver)",
        "Gated(ForcedFinalizeGate)",
        "Gated(DraftReviewerGate)",
        "ReportShapeGate",
    ]


def test_the_off_state_assembles_no_gate(tmp_path) -> None:
    for cfg in (
        FlowConfig(enabled=True),
        FlowConfig(enabled=True, ask_user={"enabled": True}),  # no conversation
        FlowConfig(enabled=True, conversation={"enabled": True}),  # no askUser
    ):
        assert _gates(_chain(cfg, tmp_path)) == []


def test_the_gate_reads_its_bounds_from_config(tmp_path) -> None:
    cfg = _cfg(
        ask_user={
            "enabled": True,
            "max_rounds": 3,
            "first_iteration_only": False,
            "max_questions": 2,
            "max_outline_items": 1,
            "outline": False,
        },
    )
    gate = _gates(_chain(cfg, tmp_path))[0]
    assert (gate._max_rounds, gate._first_iteration_only) == (3, False)
    assert (gate._max_questions, gate._max_outline_items, gate._outline) == (2, 1, False)


def test_ask_user_behaves_the_same_under_both_conversation_gate_modes(tmp_path) -> None:
    """``conversation.gate`` is ``"always" | "agentic"``, and ``ConversationGate``
    is only CONSTRUCTED for ``agentic`` - under ``always``, the turn-mode decision
    returns ``TurnMode(True, "config_always")`` with no LLM call at all.

    The fourth draft's withdrawn design hung the reply/new-request verdict on that
    call, so it was undefined under ``always``. ``is_reply_to`` is a pure function,
    which is what makes the two modes identical here - and why no third startup
    warning is needed."""
    built = {}
    for mode in ("always", "agentic"):
        cfg = _cfg(conversation={"enabled": True, "gate": mode})
        chain = _chain(cfg, tmp_path, provider=_StubProvider())
        gate = _gates(chain)[0]
        built[mode] = (
            cfg.ask_user_on,
            cfg.ask_user.brief,
            gate._mode,
            gate._delivery,
            gate._max_rounds,
            gate._first_iteration_only,
        )
        # The turn frame holds the classifier, and only ``agentic`` has one.
        assert (chain[0]._gate is not None) == (mode == "agentic")
    assert built["always"] == built["agentic"]


def test_build_dr_flow_wires_one_instance_through_tool_gate_and_segment(tmp_path) -> None:
    """The gate's readiness check reads the CONTRIBUTED tool: a gate holding its
    own copy would grant a round trip on an instance no broker was ever bound
    to, and the model's call would fall through to the undelivered string."""
    cfg = _cfg(ask_user={"enabled": True, "delivery": "tool"})
    tool = DRAskUserTool(delivery="tool")
    gates = _gates(_chain(cfg, tmp_path, tools=ToolHandles(ask_user=tool)))
    assert tool.blocking_interaction is True
    assert len(gates) == 1
    assert gates[0]._tool is tool
    assert "The call returns their answers" in _flat(render_identity_and_contract(cfg)[1])


def test_two_config_keys_are_enough_from_a_fresh_config(tmp_path) -> None:
    """The question a first-time user asks, pinned so the README's answer stays
    true.

    [port] ``toolsAllowlist`` was not a third key in the fork either - the
    assembly widened it once the two switches were on - and on this build it is
    not a lock at all: the plugin does no slimming and the product shapes its
    tool face with ``tools.disabledTools``, which names nothing by default.
    """
    slice_ = {"enabled": True, "askUser": {"enabled": True}, "conversation": {"enabled": True}}
    cfg = FlowConfig.from_slice(slice_)
    assert cfg.ask_user_on is True
    assert _contributed_ask_user(tmp_path, slice_) is not None
    assert len(_gates(_chain(cfg, tmp_path))) == 1
    assert "`ask_user`" in render_identity_and_contract(cfg)[1]

    # One key alone is not enough, in either direction.
    for partial in (
        {"enabled": True, "askUser": {"enabled": True}},
        {"enabled": True, "conversation": {"enabled": True}},
    ):
        assert _contributed_ask_user(tmp_path, partial) is None
        assert FlowConfig.from_slice(partial).ask_user_on is False


# ---------------------------------------------------------------------------
# The brief and the memo at the turn's entry
# ---------------------------------------------------------------------------


def _record(**kw) -> SessionRecord:
    return SessionRecord(**kw)


def _pending(question=_ZH_QUESTION, original=_ZH_ORIGINAL, chain=1) -> dict:
    return PendingClarify(original_question=original, questions=[_q(question)], chain_round=chain).to_metadata()


def _frame(cfg: FlowConfig, tmp_path, *, gate=None) -> TurnFrame:
    return TurnFrame(cfg, SessionStore(tmp_path), gate)


# ---------------------------------------------------------------------------
# The pending round trip through the frame's two seams
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The answer turn is a research turn
# ---------------------------------------------------------------------------


def _iteration_ctx(question: str) -> AgentHookContext:
    """The iteration phase's own context, as the loop builds it: a fresh object
    whose ``messages`` carry the conversation so far and whose ``turn_base`` says
    where this turn starts."""
    return AgentHookContext(
        session_key="cli:t",
        iteration=1,
        turn_question=question,
        turn_base=2,
        messages=[
            {"role": "user", "content": "compare the two"},
            {"role": "assistant", "content": "which market? which year?"},
        ],
    )


# ---------------------------------------------------------------------------
# askUser.mode and askUser.delivery - what the clause ends up saying
# ---------------------------------------------------------------------------


def test_the_product_default_mandates_a_first_turn_round() -> None:
    """``mode="first_turn"`` is the default because ``enabled`` is False by default:
    the knob is only reachable from a product profile, so its default is a product
    decision and no benchmark arm can see either value."""
    assert AskUserConfig().mode == "first_turn"
    text = _seg(ask_user=True)  # i.e. the default
    assert "On the first turn of a conversation, before any search" in text
    assert "Ask even when the question looks complete" in text


def test_one_fixed_clause_states_both_regimes_rather_than_varying_per_turn() -> None:
    """dr@3.0 requires that a gate cut behaviour and never TEXT: the system prompt
    heads the cached prefix, so rendering one clause on turn one and another
    afterwards would re-bill the whole conversation at uncached rates (measured at
    4.3x when a provider slot silently disabled caching).

    So the clause carries both regimes and the model reads its own turn number off
    the history. The test is that the text does not depend on the turn - there is no
    per-turn input to the renderer at all - and that both regimes are stated.

    [port] Stronger here than in the fork, and for free: the product renders this
    text ONCE, into a workspace file, so a per-turn input could not be honoured
    even if one existed.
    """
    assert "turn" not in inspect.signature(render_parts).parameters
    assert list(inspect.signature(render_identity_and_contract).parameters) == ["cfg"]
    text = _seg(ask_user=True, ask_user_mode="first_turn")
    assert "On the first turn of a conversation" in text  # mandatory here
    assert "On later turns, ask only when answering well genuinely depends" in _flat(text)


def test_the_tool_delivery_keeps_the_answer_first_reply_rule() -> None:
    """Asking happens INSIDE the turn, so the identity's reply rule stays intact;
    rewriting it would contradict the clause's "never repeat the questions into
    your reply". The other substitutions still apply."""
    text = _seg(ask_user=True, ask_user_delivery="tool")
    assert "First line: the answer itself and nothing else." in text
    assert "If you are asking the user, the questions are the" not in text
    assert "You may ask the user once" in text
    assert "no shell, no files,\nno stored memory" in text


def test_unknown_config_keys_warn_and_declared_wiring_stays_silent():
    """[C9] A typo'd knob used to validate clean (``extra="ignore"`` with no
    voice); now the slice door names what it ignores. The wiring keys the
    plugin's tools read raw -- which FlowConfig deliberately does not model --
    must stay silent, or every healthy launch cries wolf.
    """
    from loguru import logger as _logger
    from research_flow.config import FlowConfig

    records: list[str] = []
    sink = _logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        FlowConfig.from_slice(
            {
                "enabled": True,
                "maxIterationz": 999,
                "finalShape": {"reportStructur": False},
                "stateRoot": "/tmp/x",
                "proxy": "socks5://127.0.0.1:1080",
                "fetch": {"apiKey": "k"},
                "search": {"apiKey": "k"},
                "contextWindowTokens": 100000,
                "identityOverride": "text",
            }
        )
    finally:
        _logger.remove(sink)
    text = "\n".join(records)
    assert "maxIterationz" in text, "a top-level typo must be named"
    assert "finalShape.reportStructur" in text, "a nested typo must be named with its path"
    for silent in ("stateRoot", "proxy", "search.apiKey", "identityOverride", "contextWindowTokens"):
        assert silent not in text, f"{silent} is declared wiring or a real knob, not a typo"


def test_a_mode_overlay_typo_is_refused_with_its_path():
    """A stricter door than the base slice's: an overlay carries knobs and nothing
    else, so a key the flow does not read is a typo that would run the base value
    under a mode label promising otherwise. Refused, naming the path, the way the
    vendored twin's ``extra="forbid"`` refuses at startup."""
    from research_flow.config import FlowConfig

    cfg = FlowConfig.from_slice({"enabled": True})
    with pytest.raises(ValueError, match="budgetNote.enabledd"):
        cfg.with_overlay({"budgetNote": {"enabledd": True}})
    # A real knob still merges, and null still means "back to the default".
    merged = cfg.with_overlay({"verify": {"model": None}, "budgetNote": {"enabled": False}})
    assert merged.verify.model is None and merged.budget_note.enabled is False
