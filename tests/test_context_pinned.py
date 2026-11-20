"""Pinned skill bodies: which history messages carry one, and the config default.

The scan is strategy-independent: every history strategy that drops messages
has to know which ones it may not drop. Its edge cases live here rather than
in one strategy's tests.
"""

from __future__ import annotations

import json

import pytest

from raven.config.raven import ContextConfig
from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.context_engine.pinned import pinned_message_ids


def _use_skill_exchange(call_id: str, skill_id: str, body: str) -> list[dict]:
    """The two messages a `use_skill` fetch leaves in the session log."""
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "use_skill", "arguments": json.dumps({"skill_id": skill_id})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "name": "use_skill", "content": body},
    ]


def test_pinned_scan_takes_the_whole_exchange_and_supersedes_earlier_fetches() -> None:
    """Half a tool exchange is a dangling tool_call, and a re-read must move the
    pin rather than add a second copy of the same body."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "use_skill", "arguments": '{"skill_id": "local/guide"}'},
                },
                {"id": "c2", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "use_skill", "content": "body"},
        {"role": "tool", "tool_call_id": "c2", "name": "grep", "content": "hits"},
        {"role": "user", "content": "again"},
        *_use_skill_exchange("c3", "local/guide", "body v2"),
    ]

    pinned = pinned_message_ids(messages, ["local/guide"])

    # Only the latest fetch (5, 6) -- and the sibling grep result of the earlier
    # turn is not pinned because that whole turn was superseded.
    assert pinned == {5, 6}


def test_pinned_scan_covers_sibling_results_of_the_pinned_turn() -> None:
    """The pin is the whole tool group, not the one result that carries the body.

    A turn can fetch the guide and call another tool beside it, both answered
    under one assistant message. Pinning only the fetch's own result would let
    compaction keep a result whose call it dropped -- a prompt the provider
    refuses -- which is why the answer is the same positional pairing the
    trimmer drops whole groups by.
    """
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "use_skill", "arguments": '{"skill_id": "local/guide"}'},
                },
                {"id": "c2", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "use_skill", "content": "body"},
        {"role": "tool", "tool_call_id": "c2", "name": "grep", "content": "hits"},
    ]

    assert pinned_message_ids(messages, ["local/guide"]) == {1, 2, 3}
    # The same group from the body alone: selecting the pinned result pulls its
    # call and every sibling result, so the kept exchange is one the log can
    # have started with.
    assert HistoryTrimmer.canonical_ids(messages, [0, 2]) == [0, 1, 2, 3]


@pytest.mark.parametrize("tool_name", ["use_skill", "read_skill"])
@pytest.mark.parametrize("inner_arguments", ["object", "string"])
def test_pinned_scan_reads_a_skill_fetch_forwarded_through_tool_call(tool_name, inner_arguments) -> None:
    """A fetch through ``tool_call`` is the fetch it forwards.

    The outer name is the forwarder, so a scan that stopped there left the
    body the model loaded through the fold unpinned and compaction could drop
    it. The tool accepts the target's arguments as an object or as a JSON
    string, so both spellings have to read as the same fetch.
    """
    forwarded = {"skill_id": "local/guide"}
    messages = [
        {"role": "user", "content": "read the guide"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "tool_call",
                        "arguments": json.dumps(
                            {
                                "name": tool_name,
                                "arguments": forwarded if inner_arguments == "object" else json.dumps(forwarded),
                            }
                        ),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": tool_name, "content": "body"},
    ]

    assert pinned_message_ids(messages, ["local/guide"]) == {1, 2}


@pytest.mark.parametrize("later_id", ["first", "different"])
@pytest.mark.parametrize("reread", [False, True])
def test_pinned_scan_pairs_reused_ids_by_position(later_id, reread):
    """A re-read moves the pin; a fetch of another skill leaves it where it was."""
    messages = [
        {"role": "user", "content": "read the guide"},
        *_use_skill_exchange("first", "local/guide", "original guide"),
        {"role": "user", "content": "continue"},
        *_use_skill_exchange(later_id, "local/guide" if reread else "local/unrelated", "later body"),
    ]
    expected = {4, 5} if reread else {1, 2}
    pinned = pinned_message_ids(messages, ["local/guide"])
    assert pinned == expected
    assert HistoryTrimmer.canonical_ids(messages, [0, *pinned]) == [0, *sorted(expected)]


@pytest.mark.parametrize(
    "tool_calls",
    [
        [{"id": "c", "type": "function", "function": {"name": "use_skill", "arguments": "{not json"}}],
        [
            {
                "id": "c",
                "type": "function",
                "function": {"name": "use_skill", "arguments": '{"skill_id": "local/other"}'},
            }
        ],
        [
            {
                "id": "c",
                "type": "function",
                "function": {"name": "web_fetch", "arguments": '{"skill_id": "local/guide"}'},
            }
        ],
        ["not-a-dict"],
    ],
)
def test_pinned_scan_pins_nothing_it_cannot_positively_identify(tool_calls) -> None:
    """A malformed stored tool_call must never raise, and a fetch of some other
    skill is not this skill's pin."""
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": tool_calls},
        {"role": "tool", "tool_call_id": "c", "name": "use_skill", "content": "body"},
    ]

    assert pinned_message_ids(messages, ["local/guide"]) == set()


def test_pinned_scan_is_off_when_no_ids_are_configured() -> None:
    messages = [
        {"role": "user", "content": "Set up the project."},
        *_use_skill_exchange("call_guide", "local/guide", "body"),
    ]
    assert pinned_message_ids(messages, []) == set()
    assert pinned_message_ids(messages, None) == set()


def test_the_default_pinned_skill_is_the_id_the_dag_tool_advertises() -> None:
    """The config default and the tool's pointer are the same string in two
    layers; this is the seam that catches them drifting apart."""
    from raven.agent.subagent.dag_tool import GUIDE_SKILL_ID

    assert ContextConfig().pinned_skill_ids == [GUIDE_SKILL_ID]
