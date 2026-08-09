"""Tests for raven.providers.reasoning -- recovering an orphaned </think>.

A backend run without a reasoning parser swallows the opening tag into its
prompt template, so the completion carries bare reasoning prose followed by a
lone closing tag and no opener to pair it with. ``split_orphan_think`` is the
one place that shape gets recognized and split; a paired block or no tag at
all must pass through untouched for the existing complete-block handlers.
"""

from __future__ import annotations

from raven.providers.reasoning import split_orphan_think


def test_orphan_close_tag_splits_reasoning_from_content():
    reasoning, content = split_orphan_think("raw reasoning text</think>\nfinal answer")

    assert reasoning == "raw reasoning text"
    assert content == "final answer"


def test_paired_complete_block_is_left_untouched():
    text = "<think>raw reasoning</think>\nfinal answer"

    assert split_orphan_think(text) == (None, text)


def test_no_closing_tag_is_left_untouched():
    text = "just a plain answer, no tags at all"

    assert split_orphan_think(text) == (None, text)


def test_orphan_thinking_variant_splits_too():
    reasoning, content = split_orphan_think("some reasoning</thinking>\nthe answer")

    assert reasoning == "some reasoning"
    assert content == "the answer"


def test_whitespace_only_prefix_strips_the_tag_without_a_reasoning_value():
    reasoning, content = split_orphan_think("   \n</think>\nthe answer")

    assert reasoning is None
    assert content == "the answer"


def test_content_after_the_tag_is_preserved_beyond_the_first_newline():
    reasoning, content = split_orphan_think("reasoning</think>\nline one\nline two")

    assert reasoning == "reasoning"
    assert content == "line one\nline two"


def test_only_a_single_leading_newline_is_dropped_from_content():
    reasoning, content = split_orphan_think("reasoning</think>\n\nblank line kept")

    assert reasoning == "reasoning"
    assert content == "\nblank line kept"


def test_content_without_a_leading_newline_is_unchanged():
    reasoning, content = split_orphan_think("reasoning</think>immediate answer")

    assert reasoning == "reasoning"
    assert content == "immediate answer"


def test_mismatched_think_open_thinking_close_is_left_untouched():
    text = "<think>raw reasoning</thinking>\nfinal answer"

    assert split_orphan_think(text) == (None, text)


def test_mismatched_thinking_open_think_close_is_left_untouched():
    text = "<thinking>raw reasoning</think>\nfinal answer"

    assert split_orphan_think(text) == (None, text)
