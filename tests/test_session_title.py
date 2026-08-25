"""Cleanup rules for a model-answered session title."""

from __future__ import annotations

import pytest

from raven.session.title import (
    TITLE_BUDGET,
    TITLE_DISCARD_FACTOR,
    clean_model_title,
    collapse_to_line,
    extract_title,
)


class _Call:
    def __init__(self, arguments: object) -> None:
        self.arguments = arguments


class _Response:
    def __init__(self, tool_calls: list[_Call] | None = None, content: str | None = None) -> None:
        self.tool_calls = tool_calls or []
        self.content = content


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Fix the login redirect"', "Fix the login redirect"),
        ("Title: Fix the login redirect", "Fix the login redirect"),
        ("**Fix the login redirect**", "Fix the login redirect"),
        ('**"Fix the login redirect"**', "Fix the login redirect"),
        ("Fix the login redirect.", "Fix the login redirect"),
        ("Fix the\nlogin redirect", "Fix the login redirect"),
    ],
)
def test_recoverable_decoration_is_stripped_not_refused(raw: str, expected: str) -> None:
    """A label, wrapping quotes, emphasis or a trailing stop is formatting noise.

    Refusing these would throw away a title that is otherwise exactly right,
    which is the whole content of the answer.
    """
    assert clean_model_title(raw) == expected


def test_answer_within_budget_is_kept_whole() -> None:
    title = "x" * TITLE_BUDGET
    assert clean_model_title(title) == title


def test_answer_over_budget_is_clamped() -> None:
    assert clean_model_title("y" * (TITLE_BUDGET + 4)) == "y" * TITLE_BUDGET


def test_answer_past_the_discard_line_is_refused_rather_than_clamped() -> None:
    """Past the discard line the model plainly ignored the instruction.

    Clamping there would publish the opening fragment of a sentence as if it
    were a name; the caller's mechanical title is the better of the two.
    """
    assert clean_model_title("z" * (TITLE_BUDGET * TITLE_DISCARD_FACTOR + 1)) is None


@pytest.mark.parametrize("raw", ["", "   ", "\n\n", '""', "Title:"])
def test_answers_with_nothing_in_them_are_refused(raw: str) -> None:
    assert clean_model_title(raw) is None


def test_collapse_folds_every_run_of_whitespace() -> None:
    assert collapse_to_line("  a \n\t b   c  ") == "a b c"


def test_extract_reads_the_tool_call_arguments_as_json_string() -> None:
    assert extract_title(_Response([_Call('{"title": "Ship it"}')])) == "Ship it"


def test_extract_reads_the_tool_call_arguments_as_dict() -> None:
    assert extract_title(_Response([_Call({"title": "Ship it"})])) == "Ship it"


def test_extract_skips_a_call_whose_arguments_are_not_json() -> None:
    """One unparseable call must not hide a usable one behind it."""
    assert extract_title(_Response([_Call("not json"), _Call({"title": "Ship it"})])) == "Ship it"


def test_prose_is_not_salvaged_when_the_model_skipped_the_tool_call() -> None:
    """Preamble is invisible to the cleanup, which anchors its label at the start.

    "Sure! Here's a title: Fix the login redirect" is 43 codepoints, under the
    discard line, and would be published as "Sure! Here's a title: Fi".
    """
    assert extract_title(_Response(content="Sure! Here's a title: Fix the login redirect")) is None
    assert extract_title(_Response(content="Ship it")) is None


def test_extract_returns_none_when_there_is_nothing_to_read() -> None:
    assert extract_title(_Response(content="   ")) is None
