"""dr@3.4-askuser: the pure text surface of the clarify round.

The half of ``ask_user`` that is a set of functions over strings: the argument
parse, the two renderers and the strip that pairs with one of them, the reply
predicate, the pending's own metadata round trip, and the ContextVars the gate
and the loop hand each other state through. ``AskUserGate``, ``ClarifyExemptHook``
and the DR tool are driven from ``test_agents_research_flow_ask_user.py``;
nothing here constructs one.

Two properties carry most of the weight.

**The rendered bytes are pinned.** ``render_handoff`` is the independent
variable of the stratum this feature was measured on - it is the text that lands
in history verbatim - so a template that drifts makes the reading
unattributable, and the shas are asserted the way a prompt segment's are.

**The scaffolding follows the question's language, and the English text is the
source.** The sentences are English message ids rendered through
``raven.i18n.t_in``; their Chinese variants live in the repo's one catalog,
``raven/i18n/zh.py``. So an assertion about a Chinese scaffolding line names the
English source and translates it, and the Chinese INPUT that exercises the
language switch is spelled as escapes - ``raven/i18n/`` is the only place in
this repo that carries CJK text.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.ask_user import (  # noqa: E402
    _BRIEF_ORIGINAL,
    _BRIEF_REPLIED,
    _HANDOFF_LEAD,
    _OUTLINE_LEAD,
    BRIEF_CLOSE,
    BRIEF_OPEN,
    PendingClarify,
    clarify_verdict,
    is_reply_to,
    own_text,
    parse_ask_user_args,
    render_brief,
    render_handoff,
    reply_overlap,
    scaffold_language,
    set_chain_round,
    set_clarify_verdict,
    set_first_turn,
    set_turn_brief,
    strip_brief,
    take_pending_clarify,
)

from raven.i18n import t_in  # noqa: E402


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


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _q(text="which entity?", options=()):
    return {"question": text, "options": list(options)}


def _scenario(fn):
    """Run a whole scenario inside ONE asyncio context.

    ``asyncio.run`` creates a fresh Task and a Task COPIES the current Context, so
    a ContextVar set inside one ``asyncio.run`` is invisible to the next call and
    to the caller. The production handoff works precisely because the gate and the
    loop's persist step run in the same task - the agent loop is awaited, never
    spawned - so a test that called ``asyncio.run`` per step would be asserting
    against a topology this code never has.
    """
    return asyncio.run(fn())


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_parse_caps_both_lists_and_drops_empty_entries() -> None:
    payload = parse_ask_user_args(
        {
            "questions": [_q("a"), {"question": "  "}, _q("b"), _q("c"), _q("d")],
            "outline": [
                {"goal": "g1", "evidence": "e"},
                {"goal": ""},
                "nope",
                {"goal": "g2"},
                {"goal": "g3"},
                {"goal": "g4"},
                {"goal": "g5"},
            ],
        },
        max_questions=3,
        max_outline_items=2,
    )
    assert [q["question"] for q in payload.questions] == ["a", "b", "c"]
    assert [o["goal"] for o in payload.outline] == ["g1", "g2"]


def test_parse_accepts_a_bare_string_question() -> None:
    """A model that passed a string asked a real question; refusing it on shape
    would spend the round trip and deliver nothing."""
    payload = parse_ask_user_args({"questions": ["which year?"]})
    assert payload.questions == [{"question": "which year?", "options": []}]


def test_parse_never_raises_on_garbage() -> None:
    """``CompositeHook`` swallows a raising hook into a silent no-op, so a parse
    that can raise is a feature that stops working with nothing in the log.

    ``{"questions": "which year?"}`` is the case worth naming: a str is iterable,
    so an ``or []`` guard reads it as one question PER CHARACTER, and every
    character is a non-empty str that the bare-string branch accepts. The result
    was a handoff asking twelve one-letter questions."""
    for bad in (
        None,
        "",
        [],
        3,
        {"questions": "not a list"},
        {"questions": [None, 7]},
        {"questions": {"a": 1}},
        {"outline": "goal"},
    ):
        payload = parse_ask_user_args(bad)
        assert payload.questions == []
        assert payload.outline == []


# ---------------------------------------------------------------------------
# Rendering - fixed bytes, because this text enters history
# ---------------------------------------------------------------------------


def test_the_handoff_template_is_pinned() -> None:
    """``render_handoff`` is the independent variable of the stratum dr@3.4
    measured at 2/9 well-formed against 8/14: it is the text that lands in
    history verbatim. A template that drifts makes the first reading
    unattributable, so the bytes are pinned like a prompt segment."""
    p = PendingClarify(
        questions=[
            _q("Which entity do you mean?", ("the phone maker", "the network business")),
            _q("Which fiscal year?"),
        ],
        outline=[{"goal": "settle the entity", "evidence": "annual report", "why": "the figure differs"}],
    )
    text = render_handoff(p)
    assert _sha(text) == "90e02c29d6005c04"
    assert text.startswith("I will start researching as soon as these are settled:")


def test_the_scaffolding_follows_the_question_language() -> None:
    """The handoff is the only prose here a user reads directly, and product
    traffic is largely Chinese. An English lead-in over Chinese questions is the
    visible tell of a machine-made reply; ASCII parentheses inside a Chinese line
    are the same tell one level down. The brief follows the same switch because a
    label in one language over a verbatim quotation in another invites the model
    to translate the quotation.

    The delimiters stay ASCII in both: they are what ``strip_brief`` and the
    persist step anchor on, and a localised anchor leaves the block unstripped -
    which means persisted, which means accumulating.

    The scaffolding sentences are English source text rendered through ``t_in``,
    so the assertions name the source and translate it rather than repeating the
    catalog's Chinese here."""
    q1 = "\u4f60\u6307\u7684\u662f\u624b\u673a\u4e1a\u52a1\u8fd8\u662f\u901a\u4fe1\u7f51\u7edc\u4e1a\u52a1\uff1f"
    zh = PendingClarify(
        original_question="\u54ea\u4e00\u5bb6\u8bfa\u57fa\u4e9a\u5b9e\u4f53\u7684\u8425\u6536\u66f4\u9ad8\uff1f",
        questions=[_q(q1, ("\u624b\u673a\u4e1a\u52a1", "\u901a\u4fe1\u7f51\u7edc\u4e1a\u52a1"))],
        outline=[{"goal": "\u786e\u8ba4\u4e3b\u4f53", "evidence": "\u5e74\u62a5", "why": "\u53e3\u5f84\u4e0d\u540c"}],
    )
    handoff = render_handoff(zh)
    assert _sha(handoff) == "251d09e64bc20cce"
    assert handoff.startswith(t_in("zh", _HANDOFF_LEAD))
    assert "I will start researching" not in handoff
    assert "\n1. \u786e\u8ba4\u4e3b\u4f53" in handoff  # goal only, no evidence/why
    reply = "\u624b\u673a\u4e1a\u52a1\uff0c2023 \u8d22\u5e74"
    brief = render_brief(zh, reply)
    assert brief.startswith(BRIEF_OPEN) and brief.endswith(BRIEF_CLOSE)
    assert t_in("zh", _BRIEF_ORIGINAL, q=zh.original_question) in brief
    assert t_in("zh", _BRIEF_REPLIED, reply=reply) in brief
    assert "Original question" not in brief


def test_the_language_switch_reads_word_counts_not_a_character_ratio() -> None:
    """The two mixed shapes that occur are indistinguishable by character ratio -
    an English question quoting one Chinese noun sits at 0.167 and a Chinese
    question quoting a Latin product name at 0.171 - because a Latin product name
    inflates the denominator exactly like Latin prose does. CJK ideograph count
    against Latin WORD count separates them."""
    assert scaffold_language("What is \u5c0f\u7ea2\u4e66's revenue") == "en"
    assert scaffold_language("OpenRouter \u7684 deepseek-v4-flash \u4ef7\u683c\u662f\u591a\u5c11") == "zh"
    assert scaffold_language("Compare \u534e\u4e3a and Apple revenue in 2023") == "en"
    assert scaffold_language("\u534e\u4e3a\u548c\u82f9\u679c 2023 revenue \u5bf9\u6bd4") == "zh"
    # No CJK at all, and the empty string, are English - the safe default, since
    # the questions themselves carry the language regardless.
    for neutral in ("", "What is the revenue?", "\u043a\u0430\u043a\u043e\u0439 \u0433\u043e\u0434"):
        assert scaffold_language(neutral) == "en"


def test_the_handoff_carries_no_report_headings() -> None:
    """This text becomes the next turn's history. A partial three-section report
    sitting there is a worse few-shot than none - the mechanism report_shape.py
    measured. It also must not read as a refusal, and it must not call the
    outline a plan (CONTEXT.md keeps that word for the search plan)."""
    text = render_handoff(PendingClarify(questions=[_q()], outline=[{"goal": "g", "evidence": "e"}]))
    for heading in ("## Answer", "## Findings", "## Limitations", "##"):
        assert heading not in text
    assert "plan" not in text.lower()
    assert "start researching" in text


def test_the_lead_in_does_not_depend_on_the_question_count() -> None:
    """One fixed sentence per language, not one per count: a template whose bytes
    vary with the payload is not a fixed template."""
    for question in ("q", "\u95ee\u9898"):
        heads = {
            render_handoff(
                PendingClarify(
                    original_question=question,
                    questions=[_q(f"{question}{i}") for i in range(n)],
                )
            ).splitlines()[0]
            for n in (1, 2, 3)
        }
        assert len(heads) == 1


def test_the_brief_transcribes_the_reply_and_asserts_nothing() -> None:
    """The one thing that caps the cost of a misread ``is_reply_to``. A block that
    paired each question with an inferred answer would render "never mind, look up
    something else" AS the answer to a question; a verbatim quote leaves a
    paragraph that visibly is not an answer."""
    p = PendingClarify(original_question="How many?", questions=[_q("which entity?"), _q("which year?")])
    brief = render_brief(p, "never mind, find me something else")
    assert brief.startswith(BRIEF_OPEN) and brief.endswith(BRIEF_CLOSE)
    assert "The user replied, verbatim: never mind, find me something else" in brief
    # Never a Q -> A pairing: the reply text appears exactly once, unattached.
    assert brief.count("never mind") == 1
    assert "which entity?" in brief and "which year?" in brief


def test_strip_brief_survives_not_being_the_outermost_prefix() -> None:
    """``find``, not ``startswith``. Which block is outermost is a fact about the
    ORDER of three call sites in the persist step, and a prefix match would fail
    silently the day that order changes - and an unstripped block is persisted and
    then accumulates."""
    brief = render_brief(PendingClarify(questions=[_q()]), "yes")
    assert strip_brief(f"{brief}\n\nthe user text") == "the user text"
    assert strip_brief(f"SOMETHING ELSE\n\n{brief}\n\ntail") == "SOMETHING ELSE\n\ntail"
    assert strip_brief("no brief here") == "no brief here"
    # Half a block is worse than either whole outcome, so an unterminated open
    # delimiter is left alone rather than cut at a guess.
    assert strip_brief(f"{BRIEF_OPEN}\nunterminated") == f"{BRIEF_OPEN}\nunterminated"


# ---------------------------------------------------------------------------
# is_reply_to - biased towards "answer", one veto
# ---------------------------------------------------------------------------


def test_a_short_message_is_always_read_as_an_answer() -> None:
    """ "option 2" shares almost nothing with the question and IS the answer. The
    asymmetry is the point: a new request read as an answer costs one stale
    paragraph, an answer read as a new request throws away everything the user
    just said and they cannot tell it happened."""
    p = PendingClarify(
        original_question="Which of the two Nokia entities had higher revenue in FY2023?",
        questions=[_q("Do you mean the phone maker or the network business?")],
    )
    for reply in ("2", "the second", "yes", "\u624b\u673a\u90a3\u5bb6"):
        assert is_reply_to(p, reply) is True


def test_the_veto_fires_on_cjk_and_not_on_latin_at_the_configured_threshold() -> None:
    """The measured property of a character-bigram rate, pinned so it is a stated
    fact rather than a surprise.

    Latin script shares function-word bigrams with any other Latin text, so its
    background rate sits near 0.18 - far above the configured 0.05 - while
    unrelated CJK scores 0. At the default the veto therefore fires on Chinese and
    effectively never on English. That IS the designed bias direction (an answer
    misread as a new request throws away everything the user said), but it means
    ``brief_requires_answer_check`` is close to a no-op on Latin traffic, which is
    why the rate itself is recorded for an offline re-fit."""
    en = PendingClarify(
        original_question="Which of the two Nokia entities had higher revenue?",
        questions=[_q("Do you mean the phone maker or the network business?")],
    )
    en_new = "Forget that. Summarise the 2024 EU battery regulation and its compliance deadlines."
    assert 0.10 < reply_overlap(en, en_new) < 0.30
    assert is_reply_to(en, en_new) is True  # not vetoed at 0.05
    assert is_reply_to(en, en_new, threshold=0.30) is False

    zh = PendingClarify(
        original_question="\u54ea\u4e00\u5bb6\u8bfa\u57fa\u4e9a\u5b9e\u4f53\u7684\u8425\u6536\u66f4\u9ad8\uff1f",
        questions=[
            _q("\u4f60\u6307\u7684\u662f\u624b\u673a\u4e1a\u52a1\u8fd8\u662f\u901a\u4fe1\u7f51\u7edc\u4e1a\u52a1\uff1f")
        ],
    )
    zh_new = "\u7b97\u4e86\uff0c\u5e2e\u6211\u67e5\u4e00\u4e0b\u6b27\u76df\u65b0\u7535\u6c60\u6cd5\u89c4\u7684\u5408\u89c4\u622a\u6b62\u65e5\u671f\u90fd\u6709\u54ea\u4e9b\u3002"
    assert reply_overlap(zh, zh_new) == 0.0
    assert is_reply_to(zh, zh_new) is False  # vetoed at 0.05
    assert (
        is_reply_to(
            zh,
            "\u624b\u673a\u4e1a\u52a1\uff0c2023 \u8d22\u5e74\uff0c\u53e6\u5916\u628a\u901a\u4fe1\u7f51\u7edc\u4e1a\u52a1\u7684\u6570\u5b57\u4e5f\u7ed9\u6211\u3002",
        )
        is True
    )


def test_a_long_message_that_echoes_the_question_is_an_answer() -> None:
    p = PendingClarify(
        original_question="Which entity had higher revenue?",
        questions=[_q("Do you mean the phone maker or the network business?")],
    )
    reply = "I mean the network business, not the phone maker - and please cover both revenue lines."
    assert is_reply_to(p, reply) is True


def test_the_threshold_is_the_ablation_knob() -> None:
    p = PendingClarify(
        original_question="Which entity had higher revenue?",
        questions=[_q("Do you mean the phone maker or the network business?")],
    )
    other = "Forget that. Summarise the 2024 EU battery regulation and its compliance deadlines."
    assert is_reply_to(p, other, threshold=0.0) is True
    assert is_reply_to(p, other, threshold=0.9) is False


def test_the_length_guard_exempts_a_short_message_from_the_veto_entirely() -> None:
    """Independent of the threshold: below 60% of the original question's length
    the message is an answer whatever it overlaps. "2" is the commonest answer
    shape and shares nothing with the question."""
    p = PendingClarify(
        original_question="Which of the two Nokia entities had higher revenue in FY2023?",
        questions=[_q("Do you mean the phone maker or the network business?")],
    )
    assert is_reply_to(p, "2", threshold=0.99) is True


def test_the_overlap_reference_carries_the_options_and_the_length_bar_does_not() -> None:
    """Two strings, two jobs. ``question_text`` is what was asked, so it sets how
    long a reply is expected to be; ``reference_text`` is what the user was handed,
    so it is the vocabulary a reply can reuse. Merging them would put the length
    bar at 60% of the whole option list, which no selection could clear."""
    p = PendingClarify(questions=[_q("how deep?", ("an overview", "the mechanics"))])
    assert p.question_text == "how deep?"
    assert "an overview" in p.reference_text and "the mechanics" in p.reference_text
    assert p.reference_text.startswith("how deep?")


def test_an_option_quoting_reply_is_an_answer_on_both_routes() -> None:
    """The two replies live traffic produced, one per exemption route.

    Both name option labels - which is what an option list asks for - and both
    scored 0.000 against the question stems alone, so both were read as new
    requests and the brief was never injected. Neither fix subsumes the other:
    the first clears on length, the second only on overlap.
    """
    depth_q = "\u4f60\u5e0c\u671b\u300c\u6df1\u5ea6\u5b66\u4e60\u300d\u7684\u89e3\u91ca\u6709\u591a\u6df1\uff1f"
    depth_options = (
        "\u5165\u95e8\u6982\u8ff0",
        "\u4e2d\u7b49\uff08\u6982\u5ff5 + \u4e0e\u673a\u5668\u5b66\u4e60\u7684\u5173\u7cfb\uff09",
        "\u6280\u672f\u7ec6\u8282\uff08\u67b6\u6784\u3001\u8bad\u7ec3\u8fc7\u7a0b\u3001\u6570\u5b66\u539f\u7406\uff09",
    )
    aspect_q = "\u4f60\u6700\u60f3\u641e\u6e05\u695a\u7684\u662f\u54ea\u4e00\u65b9\u9762\uff1f"
    aspect_options = (
        "\u5b83\u5230\u5e95\u662f\u4ec0\u4e48\u3001\u600e\u4e48\u8fd0\u4f5c",
        "\u5b83\u548c\u4eba\u5de5\u667a\u80fd\u3001\u673a\u5668\u5b66\u4e60\u662f\u4ec0\u4e48\u5173\u7cfb",
    )
    deep = PendingClarify(
        original_question="\u4ec0\u4e48\u662fdeep learning",
        questions=[_q(depth_q, depth_options), _q(aspect_q, aspect_options)],
    )
    reply = "1. \u6280\u672f\u7ec6\u8282\uff1b2. \u90fd\u9700\u8981\u3002 \u7ee7\u7eed"
    # Exempt on length: 18 characters against 60% of the stems. The bar used to be
    # 60% of ``original_question`` - 9.6 characters - so a short research question
    # closed the exemption a selection depends on.
    assert len(reply) <= 0.6 * len(deep.question_text)
    assert is_reply_to(deep, reply) is True

    which_q = "\u4f60\u95ee\u7684\u201cTransformer\u201d\u662f\u6307\u54ea\u4e00\u4e2a\uff1f"
    which_options = (
        "\u6df1\u5ea6\u5b66\u4e60\u4e2d\u7684 Transformer \u795e\u7ecf\u7f51\u7edc\u67b6\u6784",
        "\u7535\u5f71/\u52a8\u753b\u300a\u53d8\u5f62\u91d1\u521a\u300b",
        "\u7535\u529b\u8bbe\u5907\u53d8\u538b\u5668",
    )
    depth2_q = "\u4f60\u5e0c\u671b\u6211\u8bb2\u591a\u6df1\uff1f"
    depth2_options = (
        "\u5165\u95e8\u6982\u5ff5\uff1a\u5b83\u662f\u4ec0\u4e48\u3001\u4e3a\u4ec0\u4e48\u91cd\u8981",
        "\u6280\u672f\u539f\u7406\uff1a\u81ea\u6ce8\u610f\u529b\u3001\u591a\u5934\u6ce8\u610f\u529b\u3001\u4f4d\u7f6e\u7f16\u7801",
    )
    tf = PendingClarify(
        original_question="\u4ec0\u4e48\u662fTransformer",
        questions=[_q(which_q, which_options), _q(depth2_q, depth2_options)],
    )
    reply = "1. \u6df1\u5ea6\u5b66\u4e60; 2. \u90fd\u8981 3. \u7ee7\u7eed"
    # Not exempt on length, so this one rests entirely on the options being in the
    # reference: the stems alone score it 0.000 and the veto fires.
    assert len(reply) > 0.6 * len(tf.question_text)
    assert reply_overlap(tf, reply) > 0.2
    assert is_reply_to(tf, reply) is True
    stems_only = PendingClarify(
        original_question=tf.original_question,
        questions=[_q(q["question"]) for q in tf.questions],
    )
    assert reply_overlap(stems_only, reply) == 0.0
    assert is_reply_to(stems_only, reply) is False


# ---------------------------------------------------------------------------
# PendingClarify - what comes back off a session file
# ---------------------------------------------------------------------------


def test_an_outline_item_without_a_goal_is_not_an_outline_item() -> None:
    """``from_metadata`` reads a session FILE, and both renderers subscript ``goal``
    bare. A hand-edited or truncated entry took the whole turn down with a KeyError
    on the turn-entry path - the same class as ``options`` arriving as a string, and
    against this constructor's own promise never to raise."""
    p = PendingClarify.from_metadata(
        {
            "questions": [_q()],
            "outline": [{"evidence": "x"}, {"goal": "  "}, {"goal": "settle the entity"}],
        }
    )
    assert p.outline == [{"goal": "settle the entity"}]
    render_handoff(p)
    render_brief(p, "the network business")


def test_a_pending_with_no_question_is_not_a_pending() -> None:
    assert PendingClarify.from_metadata({"questions": []}) is None
    assert PendingClarify.from_metadata(None) is None
    assert PendingClarify.from_metadata({"questions": [_q()]}) is not None


def test_a_corrupt_pending_on_disk_does_not_fail_the_turn() -> None:
    """``from_metadata`` reads a session FILE. It runs on the turn-entry path, so a
    raise here fails the whole turn - for a field whose only job is a counter."""
    for bad in (
        {"questions": [_q()], "chain_round": "not a number"},
        {"questions": [_q()], "chain_round": None},
        {"questions": [_q()], "outline": "not a list"},
        {"questions": "not a list"},
    ):
        pending = PendingClarify.from_metadata(bad)
        if pending is not None:
            assert isinstance(pending.chain_round, int)
            assert isinstance(pending.outline, list)


# ---------------------------------------------------------------------------
# Regressions the first live acceptance run found
# ---------------------------------------------------------------------------


def test_the_question_is_the_users_own_words_not_the_injected_blocks() -> None:
    """``turn_question`` is captured at loop entry, i.e. AFTER assembly, so on a
    dr@3.4 product arm it carries the report reminder - ~250 characters of English
    appended to the user's message. Observed on the first live run: a Chinese
    question rendered an English lead-in.

    Three failures ride on this one string, which is why it is stripped rather
    than tolerated: the language switch counts the reminder's English words, the
    brief transcribes the reminder as part of "the original question", and
    ``is_reply_to``'s length guard scales off its length, so an inflated question
    makes every reply "short" and disables the veto entirely."""
    from research_flow.gates.conversation import MEMO_CLOSE, MEMO_OPEN
    from research_flow.gates.report_shape import render_reminder

    question = "\u5e2e\u6211\u67e5\u4e00\u4e0b\u6211\u4eec\u51e0\u4e2a\u4e3b\u8981\u7ade\u54c1\u6700\u8fd1\u7684\u5b9a\u4ef7\u7b56\u7565\uff0c\u505a\u4e2a\u5bf9\u6bd4\u3002"
    memo = f"{MEMO_OPEN}\nPages already opened:\n- https://example.com/a\n{MEMO_CLOSE}"
    brief = render_brief(PendingClarify(questions=[_q("earlier?")]), "yes")
    polluted = f"{brief}\n\n{memo}\n\n{question}\n\n{render_reminder()}"

    assert own_text(polluted) == question
    assert own_text(question) == question
    assert own_text("") == ""


# ---------------------------------------------------------------------------
# Findings from reviewing this change
# ---------------------------------------------------------------------------


def test_the_verdict_does_not_live_on_the_loop_instance() -> None:
    """``AgentLoop`` is a long-lived singleton serving every session of a gateway,
    and each turn runs in its own asyncio task. ``_RESEARCH_TURN``'s comment states
    the consequence: an attribute lets one conversation's follow-up switch off a
    research turn in another. A verdict on an attribute would stamp one
    conversation's ``pending_verdict`` onto another's trajectory.

    Asserted as an absence, because the defect is invisible in single-turn tests -
    which is every test in this file bar this one."""
    import asyncio as _asyncio

    from raven.agent.loop.main import AgentLoop

    assert not hasattr(AgentLoop, "_clarify_verdict")

    async def isolated(value):
        # A Task copies the context, so a set inside cannot leak out - the
        # property that makes this safe under concurrency.
        set_clarify_verdict(value, 0.5)
        return clarify_verdict()

    async def run():
        a, b = await _asyncio.gather(
            _asyncio.create_task(isolated("answered")),
            _asyncio.create_task(isolated("new_request")),
        )
        return a, b, clarify_verdict()

    a, b, outer = _scenario(run)
    assert a[0] == "answered" and b[0] == "new_request"
    assert outer is None


# ---------------------------------------------------------------------------
# Option shapes a generation can produce
# ---------------------------------------------------------------------------


def test_options_arriving_as_a_string_are_dropped_not_split_into_characters() -> None:
    """The bug that shipped: the ``isinstance(raw, list)`` guard was applied to the
    two TOP-LEVEL lists and not to this nested one, so ``options`` arriving as a str
    was read as one option per character.

    Observed on live traffic - a model emitted the remainder of its JSON as this
    field's value and the handoff rendered roughly seven hundred single-character
    bullets. The question text was well-formed, so the question survives and only
    the options are dropped."""
    payload = parse_ask_user_args(
        {
            "questions": [
                {"question": "which industry?", "options": ["SaaS", "retail"]},
                {"question": "which competitors?", "options": '\u6211\u76f4\u63a5\u5217\u51fa\u7ade"options":"..."'},
                {"question": "no options key at all"},
                {"question": "options is a dict", "options": {"a": 1}},
            ]
        },
        max_questions=4,
    )
    assert [len(q["options"]) for q in payload.questions] == [2, 0, 0, 0]
    assert [q["question"] for q in payload.questions][1] == "which competitors?"
    # And nothing single-character reaches the render.
    assert "\n   - \u6211\n" not in render_handoff(payload)


def test_the_option_list_is_bounded() -> None:
    """It bounds a RENDERING, and the render is what a user reads. A generation
    emitting sixty options is malformed however it got there; truncating is the only
    outcome that stays readable."""
    payload = parse_ask_user_args({"questions": [{"question": "q", "options": [f"o{i}" for i in range(40)]}]})
    assert len(payload.questions[0]["options"]) == 8
    assert render_handoff(payload).count("\n   - ") == 8


# ---------------------------------------------------------------------------
# The outline renders one scannable line per step
# ---------------------------------------------------------------------------


def test_a_long_outline_step_is_one_line_each() -> None:
    """The shape the trimming is for: the live run produced a step whose three
    clauses ran past a hundred characters on one line."""
    goal = "\u7ed9\u51fa\u8ba1\u7b97\u673a\u79d1\u5b66\u4e2d\u672c\u4f53\u8bba\u7684\u6838\u5fc3\u5185\u5bb9"
    p = PendingClarify(
        original_question="\u4ec0\u4e48\u662f ontology",
        questions=[_q("\u6307\u54ea\u4e2a\u9886\u57df\uff1f")],
        outline=[
            {
                "goal": goal,
                "evidence": "W3C \u6807\u51c6\uff08RDF\u3001OWL\uff09\u3001\u6743\u5a01\u8ba1\u7b97\u673a\u79d1\u5b66\u6765\u6e90",
                "why": "\u8fd9\u662f\u73b0\u4ee3\u6280\u672f\u8bed\u5883\u4e0b\u6700\u5e38\u89c1\u7684\u7528\u6cd5",
            }
        ],
    )
    text = render_handoff(p)
    body = text[text.index(t_in("zh", _OUTLINE_LEAD)) :]
    assert body.splitlines()[1:] == [f"1. {goal}"]
    assert max(len(line) for line in text.splitlines()) < 40
