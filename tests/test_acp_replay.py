"""A stored transcript, replayed as the stream that produced it.

``session/load`` answers by *replaying* -- the client receives the same
``session/update`` notifications a live turn produces, so a resumed session is
drawn by the same code as a fresh one and needs no second renderer. Every frame
here is validated against the vendored official schema, because the alternative
way to find a replay bug is to read it off somebody's screen.

The order is the part worth pinning. A tool call is announced on the assistant
entry that made it and answered by a later ``role="tool"`` entry, so the
``tool_call`` and its ``tool_call_update`` come from two different messages; a
mapping that emitted them per-message in the wrong order would draw the result
before the call it belongs to.
"""

from __future__ import annotations

import json

import pytest

from raven.acp.replay import MAX_REPLAYED_MESSAGES, MAX_REPLAYED_TEXT, replay
from tests.acp_schema import validate_def


def _kinds(updates):
    return [u["sessionUpdate"] for u in updates]


def _texts(updates, kind=None):
    return [
        u["content"]["text"]
        for u in updates
        if "content" in u and isinstance(u["content"], dict) and (kind is None or u["sessionUpdate"] == kind)
    ]


def _valid(updates):
    for update in updates:
        validate_def("SessionUpdate", update)
        validate_def("SessionNotification", {"sessionId": "acp:s1", "update": update})
    return updates


class TestOrder:
    def test_a_whole_conversation_replays_in_the_order_it_happened(self):
        transcript = [
            {"role": "system", "text": "you are raven"},
            {"role": "user", "text": "read a.py and fix it"},
            {
                "role": "assistant",
                "text": "Looking now.",
                "reasoning_content": "read it first",
                "tool_calls": [{"id": "c1", "name": "read_file", "arguments": json.dumps({"path": "a.py"})}],
            },
            {"role": "tool", "tool_call_id": "c1", "text": "x = 0\n"},
            {"role": "assistant", "text": "Done."},
        ]

        updates = _valid(replay(transcript, session_id="acp:s1", cwd="/work"))

        assert _kinds(updates) == [
            "user_message_chunk",
            "agent_thought_chunk",
            "agent_message_chunk",
            "tool_call",
            "tool_call_update",
            "agent_message_chunk",
        ]

    def test_the_result_never_precedes_the_call_it_answers(self):
        transcript = [
            {"role": "assistant", "tool_calls": [{"id": "c1", "name": "exec", "arguments": '{"command":"ls"}'}]},
            {"role": "tool", "tool_call_id": "c1", "text": "a.py"},
        ]

        updates = replay(transcript, session_id="acp:s1")

        assert updates[0]["sessionUpdate"] == "tool_call"
        assert updates[1]["sessionUpdate"] == "tool_call_update"
        assert updates[0]["toolCallId"] == updates[1]["toolCallId"] == "c1"

    def test_the_thought_precedes_the_words_it_produced(self):
        updates = replay(
            [{"role": "assistant", "text": "The answer is 4.", "reasoning_content": "2 plus 2"}],
            session_id="acp:s1",
        )

        assert _kinds(updates) == ["agent_thought_chunk", "agent_message_chunk"]

    def test_the_system_prompt_is_not_part_of_the_conversation(self):
        """Replaying it would put the agent's own instructions on a person's
        screen as though they had said them."""
        updates = replay([{"role": "system", "text": "secret instructions"}], session_id="acp:s1")

        assert updates == []


class TestToolCalls:
    def test_a_replayed_call_is_pending_not_in_progress(self):
        """``in_progress`` would show a spinner for a call that finished last
        week; ``completed`` would claim an outcome before the entry that carries
        it arrives."""
        updates = replay(
            [{"role": "assistant", "tool_calls": [{"id": "c1", "name": "exec", "arguments": "{}"}]}],
            session_id="acp:s1",
        )

        assert updates[0]["status"] == "pending"

    def test_a_replayed_result_is_completed(self):
        updates = replay([{"role": "tool", "tool_call_id": "c1", "text": "ok"}], session_id="acp:s1")

        assert updates[0]["status"] == "completed"
        assert _texts(updates) == []
        assert updates[0]["content"] == [{"type": "content", "content": {"type": "text", "text": "ok"}}]

    def test_stored_arguments_are_parsed_so_the_row_can_be_labelled(self):
        """They are kept as the JSON string the provider sent, so a title or a
        location needs them parsed."""
        updates = replay(
            [{"role": "assistant", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": '{"path":"a.py"}'}]}],
            session_id="acp:s1",
            cwd="/work",
        )

        assert updates[0]["title"] == "read_file: a.py"
        assert updates[0]["kind"] == "read"
        assert updates[0]["locations"] == [{"path": "/work/a.py"}]

    def test_arguments_that_will_not_parse_still_produce_a_row(self):
        """Guessing at half-parsed arguments would put a fragment of JSON on a
        tool row."""
        updates = replay(
            [{"role": "assistant", "tool_calls": [{"id": "c1", "name": "exec", "arguments": '{"command": "ls'}]}],
            session_id="acp:s1",
        )

        assert updates[0]["title"] == "exec"
        assert "locations" not in updates[0]

    def test_a_call_with_no_id_is_dropped(self):
        updates = replay(
            [{"role": "assistant", "tool_calls": [{"name": "exec", "arguments": "{}"}, "not a dict"]}],
            session_id="acp:s1",
        )

        assert updates == []

    def test_a_result_with_no_call_id_is_dropped_rather_than_shown_loose(self):
        """An invented id would create a second row for a call that already has
        one."""
        updates = replay([{"role": "tool", "text": "orphaned output"}], session_id="acp:s1")

        assert updates == []

    def test_a_stored_diff_rides_along_as_text(self):
        """The stored record is a rendering, and the file's contents at the time
        are gone -- so a structured ``diff`` block would need a ``newText`` that
        would have to be invented."""
        updates = _valid(
            replay(
                [{"role": "tool", "tool_call_id": "c1", "text": "edited", "diff": "--- a\n+++ a\n-0\n+1"}],
                session_id="acp:s1",
            )
        )
        blocks = updates[0]["content"]

        assert len(blocks) == 2
        assert all(b["type"] == "content" for b in blocks)
        assert "+1" in blocks[1]["content"]["text"]

    def test_a_result_that_is_only_a_diff_still_renders(self):
        """A write tool whose model-facing text was empty. Skipping the entry
        would drop the one record that says what changed."""
        updates = _valid(
            replay([{"role": "tool", "tool_call_id": "c1", "diff": "--- a\n+++ a\n-0\n+1"}], session_id="acp:s1")
        )

        assert len(updates[0]["content"]) == 1
        assert "+1" in updates[0]["content"][0]["content"]["text"]

    def test_a_result_with_nothing_in_it_still_closes_the_row(self):
        """Status without content, not a dropped update: the call happened and the
        row has to stop showing as unanswered."""
        updates = _valid(replay([{"role": "tool", "tool_call_id": "c1"}], session_id="acp:s1"))

        assert updates == [{"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"}]

    def test_arguments_already_parsed_are_used_as_they_are(self):
        """A live-cache message holds them as a mapping rather than as the JSON
        string a stored one carries, and both shapes reach here."""
        updates = replay(
            [{"role": "assistant", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.py"}}]}],
            session_id="acp:s1",
            cwd="/work",
        )

        assert updates[0]["title"] == "read_file: a.py"
        assert updates[0]["locations"] == [{"path": "/work/a.py"}]

    @pytest.mark.parametrize("arguments", [None, [], 5, "", "   ", "[1, 2]", '"a string"'])
    def test_arguments_of_an_unusable_shape_leave_the_row_unlabelled(self, arguments):
        """Rather than putting a fragment of whatever it was on a tool row."""
        updates = replay(
            [{"role": "assistant", "tool_calls": [{"id": "c1", "name": "exec", "arguments": arguments}]}],
            session_id="acp:s1",
        )

        assert updates[0]["title"] == "exec"
        assert "locations" not in updates[0]

    def test_a_call_that_was_never_answered_still_renders(self):
        """Its result was lost, which is a thing to show rather than a reason to
        pretend the call did not happen."""
        updates = replay(
            [{"role": "assistant", "tool_calls": [{"id": "c1", "name": "exec", "arguments": "{}"}]}],
            session_id="acp:s1",
        )

        assert _kinds(updates) == ["tool_call"]


class TestContent:
    def test_a_blocked_turn_says_so_when_it_has_no_words_of_its_own(self):
        """``action_blocked`` replaces the answer rather than accompanying it, so
        an entry carrying only a notice would otherwise replay as nothing."""
        updates = replay([{"role": "assistant", "notice": "the runtime refused this"}], session_id="acp:s1")

        assert _texts(updates) == ["the runtime refused this"]

    def test_a_notice_beside_real_text_does_not_duplicate_it(self):
        updates = replay(
            [{"role": "assistant", "text": "Here is what I did instead.", "notice": "blocked"}],
            session_id="acp:s1",
        )

        assert _texts(updates) == ["Here is what I did instead."]

    def test_a_credential_recorded_three_turns_ago_is_still_redacted(self):
        """A replayed transcript is rendered in an editor and kept in its
        history, so it is as much a publishing surface as a live frame."""
        updates = replay(
            [
                {
                    "role": "tool",
                    "tool_call_id": "c1",
                    "text": "ran: curl -H 'Authorization: Bearer sk-ant-AAAABBBBCCCC'",
                }
            ],
            session_id="acp:s1",
        )

        text = updates[0]["content"][0]["content"]["text"]
        assert "sk-ant-AAAABBBBCCCC" not in text
        assert "curl" in text

    def test_an_empty_message_produces_no_frame(self):
        assert replay([{"role": "user", "text": ""}], session_id="acp:s1") == []
        assert replay([{"role": "user"}], session_id="acp:s1") == []
        assert replay([{"role": "assistant", "text": "   "}], session_id="acp:s1") == []

    def test_an_oversized_message_is_clipped_and_says_so(self):
        updates = replay([{"role": "user", "text": "x" * (MAX_REPLAYED_TEXT + 500)}], session_id="acp:s1")

        assert updates[0]["content"]["text"].endswith("[truncated]")
        assert len(updates[0]["content"]["text"]) < MAX_REPLAYED_TEXT + 100


class TestBounds:
    def test_a_long_transcript_is_truncated_from_the_front(self):
        """Newest-last, so what is dropped is what a scrollback would have
        dropped."""
        transcript = [{"role": "user", "text": f"message {n}"} for n in range(MAX_REPLAYED_MESSAGES + 50)]

        updates = replay(transcript, session_id="acp:s1")
        texts = _texts(updates)

        assert texts[-1] == f"message {MAX_REPLAYED_MESSAGES + 49}"
        assert not any(t == "message 0" for t in texts)

    def test_the_truncation_is_announced_rather_than_silent(self):
        """A client that silently starts mid-conversation shows a person a
        history that appears to begin in the middle of a thought."""
        transcript = [{"role": "user", "text": f"m{n}"} for n in range(MAX_REPLAYED_MESSAGES + 3)]

        updates = _valid(replay(transcript, session_id="acp:s1"))

        assert "not shown" in updates[0]["content"]["text"]
        assert "3 earlier" in updates[0]["content"]["text"]

    def test_a_transcript_at_the_limit_is_not_announced(self):
        transcript = [{"role": "user", "text": f"m{n}"} for n in range(MAX_REPLAYED_MESSAGES)]

        updates = replay(transcript, session_id="acp:s1")

        assert "not shown" not in updates[0]["content"]["text"]


class TestMalformedInput:
    @pytest.mark.parametrize("messages", [None, "transcript", 5, {}])
    def test_a_non_list_transcript_replays_nothing(self, messages):
        assert replay(messages, session_id="acp:s1") == []

    def test_a_corrupt_entry_costs_only_itself(self):
        """One bad stored line must not brick the whole history -- which is the
        same rule the transcript mapper upstream follows."""
        updates = replay(
            ["not a dict", {"no_role": True}, {"role": ""}, {"role": "user", "text": "kept"}],
            session_id="acp:s1",
        )

        assert _texts(updates) == ["kept"]

    def test_an_unknown_role_is_skipped_rather_than_guessed(self):
        assert replay([{"role": "moderator", "text": "hm"}], session_id="acp:s1") == []
