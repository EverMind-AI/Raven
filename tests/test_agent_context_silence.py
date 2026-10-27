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
    may_become_silence,
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


def test_the_origin_picks_the_rule():
    assert is_silence("[SILENT] No changes", Origin.CRON) and is_silence("[SILENT] No changes", Origin.SUBAGENT)
    assert not is_silence("[SILENT] No changes", Origin.USER) and not is_silence("[SILENT] No changes", None)
    assert is_silence("NO_REPLY", Origin.USER)


@pytest.mark.parametrize("origin", sorted(PROACTIVE_ORIGINS))
def test_every_proactive_origin_reads_the_loose_rule(origin):
    assert is_silence("[SILENT] No changes", origin)


@pytest.mark.parametrize("origin", [Origin.CRON, Origin.SUBAGENT])
@pytest.mark.parametrize("text", ["*NO_REPLY*", ".NO_REPLY", "NO_REPLY."])
def test_a_proactive_turn_forgives_edge_punctuation_around_a_bare_marker(text, origin):
    assert is_silence(text, origin)


@pytest.mark.parametrize(
    "text,held",
    [
        ("N", True),
        ("NO", True),
        ("NO_", True),
        ("[SIL", True),
        ("NO_REPLY", True),
        ("Hello", False),
        ("NOT", False),
        ("", False),
        ("N" * 65, False),
    ],
)
def test_a_prefix_is_held_only_while_it_can_still_be_a_marker(text, held):
    assert may_become_silence(text) is held


def test_the_length_cap_ignores_edge_whitespace_but_not_edge_punctuation():
    at_cap = "." * 28 + "NO_REPLY" + "." * 28
    assert len(at_cap) == SILENCE_MAX_CHARS
    assert is_exact_silence(at_cap) and may_become_silence(at_cap)
    assert not is_exact_silence("." + at_cap) and not may_become_silence("." + at_cap)
    assert is_exact_silence("\n" * 70 + at_cap + " " * 70)


def test_the_instruction_teaches_the_bracketed_marker():
    assert '"[SILENT]"' in SILENCE_INSTRUCTION and "Never combine [SILENT] with content" in SILENCE_INSTRUCTION
