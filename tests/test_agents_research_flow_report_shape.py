"""dr@3.4 report-shape reminder and bar.

Two halves, and the second one is why this file is not just unit tests of
``ReportShape``. The reminder is injected in one ``AgentLoop`` method and
stripped in another; if the strip ever misses, it is persisted into history and
every later turn carries a stale copy of an instruction that was meant to be
about the current one - and nothing about that failure is visible in the reply.
Same shape of risk as the research memo, tested the same way: against
``AgentLoop``'s own bound methods rather than a copy of them, because a copy of
the injector agrees with a copy of the stripper no matter what either does.

[port] The injector moved: the plugin's ``TurnFrame.before_user_inbound``
appends the reminder through ``HookDecision.modified_content``, so the
injection tests below drive that hook. The recovery-scaffolding strip still
lives in the trunk kernel's ``AgentLoop._save_turn`` and is tested against the
real bound method, as before.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.flow import TurnFrame  # noqa: E402
from research_flow.gates.report_shape import (  # noqa: E402
    REMINDER_CLOSE,
    REMINDER_OPEN,
    ReportShape,
    ReportShapeGate,
    render_reminder,
    strip_reminder,
)
from research_flow.state import SessionStore  # noqa: E402

from raven.agent.loop.main import AgentLoop  # noqa: E402
from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402
from raven.session.manager import Session  # noqa: E402

_GOOD = "## Answer\nyes\n\n## Findings\n- a source\n\n## Limitations\nnone material"


class _Response:
    def __init__(self, content, *, has_tool_calls=False, reasoning_content=None):
        self.content = content
        self.has_tool_calls = has_tool_calls
        self.reasoning_content = reasoning_content


def _ctx(content, **kw):
    return AgentHookContext(session_key="cli:t", response=_Response(content, **kw))


# --------------------------------------------------------------------------- #
# Reading a draft's shape                                                      #
# --------------------------------------------------------------------------- #


def test_the_three_sections_are_recognised():
    shape = ReportShape(_GOOD)
    assert shape.well_formed
    assert shape.missing == ()
    assert shape.in_order


def test_a_missing_section_is_named_not_just_counted():
    """The gate quotes them back to the model, so they have to be identifiable."""
    shape = ReportShape("## Answer\nyes\n\n## Findings\n- a source")
    assert not shape.well_formed
    assert shape.missing == ("Limitations",)


def test_an_annotated_heading_still_counts_as_the_section():
    """``## Findings (as a slide outline)`` is a real shape from these sessions.

    A reader loses nothing to it and a bounce costs a full generation, so it is
    recorded and shipped. This is the single most likely false positive in the
    whole mechanism - if it ever bounces, the gate is spending a generation on
    punctuation.
    """
    shape = ReportShape("## Answer\na\n\n## Findings (as a slide outline)\nb\n\n## Limitations\nc")
    assert shape.well_formed
    assert shape.annotated == ("Findings",)


@pytest.mark.parametrize(
    "heading",
    [
        "## 1. Answer",
        "## 2) Answer",
        "## **Answer**",
        "## __Answer__",
        "## \U0001f4cc Answer",
        "## - Answer",
        "## The Answer",
        "## Answer:",
        "## \u7b54\u6848 Answer",
        "## **1. Answer**",
    ],
)
def test_a_decorated_heading_still_counts_as_the_section(heading):
    """Numbering and bolding are what a model does to a template it was given.

    Reading them as an absent section would make the bar's measured price mostly
    the price of punctuation - and that price is the number that decides whether
    ``report_bounce`` ever ships on, so a false positive here corrupts the very
    data the knob is being held off to collect.
    """
    shape = ReportShape(f"{heading}\na\n\n## Findings\nb\n\n## Limitations\nc")
    assert shape.missing == (), heading
    assert "Answer" in shape.annotated, "the deviation is tolerated, not unrecorded"


@pytest.mark.parametrize("heading", ["## Key Findings", "## Findings and evidence"])
def test_the_section_name_need_not_start_the_heading(heading):
    assert ReportShape(f"## Answer\na\n\n{heading}\nb\n\n## Limitations\nc").missing == ()


def test_a_translated_heading_set_is_not_three_missing_sections():
    """Most product traffic here is Chinese and a model answering in Chinese
    translates its headings. Bouncing that is bouncing a whole language for the
    spelling of its headings - and it would show up in the knob's price as a
    constant tax rather than as the malformed-reply rate it is meant to measure.
    """
    shape = ReportShape(
        "## \u56de\u7b54\n\u662f\u7684\n\n## \u7814\u7a76\u53d1\u73b0\n- \u6765\u6e90\n\n## \u5c40\u9650\n\u65e0"
    )
    assert shape.missing == ()
    assert set(shape.annotated) == {"Answer", "Findings", "Limitations"}


def test_the_singular_form_counts_too():
    assert ReportShape("## Answer\na\n\n## Finding\nb\n\n## Limitation\nc").missing == ()


def test_an_undecorated_heading_is_not_recorded_as_annotated():
    """Otherwise the counter says every reply deviated and measures nothing."""
    assert ReportShape(_GOOD).annotated == ()


def test_a_setext_heading_is_not_accepted():
    """Deliberate: the clause asks for ``##`` by name, and underlined headings
    have never appeared in these sessions. Documented so the next reader knows
    it was decided rather than missed."""
    assert ReportShape("Answer\n======\nyes").missing == ("Answer", "Findings", "Limitations")


def test_a_heading_at_the_wrong_level_still_counts():
    shape = ReportShape("# Answer\na\n\n### Findings\nb\n\n## Limitations\nc")
    assert shape.well_formed
    assert set(shape.off_level) == {"Answer", "Findings"}


def test_sections_out_of_order_are_recorded_but_stay_well_formed():
    """Only a MISSING section costs the reader content, so only it is bounced."""
    shape = ReportShape("## Findings\nb\n\n## Answer\na\n\n## Limitations\nc")
    assert shape.well_formed
    assert shape.in_order is False


@pytest.mark.parametrize(
    "text,in_order",
    [
        ("## Answer\na\n\n## Findings and Limitations\nb", True),
        ("## \u7ed3\u8bba\u4e0e\u5c40\u9650\na\n\n## \u53d1\u73b0\nb", False),
    ],
)
def test_one_heading_can_satisfy_two_sections(text, in_order):
    """Decided, not overlooked - so the next reader does not "fix" it.

    A combined heading gives the reader both sections; bouncing it would spend a
    generation on a conjunction, which is the expensive side of the asymmetry
    this ruler is built around. The pair shares one heading index, so ``in_order``
    can read False on a layout nobody would call out of order - recorded, not
    bounced, same as every other tolerated deviation.
    """
    shape = ReportShape(text)
    assert shape.missing == ()
    assert shape.in_order is in_order


def test_an_outline_reply_is_the_failure_this_exists_for():
    shape = ReportShape("# PPT Outline\n\n## Slide 1 - what is H100\n- a bullet")
    assert not shape.well_formed
    assert shape.missing == ("Answer", "Findings", "Limitations")


def test_prose_that_merely_mentions_a_section_word_is_not_a_heading():
    """Heading-anchored, so a sentence about the answer is not a section."""
    assert ReportShape("The answer is 42. Findings follow. Limitations apply.").missing == (
        "Answer",
        "Findings",
        "Limitations",
    )


def test_a_heading_glued_mid_line_is_not_a_heading():
    """The failure the rewrite prompt's closing sentence guards against: one
    observed rewrite ran its preamble straight into ``## Answer`` on the same
    line, so the section shipped unsatisfied - mid-line ``##`` is not a markdown
    heading, for this parser or for the reader's renderer."""
    shape = ReportShape(
        "Let me rewrite with all three sections present## Answer\nyes\n\n"
        "## Findings\nbody\n\n## Limitations\nnone material"
    )
    assert shape.missing == ("Answer",)


def test_the_rewrite_prompt_forbids_preamble_and_pins_heading_position():
    """Both halves of the observed malformed rewrite: the acknowledgement
    preamble and the glued heading. Pinned so a rewording keeps the guards."""
    from research_flow.gates.report_shape import _REWRITE_PROMPT

    assert "begin it at `## Answer`" in _REWRITE_PROMPT
    assert "start of its own line" in _REWRITE_PROMPT
    assert "do not acknowledge" in _REWRITE_PROMPT


def test_a_stem_subheading_inside_findings_masks_a_missing_section():
    """Known behaviour, pinned rather than fixed: a ``###`` subheading whose label
    carries a section stem satisfies that section, so a reply that discusses its
    limits under a translated "limitations discussion" subheading inside Findings
    and never writes ``## Limitations`` still reads well-formed - no bounce,
    malformed ships.

    This is the tolerance the module was built around (any level, any
    decoration), one step further out; tightening it would re-price ``off_level``
    for every reply already tolerated. The deep report template
    (``finalShape.reportDepth``) invites ``###`` subheadings inside Findings, so
    this false-present gets MORE likely there - which is why an A/B over that
    knob must read ``off_level_headings`` alongside ``well_formed`` instead of
    taking the counter at face value.
    """
    shape = ReportShape(
        "## Answer\nyes\n\n## Findings\nbody\n\n### \u5c40\u9650\u6027\u8ba8\u8bba\nthe caveats live here"
    )
    assert shape.well_formed
    assert shape.missing == ()
    assert "Limitations" in shape.off_level


# --------------------------------------------------------------------------- #
# The reminder's delimiters                                                    #
# --------------------------------------------------------------------------- #


def test_the_reminder_round_trips_off_a_user_message():
    msg = f"what is H100?\n\n{render_reminder()}"
    assert strip_reminder(msg) == "what is H100?"


def test_stripping_a_message_that_never_carried_one_changes_nothing():
    assert strip_reminder("what is H100?") == "what is H100?"
    assert strip_reminder("") == ""


def test_a_half_block_is_left_alone_rather_than_half_cut():
    """Delimiter-exact for the reason ``strip_memo`` is: half a block persisted
    is worse than either whole outcome."""
    text = f"question\n\n{REMINDER_OPEN} truncated mid-block"
    assert strip_reminder(text) == text


def test_the_reminder_names_the_sections_and_the_override():
    """It restates the clause; if it ever stops agreeing with the template the
    model is being told two things."""
    block = render_reminder()
    assert block.startswith(REMINDER_OPEN) and block.rstrip().endswith(REMINDER_CLOSE)
    for section in ("## Answer", "## Findings", "## Limitations"):
        assert section in block
    assert "inside the sections" in block


# --------------------------------------------------------------------------- #
# The bar                                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_well_formed_draft_is_not_touched():
    decision = await ReportShapeGate().after_iteration(_ctx(_GOOD))
    assert decision.rollback is False
    assert decision.rollback_inject is None


@pytest.mark.asyncio
async def test_a_malformed_draft_bounces_once_carrying_the_draft_and_the_ask():
    ctx = _ctx("# PPT Outline\n## Slide 1\n- x")
    decision = await ReportShapeGate().after_iteration(ctx)
    assert decision.rollback is True
    roles = [m["role"] for m in decision.rollback_inject]
    assert roles == ["assistant", "user"]
    ask = decision.rollback_inject[1]["content"]
    assert "Answer, Findings, Limitations" in ask
    # The rewrite must not be an invitation to shorten: this gate sits one layer
    # above the lossy-extraction failure class the whole module is built against.
    assert "keeping every finding" in ask and "Do not shorten" in ask
    assert ctx.metadata["report_shape_gate"]["bounces"] == 1


@pytest.mark.asyncio
async def test_the_second_malformed_draft_ships_instead_of_bouncing_again():
    """Fail-open with a receipt: 'asked twice and still malformed' is the
    evidence that the prompt-side fix has run out of road, and it has to be
    distinguishable from 'never asked'."""
    gate = ReportShapeGate()
    ctx = _ctx("no headings here")
    assert (await gate.after_iteration(ctx)).rollback is True
    second = await gate.after_iteration(ctx)
    assert second.rollback is False
    assert ctx.metadata["report_shape_gate"]["shipped_malformed"] is True
    assert ctx.metadata["report_shape_gate"]["bounces"] == 1


@pytest.mark.asyncio
async def test_a_mid_research_iteration_is_not_a_draft():
    """Tool calls mean the turn is still working; only a terminal reply is shaped."""
    decision = await ReportShapeGate().after_iteration(_ctx("thinking...", has_tool_calls=True))
    assert decision.rollback is False


@pytest.mark.asyncio
async def test_an_empty_draft_belongs_to_the_finalize_gate_not_this_one():
    """A turn with nothing to say must not be asked to say it in three sections;
    ForcedFinalizeGate owns the answerless terminal and runs ahead of this."""
    assert (await ReportShapeGate().after_iteration(_ctx(""))).rollback is False
    assert (await ReportShapeGate().after_iteration(_ctx("<think>only reasoning"))).rollback is False


@pytest.mark.asyncio
async def test_the_bar_reads_the_visible_answer_not_the_reasoning():
    """A model that reasons about outlines and then writes the report must pass."""
    ctx = _ctx(f"<think>they asked for slides, but the template wins</think>{_GOOD}")
    assert (await ReportShapeGate().after_iteration(ctx)).rollback is False


@pytest.mark.asyncio
async def test_out_of_band_reasoning_does_not_erase_the_draft():
    """Same waiver the verify gate carries: on a channel-separated stack the
    content holds only answer text, so the closing-tag bar would blank it."""
    ctx = _ctx(_GOOD, reasoning_content="reasoned elsewhere")
    gate = ReportShapeGate(closing_tag_required=True)
    assert (await gate.after_iteration(ctx)).rollback is False
    assert ctx.metadata["report_shape_gate"]["missing_seen"] == ""


@pytest.mark.asyncio
async def test_the_reason_for_a_bounce_survives_the_trajectory_export():
    """Against ``terminal_state``, not against the raw metadata.

    A gate can only see what it wrote; whether the export kept it is a different
    fact. ``_scalar_snapshot`` drops non-scalars silently, which is how three
    ForcedFinalizeGate counters vanished and a downstream scorer never fired -
    and an assertion on ``ctx.metadata`` would pass in exactly that case.

    [port] The trunk kernel has no ``terminal_state`` trajectory export, so the
    raw namespace is the only observable left; the scalar-only shape the fork
    export required (a joined string, never a list) is still asserted.
    """
    ctx = _ctx("## Answer\na\n\n## Findings\nb")
    await ReportShapeGate().after_iteration(ctx)
    exported = ctx.metadata["report_shape_gate"]
    assert exported["bounces"] == 1
    assert exported["missing_seen"] == "Limitations"


@pytest.mark.asyncio
async def test_a_successful_rewrite_still_says_what_it_was_spent_on():
    """The field must survive the pass that clears the bar.

    ``missing_seen`` is the WHY behind a bounce, and a later pass reading a
    well-formed draft used to overwrite it with the empty set - blanking the
    record on exactly the bounces that worked, which is the population
    ``report_bounce`` has to be priced against.

    [port] Read from the gate's own namespace; the fork's export layer is gone.
    """
    gate = ReportShapeGate()
    ctx = _ctx("## Answer\na\n\n## Findings\nb")
    assert (await gate.after_iteration(ctx)).rollback is True
    ctx.response = _Response(_GOOD)
    assert (await gate.after_iteration(ctx)).rollback is False

    exported = ctx.metadata["report_shape_gate"]
    assert exported["missing_seen"] == "Limitations"
    assert exported["bounces"] == 1


@pytest.mark.asyncio
async def test_the_rewrites_shrink_is_measurable_after_the_draft_is_gone():
    """The rejected draft reaches no record - not the session (synthetic), not
    the journal (benchmarks run tracing off). Its size is the only thing left
    that can answer whether the rewrite dropped evidence, which is the failure
    class the whole module is built against and the one thing
    ``_REWRITE_PROMPT`` asserts without checking.

    [port] Read from the gate's own namespace; the fork's export layer is gone.
    """
    draft = "# PPT Outline\n" + "\n".join(f"- bullet {i}" for i in range(40))
    ctx = _ctx(draft)
    await ReportShapeGate().after_iteration(ctx)
    assert ctx.metadata["report_shape_gate"]["draft_chars"] == len(draft)


@pytest.mark.asyncio
async def test_a_quiet_bar_still_leaves_a_receipt():
    """ "Installed and every draft cleared it" must not read as "never installed".

    [port] The raw namespace keeps the empty ``missing_seen`` the fork's
    scalar-only export dropped; the receipt itself is the same.
    """
    ctx = _ctx(_GOOD)
    await ReportShapeGate().after_iteration(ctx)
    assert ctx.metadata["report_shape_gate"] == {"bounces": 0, "missing_seen": ""}


@pytest.mark.asyncio
async def test_the_bounce_scaffolding_never_becomes_history():
    """The rejected draft must not survive the turn.

    This module's premise is that an earlier outline in the history outweighs the
    clause in the system prompt. A bounce that persisted its own rejected draft
    would plant that few-shot itself - paying a generation to remove an outline
    from the reply and then storing it for every later turn.
    """
    ctx = _ctx("# PPT Outline\n## Slide 1\n- x")
    decision = await ReportShapeGate().after_iteration(ctx)
    assert all(m["_recovery_synthetic"] for m in decision.rollback_inject)

    session = Session(key="cli:t")
    messages = [{"role": "user", "content": "make me slides"}]
    messages += [dict(m) for m in decision.rollback_inject]
    messages.append({"role": "assistant", "content": _GOOD})
    _Loop().save(session, messages, 0)
    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert session.messages[-1]["content"] == _GOOD


# --------------------------------------------------------------------------- #
# Round trip through the real seams                                            #
# --------------------------------------------------------------------------- #


class _Loop:
    """Stands in for the trunk ``AgentLoop``; only the persist seam is exercised."""

    _TOOL_RESULT_MAX_CHARS = AgentLoop._TOOL_RESULT_MAX_CHARS

    def __init__(self):
        self._now_fn = datetime.now

    save = AgentLoop._save_turn


def _frame(tmp_path, *, report_reminder=True):
    """A ``TurnFrame`` resolved the way the assembly resolves it; only the
    reminder flag varies."""
    cfg = FlowConfig(enabled=True)
    cfg.final_shape.report_reminder = report_reminder
    return TurnFrame(cfg, SessionStore(tmp_path), None)


@pytest.mark.asyncio
async def test_the_reminder_reaches_the_model_and_not_the_session(tmp_path):
    """The whole mechanism in one assertion: the model sees it, history does not.

    Persisting it would accumulate - turn three would carry two stale reminders
    as history plus a fresh one - and each stale copy is an instruction about a
    question that is no longer being asked.

    [port] Only the first half is assertable here: the reminder rides
    ``HookDecision.modified_content`` out of ``TurnFrame.before_user_inbound``,
    and what the trunk loop persists is the trunk's seam, not the plugin's.
    """
    ctx = AgentHookContext(session_key="cli:t", inbound_content="what is H100?")
    decision = await _frame(tmp_path).before_user_inbound(ctx)
    assert decision.modified_content.rstrip().endswith(REMINDER_CLOSE), "the model must see it"
    assert decision.modified_content.startswith("what is H100?")


@pytest.mark.asyncio
async def test_the_reminder_is_absent_when_the_arm_did_not_ask_for_the_template(tmp_path):
    ctx = AgentHookContext(session_key="cli:t", inbound_content="what is H100?")
    decision = await _frame(tmp_path, report_reminder=False).before_user_inbound(ctx)
    assert decision.modified_content is None, "the inbound text must pass through untouched"
