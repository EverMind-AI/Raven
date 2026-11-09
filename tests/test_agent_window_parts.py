"""Tests for ``raven.agent.window.parts`` -- the window divided.

The parts are what a reader is told when they ask what is filling the context
window, so the split has to be the one the request actually had: the system
prompt is the request's system message, the tools are the schemas sent beside
it, the memory and the skills are the two segments the turn recalled and
routed, and the conversation is what the rest of the prompt is.
"""

from __future__ import annotations

from raven.agent.window.parts import (
    CONVERSATION,
    MEMORY,
    SKILLS,
    SYSTEM,
    TOOLS,
    measure_parts,
)
from raven.utils.tokens import estimate_prompt_tokens

# The turn scratch as the assembler builds it: the tag, then the memory and
# the skills, joined. The envelope carries the text it appended.
SCRATCH_TAG = "[Turn Scratch — recalled for this turn only, not conversation history]"
MEMORY_TEXT = "# Memory\n\nthe reader likes short answers"
SKILLS_TEXT = "# Skills\n\nuse the deck engine for a deck"


def test_the_system_prompt_is_the_system_message() -> None:
    parts = measure_parts([{"role": "system", "content": "you are raven, a terminal agent"}])

    assert parts[SYSTEM] > 0
    assert parts[TOOLS] == 0
    assert parts[CONVERSATION] == 0
    assert parts[MEMORY] == 0
    assert parts[SKILLS] == 0


def test_the_tools_are_the_schemas_sent_beside_the_messages() -> None:
    parts = measure_parts(
        [{"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "exec", "description": "run a command"}}],
    )

    assert parts[TOOLS] > 0
    assert parts[CONVERSATION] > 0
    assert parts[SYSTEM] == 0


def test_the_conversation_is_everything_that_is_not_the_system_prompt() -> None:
    parts = measure_parts(
        [
            {"role": "system", "content": "you are raven"},
            {"role": "user", "content": "hello there"},
            {"role": "assistant", "content": "hi"},
            {"role": "tool", "content": "a tool said something long enough to count"},
        ]
    )

    assert parts[SYSTEM] > 0
    assert parts[CONVERSATION] > parts[SYSTEM]
    assert parts[TOOLS] == 0


def test_the_memory_and_the_skills_are_the_segments_the_turn_scratch_carried() -> None:
    """What the assembler split the scratch into, measured as its own parts."""
    scratch = f"{SCRATCH_TAG}\n\n{MEMORY_TEXT}\n\n---\n\n{SKILLS_TEXT}"
    parts = measure_parts(
        [
            {"role": "system", "content": "you are raven"},
            {"role": "user", "content": f"hello there\n\n{scratch}", "_turn_scratch": scratch},
        ],
        None,
        {"memory": MEMORY_TEXT, "skills": SKILLS_TEXT},
    )

    assert parts[MEMORY] > 0
    assert parts[SKILLS] > 0
    assert parts[CONVERSATION] > 0
    assert parts[SYSTEM] > 0


def test_the_recalled_text_is_not_counted_twice() -> None:
    """The conversation is what the user said, not the memory and the skills as well."""
    scratch = f"{SCRATCH_TAG}\n\n{MEMORY_TEXT}\n\n---\n\n{SKILLS_TEXT}"
    with_recall = measure_parts(
        [
            {"role": "system", "content": "you are raven"},
            {"role": "user", "content": f"hello there\n\n{scratch}", "_turn_scratch": scratch},
        ],
        None,
        {"memory": MEMORY_TEXT, "skills": SKILLS_TEXT},
    )
    without_recall = measure_parts(
        [
            {"role": "system", "content": "you are raven"},
            {"role": "user", "content": "hello there"},
        ]
    )

    assert with_recall[CONVERSATION] < without_recall[CONVERSATION] + 30
    assert with_recall[CONVERSATION] < 40


def test_the_recalled_text_is_taken_out_of_the_system_prompt_too() -> None:
    """With ``context.cache.turn_scratch`` off the recalled text rides in the
    system prompt -- and it is still the memory, not the system prompt."""
    parts = measure_parts(
        [{"role": "system", "content": f"you are raven\n\n---\n\n{MEMORY_TEXT}"}],
        None,
        {"memory": MEMORY_TEXT},
    )

    assert parts[MEMORY] > 0
    assert parts[SYSTEM] > 0
    assert parts[CONVERSATION] == 0


def test_the_recalled_text_is_taken_out_of_a_message_sent_as_blocks() -> None:
    """A message carrying a picture is a list of blocks: the recalled text is cut
    out of its text block, the picture is left as it is, and a message with no
    content at all passes through."""
    picture = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    with_recall = measure_parts(
        [
            {"role": "user", "content": [{"type": "text", "text": f"look\n\n{MEMORY_TEXT}"}, picture]},
            {"role": "assistant", "content": None},
        ],
        None,
        {"memory": MEMORY_TEXT},
    )
    without_recall = measure_parts(
        [
            {"role": "user", "content": [{"type": "text", "text": "look\n\n"}, picture]},
            {"role": "assistant", "content": None},
        ]
    )

    assert with_recall[MEMORY] > 0
    assert with_recall[CONVERSATION] == without_recall[CONVERSATION]


def test_the_parts_add_up_to_the_prompt() -> None:
    """Each part is measured on its own, so the five come to the whole within
    a few tokens -- the estimator's own boundaries, not a hole in the account."""
    messages = [
        {"role": "system", "content": "you are raven"},
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi"},
    ]
    tools = [{"type": "function", "function": {"name": "exec", "description": "run a command"}}]
    parts = measure_parts(messages, tools, {"memory": MEMORY_TEXT})

    whole = estimate_prompt_tokens(messages) + estimate_prompt_tokens([], tools)
    assert abs(sum(parts.values()) - whole) <= 20


def test_an_empty_prompt_has_no_parts() -> None:
    assert measure_parts([]) == {SYSTEM: 0, TOOLS: 0, CONVERSATION: 0, MEMORY: 0, SKILLS: 0}
