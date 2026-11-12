"""Tests for ``raven.agent.window.parts`` -- the window divided.

The parts are what a reader is told when they ask what is filling the context
window, so the split has to be the one the request actually had: the system
prompt is the request's system message, the tools are the schemas sent beside
it, the memory is the recalled segment, the skills are every byte the skill
subsystem put in the prompt (a resident digest, a routed segment, a pull menu,
a skill tool's result), and the conversation is what the rest of the prompt
is.
"""

from __future__ import annotations

import pytest

from raven.agent.window.parts import (
    ATTACHMENT_NOTES_KEY,
    CONVERSATION,
    FILES,
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
    assert measure_parts([]) == {SYSTEM: 0, TOOLS: 0, CONVERSATION: 0, FILES: 0, MEMORY: 0, SKILLS: 0}


# The always-on digest a pull-mode turn carries in its system prompt, and the
# menu the same discovery adds to the user envelope.
ACTIVE_SKILLS_TEXT = "# Active Skills\n\n### Skill: deck-engine\n\nBuild a deck from an outline."
SCENT_TEXT = "Possibly relevant skills (matched for: 'deck'):\n- local/deck-engine: build a deck"


def test_the_always_on_digest_is_skill_bytes() -> None:
    """Pull discovery builds no routed segment; the resident digest rides the
    system prompt, and it is skill material rather than the system's own words."""
    messages = [
        {"role": "system", "content": f"you are raven\n\n---\n\n{ACTIVE_SKILLS_TEXT}"},
        {"role": "user", "content": "make a deck"},
    ]
    without = measure_parts(messages)
    parts = measure_parts(messages, None, {"active_skills": ACTIVE_SKILLS_TEXT})

    assert without[SKILLS] == 0
    assert parts[SKILLS] > 0
    assert parts[SYSTEM] < without[SYSTEM]
    assert parts[CONVERSATION] == without[CONVERSATION]


def test_the_pull_menu_is_skill_bytes() -> None:
    """The scent menu is pull discovery's per-turn skill material, so it is the
    skills row's, not the conversation's."""
    messages = [{"role": "user", "content": f"make a deck\n{SCENT_TEXT}"}]
    without = measure_parts(messages)
    parts = measure_parts(messages, None, {"scent": SCENT_TEXT})

    assert parts[SKILLS] > 0
    assert parts[CONVERSATION] < without[CONVERSATION]


SKILL_BODY = "### Skill: deck-engine\n\n" + "Build the deck from the outline. " * 40


@pytest.mark.parametrize("tool", ["find_skill", "read_skill", "use_skill"])
def test_a_skill_tool_result_is_skill_bytes(tool: str) -> None:
    """A skill's body reaches the request as an ordinary tool message; read as
    conversation, the cost of a skill would be put on the talk that used it."""
    messages = [
        {"role": "user", "content": "make a deck"},
        {"role": "tool", "tool_call_id": "c1", "name": tool, "content": SKILL_BODY},
    ]
    as_talk = measure_parts([messages[0], {**messages[1], "name": "exec"}])
    parts = measure_parts(messages)

    assert as_talk[SKILLS] == 0
    assert parts[SKILLS] > 0
    assert parts[CONVERSATION] < as_talk[CONVERSATION]


def test_a_tool_result_that_is_not_a_skill_stays_conversation() -> None:
    messages = [
        {"role": "user", "content": "make a deck"},
        {"role": "tool", "tool_call_id": "c1", "name": "exec", "content": "a command said this"},
    ]

    assert measure_parts(messages)[SKILLS] == 0


@pytest.mark.parametrize("name", [["read_skill"], {"tool": "read_skill"}, 3], ids=["list", "dict", "int"])
def test_a_tool_message_whose_name_cannot_be_read_is_not_skill_bytes(name) -> None:
    """A replay hands recorded shapes back through this split, so a ``name`` that
    is not a string is no skill result -- and never an exception after a turn
    has already answered."""
    parts = measure_parts([{"role": "tool", "tool_call_id": "c1", "name": name, "content": "x"}])

    assert parts[SKILLS] == 0


# A recorded segment only answers for a part when the prompt really carried it:
# the sum closes when every byte moved out of one bucket was there to move.
# These are the routes this split cuts material out of.
SUM_CASES = [
    pytest.param(
        [
            {"role": "system", "content": f"you are raven\n\n---\n\n{ACTIVE_SKILLS_TEXT}"},
            {"role": "user", "content": "make a deck"},
        ],
        None,
        {"active_skills": ACTIVE_SKILLS_TEXT},
        id="resident-digest-in-the-system-prompt",
    ),
    pytest.param(
        [{"role": "user", "content": f"make a deck\n{SCENT_TEXT}"}],
        [{"type": "function", "function": {"name": "exec", "description": "run a command"}}],
        {"scent": SCENT_TEXT},
        id="pull-menu-in-the-user-envelope",
    ),
    pytest.param(
        [
            {"role": "user", "content": "make a deck"},
            {"role": "tool", "tool_call_id": "c1", "name": "read_skill", "content": SKILL_BODY},
        ],
        None,
        None,
        id="skill-tool-result",
    ),
    pytest.param(
        [
            {"role": "system", "content": "you are raven"},
            {"role": "user", "content": f"hello there\n\n{SCRATCH_TAG}\n\n{MEMORY_TEXT}\n\n---\n\n{SKILLS_TEXT}"},
        ],
        None,
        {"memory": MEMORY_TEXT, "skills": SKILLS_TEXT},
        id="turn-scratch-memory-and-routing-segment",
    ),
]


@pytest.mark.parametrize(("messages", "tools", "window_parts"), SUM_CASES)
def test_the_parts_add_up_when_the_recorded_text_is_in_the_prompt(messages, tools, window_parts) -> None:
    """The five parts come to the whole within the estimator's own boundaries.
    Each case pins one of the routes this split moves bytes out of, with the
    recorded text actually in the prompt it measures."""
    parts = measure_parts(messages, tools, window_parts)

    whole = estimate_prompt_tokens(messages) + estimate_prompt_tokens([], tools)
    assert abs(sum(parts.values()) - whole) <= 5


def test_the_sum_guard_catches_a_recorded_segment_the_prompt_does_not_carry() -> None:
    """The control for the guard above: a segment handed over by name but never
    written into the prompt moves the same bytes twice, so the sum drifts far
    past the bound and the guard has something to catch."""
    messages = [{"role": "user", "content": "hello there"}]
    parts = measure_parts(messages, None, {"scent": SCENT_TEXT})

    whole = estimate_prompt_tokens(messages)
    assert abs(sum(parts.values()) - whole) > 5


# The files a request carries: the attachment notes the assembler recorded on
# the message, and the pictures sent as image blocks. Both are taken out of
# the conversation and measured on their own content.


def test_the_notes_recorded_on_a_message_are_the_files_not_the_conversation() -> None:
    """What the assembler recorded as attachment notes is counted as files.

    The notes are cut from the conversation by the recorded text -- a
    note-shaped line the user typed, with no record on the message, stays
    conversation.
    """
    note = "[Attachment: report.pdf (path: /tmp/report.pdf) - use the read_file tool to read its contents]"
    parts = measure_parts(
        [
            {"role": "system", "content": "you are raven"},
            {"role": "user", "content": f"summarize this\n\n{note}", ATTACHMENT_NOTES_KEY: note},
        ]
    )

    assert parts[FILES] > 0
    assert parts[CONVERSATION] > 0
    # The note is out of the conversation: what is left is the user's words.
    assert parts[CONVERSATION] < estimate_prompt_tokens([{"role": "user", "content": "summarize this"}]) + 30


def test_a_note_shaped_line_without_a_record_stays_conversation() -> None:
    """Cutting by the ``[Attachment:`` shape would take the user's own words."""
    typed = "the log said [Attachment: fake.pdf (path: /x)] and I pasted it"
    parts = measure_parts([{"role": "user", "content": typed}])

    assert parts[FILES] == 0
    assert parts[CONVERSATION] == estimate_prompt_tokens([{"role": "user", "content": typed}])


def test_a_picture_is_files_not_conversation() -> None:
    """An image block is counted on its own content and dropped from the talk."""
    picture = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    note = "[Image: cat.png (path: /tmp/cat.png) | 64x64px - re-read it with read_file if you need another look]"
    with_picture = measure_parts(
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": f"look\n\n{note}"}, picture],
                ATTACHMENT_NOTES_KEY: note,
            }
        ]
    )
    without = measure_parts([{"role": "user", "content": "look"}])

    assert with_picture[FILES] > 0
    # The note is cut and the block dropped: the talk is the user's word alone.
    assert with_picture[CONVERSATION] <= without[CONVERSATION] + 5


def test_the_parts_still_add_up_when_files_are_in_the_prompt() -> None:
    """Files are measured on their own content, so the account still closes."""
    note = "[Attachment: report.pdf (path: /tmp/report.pdf)]"
    picture = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    messages = [
        {"role": "system", "content": "you are raven"},
        {
            "role": "user",
            "content": [{"type": "text", "text": f"look\n\n{note}"}, picture],
            ATTACHMENT_NOTES_KEY: note,
        },
    ]
    parts = measure_parts(messages)

    whole = estimate_prompt_tokens(messages)
    assert parts[FILES] > 0
    assert abs(sum(parts.values()) - whole) <= 20


def test_a_block_message_with_no_picture_is_left_whole() -> None:
    """Blocks are the common shape once anything multimodal has been in the
    turn; without a picture there is nothing to drop."""
    parts = measure_parts([{"role": "user", "content": [{"type": "text", "text": "hello there"}]}])

    assert parts[CONVERSATION] > 0
    assert parts[FILES] == 0
