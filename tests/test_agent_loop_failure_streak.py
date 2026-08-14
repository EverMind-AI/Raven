"""Tool-failure streak accounting (raven/agent/loop/main.py).

The loop injects a change-approach nudge when one tool fails the same way
several times running. Two things decide whether that nudge helps or hurts:
what counts as "the same failure", and whether the nudge text asserts a cause
it cannot know.
"""

from __future__ import annotations

from raven.agent.loop.failure_streak import failure_class, loop_break_nudge


def test_different_failures_from_one_tool_are_different_classes() -> None:
    """A model getting two different errors is adapting, not stuck.

    Keying the streak on the tool name alone counts those together and fires
    the nudge at a model that is working through a problem -- the opposite of
    what the nudge is for.
    """
    truncated = failure_class("Error: [truncated] Arguments for 'write_file' were cut off at 8192 tokens")
    schema = failure_class("Error: Invalid parameters for tool 'write_file': missing required field")
    missing = failure_class("Error: File not found: /tmp/nope.py")

    assert len({truncated, schema, missing}) == 3


def test_the_same_failure_twice_is_one_class() -> None:
    """The streak still has to fire on a genuinely repeated dead call."""
    first = failure_class("Error: File not found: /tmp/a.py")
    second = failure_class("Error: File not found: /tmp/b.py")

    assert first == second == "not_found"


def test_truncation_is_its_own_class() -> None:
    """Truncation is not a schema error even though validation is what failed.

    They arrive through the same code path and would otherwise share a class,
    which would let a truncated call and a genuine schema mistake accumulate
    into one streak.
    """
    assert failure_class("Error: [truncated] Arguments for 'write_file' were cut off") == "truncated"
    assert failure_class("Error: Invalid parameters for tool 'write_file': missing path") == "schema"


def test_nudge_does_not_assert_a_cause_it_cannot_know() -> None:
    """The nudge used to branch on guessed failure types.

    One of those branches told the model to re-examine the path -- advice that,
    given to a model whose arguments had been truncated, sends it looking for a
    mistake it did not make. The nudge points at the error text instead, which
    is the one account of the failure that is actually true.
    """
    text = loop_break_nudge("write_file", 2)

    assert "write_file" in text
    assert "2 times" in text
    assert "error text above" in text
    for guess in ("external dependency", "network/API/search", "file or path error", "EXACT path"):
        assert guess not in text


def test_a_malformed_arguments_failure_has_its_own_class() -> None:
    """`[invalid arguments]` is a distinct failure, not an uncategorised one.

    It reached the registry after this classifier was written, so it fell into
    `other` -- the same bucket as every unrelated error. A model alternating
    between malformed JSON and some other failure would then read as stuck on
    one thing, which is exactly the conflation this key exists to prevent.
    """
    malformed = failure_class("Error: [invalid arguments] The arguments for 'write_file' were not valid JSON")
    schema = failure_class("Error: Invalid parameters for tool 'write_file': missing required content")
    other = failure_class("Error: broker unavailable")

    assert malformed == "invalid_arguments"
    assert len({malformed, schema, other}) == 3
