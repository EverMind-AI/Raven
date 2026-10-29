"""The silence-marker protocol: which model replies mean "nothing to report", and which rule reads them."""

import pytest

from raven.agent.context.silence import (
    SILENCE_INSTRUCTION,
    SILENCE_MARKERS,
    SILENCE_MAX_CHARS,
    SILENT_MARKER,
    is_exact_silence,
    is_loose_silence,
    is_silence,
    teaches_silence,
)
from raven.spine import PROACTIVE_ORIGINS, Origin


def test_the_marker_set_and_its_length_cap_are_the_agreed_values():
    assert SILENT_MARKER == "[SILENT]"
    assert SILENCE_MARKERS == frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"})
    assert SILENCE_MAX_CHARS == 64


@pytest.mark.parametrize(
    "text", ["[SILENT]", "  silent \n", "NO_REPLY", "no reply", "*NO_REPLY*", ".NO_REPLY", "NO_REPLY."]
)
def test_an_exact_marker_is_silence(text):
    assert is_exact_silence(text)


@pytest.mark.parametrize(
    "text", ["", "   ", "[SILENT", "Reply NO_REPLY if you agree", "[SILENT] nothing new", "x" * 65, None, 5]
)
def test_anything_else_is_not_exact_silence(text):
    assert not is_exact_silence(text)


@pytest.mark.parametrize(
    "text", ["[SILENT] No changes detected", "2 deals filtered\n\n[SILENT]", "NO_REPLY\nnothing changed", "silent"]
)
def test_a_proactive_turn_may_wrap_the_marker(text):
    assert is_loose_silence(text)


@pytest.mark.parametrize(
    "text", ["I considered staying [SILENT] but here is the summary", "Silent retry succeeded", ""]
)
def test_a_marker_mid_sentence_is_a_report(text):
    assert not is_loose_silence(text)


def test_the_origin_and_the_lesson_pick_the_rule():
    assert is_silence("[SILENT] No changes", Origin.CRON, taught=True)
    assert is_silence("[SILENT] No changes", Origin.SUBAGENT, taught=True)
    assert not is_silence("[SILENT] No changes", Origin.USER, taught=True)
    assert not is_silence("[SILENT] No changes", None, taught=True)
    assert not is_silence("[SILENT] No changes", Origin.CRON, taught=False)
    assert is_silence("NO_REPLY", Origin.CRON, taught=True)


@pytest.mark.parametrize("taught", [False, True])
@pytest.mark.parametrize("origin", [Origin.USER, None])
@pytest.mark.parametrize("text", ["[SILENT]", "  [SILENT]\n"])
def test_a_persons_turn_honours_the_taught_marker(text, origin, taught):
    assert is_silence(text, origin, taught=taught)


@pytest.mark.parametrize("taught", [False, True])
@pytest.mark.parametrize("origin", [Origin.USER, None])
@pytest.mark.parametrize("text", ["Silent.", "silent", "No reply.", "NO_REPLY", "*NO_REPLY*", "[silent]", "[SILENT]."])
def test_a_persons_one_word_answer_is_not_silence(text, origin, taught):
    assert not is_silence(text, origin, taught=taught)


@pytest.mark.parametrize("origin", sorted(PROACTIVE_ORIGINS))
def test_every_taught_proactive_origin_reads_the_loose_rule(origin):
    assert is_silence("[SILENT] No changes", origin, taught=True)


@pytest.mark.parametrize("origin", sorted(PROACTIVE_ORIGINS))
@pytest.mark.parametrize(
    "text", ["No reply.", "silent", "NO_REPLY", "*NO_REPLY*", "Checked the queue.\n\nNo reply", "[SILENT] No changes"]
)
def test_an_untaught_proactive_turn_reads_the_rule_a_persons_does(text, origin):
    """Heartbeat, a sub-agent's result and a sentinel notice are never told the marker, so a reply
    that happens to be or end on one of its spellings is an answer, and only ``[SILENT]`` is not."""
    assert not is_silence(text, origin, taught=False)
    assert is_silence(" [SILENT]\n", origin, taught=False)


@pytest.mark.parametrize("origin", [Origin.CRON, Origin.SUBAGENT])
@pytest.mark.parametrize("text", ["*NO_REPLY*", ".NO_REPLY", "NO_REPLY."])
def test_a_proactive_turn_forgives_edge_punctuation_around_a_bare_marker(text, origin):
    assert is_silence(text, origin, taught=True)


def test_the_length_cap_ignores_edge_whitespace_but_not_edge_punctuation():
    at_cap = "." * 28 + "NO_REPLY" + "." * 28
    assert len(at_cap) == SILENCE_MAX_CHARS
    assert is_exact_silence(at_cap)
    assert not is_exact_silence("." + at_cap)
    assert is_exact_silence("\n" * 70 + at_cap + " " * 70)


def test_the_instruction_teaches_the_bracketed_marker():
    assert '"[SILENT]"' in SILENCE_INSTRUCTION and "Never combine [SILENT] with content" in SILENCE_INSTRUCTION


@pytest.mark.parametrize(
    ("prompt", "taught"),
    [
        (f"[Scheduled Task] Timer finished.\n\n{SILENCE_INSTRUCTION}", True),
        (SILENCE_INSTRUCTION, True),
        ("Summarize this naturally for the user.", False),
        ("Reply [SILENT] if there is nothing new.", False),
        ("", False),
        (None, False),
        ([{"type": "text", "text": SILENCE_INSTRUCTION}], False),
    ],
)
def test_a_prompt_teaches_the_marker_only_by_carrying_the_instruction(prompt, taught):
    assert teaches_silence(prompt) is taught
