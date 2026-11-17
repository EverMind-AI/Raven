"""The runtime-context replay: the block, the skill menu and the turn scratch.

The provider's cache is a prefix cache: a turn's request is only reused when
the next turn sends the same bytes. Three pieces of the user message carry
per-turn text that the session write used to drop -- the runtime-context block,
the skill menu inside it, and the turn scratch at its end. Each is filed with
the message and rejoined on the way out, so a turn's user message replays
byte for byte.

These tests cover the pieces the rejoin is built from: how a recorded text is
located in a message (by what identifies it, never by position), how the block
and its menu are joined, and how the scratch is put back.
"""

from __future__ import annotations

from typing import Any

from raven.agent.context import ContextBuilder
from raven.context_engine.runtime_context import (
    RUNTIME_CONTEXT_FIELD,
    RUNTIME_CONTEXT_KEY,
    block_with_menu,
    locate,
    replayed_content,
)
from raven.context_engine.scratch import (
    TURN_SCRATCH_AT_FIELD,
    TURN_SCRATCH_FIELD,
    TURN_SCRATCH_KEY,
    TURN_SCRATCH_TAG,
    render_turn_scratch,
)

_TAG = ContextBuilder._RUNTIME_CONTEXT_TAG
_LINES = f"{_TAG}\nCurrent Time: now\nChat ID: x"
_MENU = "[BEGIN UNTRUSTED skill catalog #abc]\n- local/one: does a thing\n[END UNTRUSTED skill catalog #abc]"
_SCRATCH = render_turn_scratch(["# Memory\n\n- recalled: espresso"])


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [str(b.get("text", "")) for b in content or [] if isinstance(b, dict)]


class TestLocate:
    """A recorded text is found by what it is, not by where it sits."""

    def test_a_string_content_locates_the_text_it_holds(self) -> None:
        assert locate("the words\n\nand the block", "and the block") == ("string", 11)

    def test_a_string_content_without_the_text_locates_nothing(self) -> None:
        assert locate("the words", "and the block") is None

    def test_a_block_list_locates_the_block_holding_the_text(self) -> None:
        content = [
            {"type": "text", "text": "a recovery notice"},
            {"type": "text", "text": "and the block"},
            {"type": "text", "text": "the words"},
        ]

        assert locate(content, "and the block") == ("block", 1)

    def test_a_block_list_locates_the_first_block_holding_the_text(self) -> None:
        content = [
            {"type": "text", "text": "and the block"},
            {"type": "text", "text": "and the block"},
        ]

        assert locate(content, "and the block") == ("block", 0)

    def test_a_block_list_without_the_text_locates_nothing(self) -> None:
        content = [{"type": "text", "text": "the words"}]

        assert locate(content, "and the block") is None

    def test_a_block_that_is_not_text_is_skipped(self) -> None:
        content = [{"type": "image_url", "image_url": {"url": "data:..."}}, {"type": "text", "text": "the block"}]

        assert locate(content, "the block") == ("block", 1)

    def test_content_that_is_neither_shape_locates_nothing(self) -> None:
        assert locate(None, "the block") is None
        assert locate(42, "the block") is None


class TestBlockWithMenu:
    """The block the envelope opens with is the lines, then the menu."""

    def test_without_a_menu_the_block_is_the_lines(self) -> None:
        assert block_with_menu(_LINES, None) == _LINES

    def test_with_a_menu_the_block_is_the_lines_and_the_menu(self) -> None:
        assert block_with_menu(_LINES, _MENU) == f"{_LINES}\n{_MENU}"


class TestTheBlockIsFiledWhole:
    """The filed block is what the model saw: the lines and the menu."""

    def test_a_message_with_a_menu_replays_with_the_menu(self) -> None:
        message = {
            "role": "user",
            "content": "the words",
            RUNTIME_CONTEXT_FIELD: block_with_menu(_LINES, _MENU),
        }

        assert replayed_content(message) == f"{block_with_menu(_LINES, _MENU)}\n\nthe words"

    def test_a_message_without_a_menu_replays_with_the_lines_only(self) -> None:
        message = {"role": "user", "content": "the words", RUNTIME_CONTEXT_FIELD: _LINES}

        assert replayed_content(message) == f"{_LINES}\n\nthe words"

    def test_a_message_filed_before_this_change_replays_as_it_did(self) -> None:
        message = {"role": "user", "content": "the words"}

        assert replayed_content(message) == "the words"

    def test_a_block_still_in_the_content_is_not_joined_a_second_time(self) -> None:
        """A notice put in front of the message after assembly left the block in
        the content, and the strip above kept it there. The replay must not add
        it again."""
        message = {
            "role": "user",
            "content": f"[Recovery -- the previous turn was interrupted]\n\n{_LINES}\n\nthe words",
            RUNTIME_CONTEXT_FIELD: _LINES,
        }

        assert replayed_content(message) == message["content"]


class TestTheScratchIsFiledAndRejoined:
    """The scratch rides at the end of the user message and is filed with it."""

    def test_a_message_with_a_scratch_replays_with_the_scratch_at_the_end(self) -> None:
        message = {
            "role": "user",
            "content": "the words",
            RUNTIME_CONTEXT_FIELD: _LINES,
            TURN_SCRATCH_FIELD: _SCRATCH,
        }

        replayed = replayed_content(message)

        assert replayed == f"{_LINES}\n\nthe words\n\n{_SCRATCH}"
        assert replayed.endswith(_SCRATCH)

    def test_a_block_list_replays_with_the_scratch_as_its_last_block(self) -> None:
        message = {
            "role": "user",
            "content": [{"type": "text", "text": "the words"}],
            RUNTIME_CONTEXT_FIELD: _LINES,
            TURN_SCRATCH_FIELD: _SCRATCH,
        }

        replayed = replayed_content(message)

        assert isinstance(replayed, list)
        assert _texts(replayed)[0] == _LINES
        assert _texts(replayed)[-1] == _SCRATCH

    def test_a_message_without_a_scratch_replays_without_one(self) -> None:
        message = {"role": "user", "content": "the words", RUNTIME_CONTEXT_FIELD: _LINES}

        assert replayed_content(message) == f"{_LINES}\n\nthe words"

    def test_the_scratch_is_found_by_its_text_wherever_it_sits(self) -> None:
        """A notice put in front of the message moves the block off the start;
        the scratch is still the recorded text and is rejoined as it was."""
        message = {
            "role": "user",
            "content": [{"type": "text", "text": "a recovery notice"}, {"type": "text", "text": "the words"}],
            RUNTIME_CONTEXT_FIELD: _LINES,
            TURN_SCRATCH_FIELD: _SCRATCH,
        }

        replayed = replayed_content(message)

        assert isinstance(replayed, list)
        assert _texts(replayed)[-1] == _SCRATCH

    def test_a_block_list_that_does_not_hold_the_scratch_is_left_alone(self) -> None:
        """The record names a scratch the content no longer holds, so the content
        comes back as it is rather than gaining a block."""
        from raven.context_engine.scratch import split_turn_scratch

        content = [{"type": "text", "text": "the words"}]

        assert split_turn_scratch(content, _SCRATCH) == (content, None)

    def test_a_block_list_holding_nothing_else_after_the_cut_is_empty(self) -> None:
        """Only the scratch block: nothing is left, which is the caller's cue to
        skip the message."""
        from raven.context_engine.scratch import split_turn_scratch

        assert split_turn_scratch([{"type": "text", "text": _SCRATCH}], _SCRATCH) == (None, 0)

    def test_a_block_list_gets_the_scratch_back_at_its_index(self) -> None:
        message = {
            "role": "user",
            "content": [{"type": "text", "text": "the words"}],
            RUNTIME_CONTEXT_FIELD: _LINES,
            TURN_SCRATCH_FIELD: _SCRATCH,
            TURN_SCRATCH_AT_FIELD: 1,
        }

        replayed = replayed_content(message)

        assert replayed == [
            {"type": "text", "text": _LINES},
            {"type": "text", "text": "the words"},
            {"type": "text", "text": _SCRATCH},
        ]

    def test_a_note_kept_after_the_scratch_keeps_its_place(self) -> None:
        """A hook note appended after the scratch stays where it was, so the
        scratch goes back before it, not after it."""
        note = "a hook note"
        sent = f"the words\n\n{_SCRATCH}\n\n{note}"
        message = {
            "role": "user",
            "content": f"the words\n\n{note}",
            RUNTIME_CONTEXT_FIELD: _LINES,
            TURN_SCRATCH_FIELD: _SCRATCH,
            TURN_SCRATCH_AT_FIELD: len("the words"),
        }

        replayed = replayed_content(message)

        assert replayed == f"{_LINES}\n\n{sent}"
        assert replayed.index(_SCRATCH) < replayed.index(note)


class TestTheScratchTag:
    """The tag says what the scratch now is: the recall of the turn it was asked in."""

    def test_the_tag_does_not_claim_the_scratch_is_outside_the_history(self) -> None:
        assert "not conversation history" not in TURN_SCRATCH_TAG

    def test_the_tag_names_the_turn_the_recall_belongs_to(self) -> None:
        assert "the turn it was asked in" in TURN_SCRATCH_TAG


class TestThePrivateKeys:
    """The private keys ride on the live message and never reach the session."""

    def test_the_live_keys_are_private(self) -> None:
        assert RUNTIME_CONTEXT_KEY.startswith("_")
        assert TURN_SCRATCH_KEY.startswith("_")

    def test_the_filed_fields_are_public(self) -> None:
        assert not RUNTIME_CONTEXT_FIELD.startswith("_")
        assert not TURN_SCRATCH_FIELD.startswith("_")
