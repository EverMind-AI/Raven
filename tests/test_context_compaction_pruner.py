"""The tool-result pruner -- what it replaces, what it refuses to touch.

Three properties carry the design and each is checked directly rather than
through the engine that calls it:

- **it reclaims without removing** -- the message list comes back the same
  length, in the same order, with the same tool pairing, so nothing downstream
  has to re-close adjacency;
- **it is idempotent** -- a second pass finds nothing, because the line it
  writes is shorter than the shortest thing it will select. No "already pruned"
  marker to keep in step with the format;
- **it does not write through to the log** -- the list it is handed holds the
  session's own dicts, so a pruned message has to be a new one. This is the
  property that makes the whole scheme work: the window loses the body, the
  log keeps it, and the ref is what connects them.
"""

from __future__ import annotations

import copy
import json

import pytest

from raven.config.raven import PRUNE_LINE_MAX_CHARS, ContextPruneConfig
from raven.context_engine.history.compacting.pruner import ToolResultPruner
from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.contracts.context import SessionRevision
from raven.utils.refs import SourceRef

REVISION = SessionRevision(log_id="1a2b3c4d" * 4, generation=0)
SCOPE = REVISION.scope
BIG = "stack trace line\n" * 3_000


def _exchange(call_id: str, tool: str, result: str, arguments: dict | None = None) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(arguments or {})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "name": tool, "content": result},
    ]


def _session(*results: tuple[str, str, str], arguments: dict | None = None) -> list[dict]:
    """A user question, then one exchange per result, then an answer."""
    messages: list[dict] = [{"role": "user", "content": "why is auth failing?"}]
    for call_id, tool, result in results:
        messages += _exchange(call_id, tool, result, arguments)
    messages.append({"role": "assistant", "content": "because the token rotates late"})
    return messages


def _pruner(**overrides) -> ToolResultPruner:
    return ToolResultPruner(ContextPruneConfig(**{"keep_recent_pairs": 0, **overrides}))


def _prune(messages: list[dict], **overrides):
    protect_from = overrides.pop("protect_from", None)
    protected_ids = overrides.pop("protected_ids", frozenset())
    return _pruner(**overrides).run(
        messages,
        revision=REVISION,
        protect_from=protect_from,
        protected_ids=protected_ids,
    )


class TestTheReplacement:
    def test_a_large_result_becomes_one_short_line(self) -> None:
        messages = _session(("c1", "exec", BIG))
        out = _prune(messages)

        line = out.messages[2]["content"]
        assert len(BIG) > 40_000 and len(line) < 100
        assert "\n" not in line

    def test_the_line_carries_a_ref_that_names_the_message_it_replaced(self) -> None:
        out = _prune(_session(("c1", "exec", BIG)))

        ref = SourceRef.parse(out.messages[2]["content"])
        assert ref == SourceRef("session", SCOPE, 2, 2)
        assert out.refs == (ref,)

    def test_the_line_names_the_tool_and_the_size_of_what_it_replaced(self) -> None:
        out = _prune(_session(("c1", "grep", BIG), arguments={"pattern": "refreshToken"}))

        line = out.messages[2]["content"]
        assert line.startswith("[tool:grep pattern=refreshToken")
        assert f"{len(BIG):,} chars" in line

    def test_a_shell_result_keeps_its_exit_code(self) -> None:
        """The one piece of structure a result's *text* can be read for without
        guessing: every shell result ends with it."""
        out = _prune(_session(("c1", "exec", BIG + "\nExit code: 1")))

        assert "exit=1" in out.messages[2]["content"]

    def test_a_failed_call_is_marked_as_one(self) -> None:
        out = _prune(_session(("c1", "read_file", "Error: no such file\n" + BIG)))

        assert " error " in out.messages[2]["content"]

    def test_a_successful_call_is_marked_as_one(self) -> None:
        out = _prune(_session(("c1", "read_file", BIG)))

        assert " ok " in out.messages[2]["content"]

    def test_the_line_stays_inside_its_bound(self) -> None:
        """The bound is what makes idempotence arithmetic rather than a guard,
        so a long argument has to be clipped rather than allowed through."""
        out = _prune(_session(("c1", "web_fetch", BIG), arguments={"url": "https://example.com/" + "x" * 4_000}))

        line = out.messages[2]["content"]
        assert len(line) <= PRUNE_LINE_MAX_CHARS
        assert SourceRef.parse(line) is not None

    def test_the_ref_is_the_half_that_survives_a_clip(self) -> None:
        """With a long enough scope the record has to give way to the ref: a
        clipped ref resolves to nothing, and then the body really is gone."""
        long_scope = SessionRevision(log_id="f" * 32, generation=10**95)
        out = ToolResultPruner(ContextPruneConfig(keep_recent_pairs=0)).run(
            _session(("c1", "exec", BIG)),
            revision=long_scope,
        )

        line = out.messages[2]["content"]
        assert len(line) <= PRUNE_LINE_MAX_CHARS
        assert not line.startswith("[tool:exec ok")
        assert SourceRef.parse(line) == out.refs[0]


class TestWhatItLeavesAlone:
    def test_a_result_under_the_threshold_is_untouched(self) -> None:
        small = "ok, 12 lines written"
        out = _prune(_session(("c1", "write_file", small)))

        assert out.messages[2]["content"] == small
        assert out.pruned_count == 0

    def test_the_recent_exchanges_keep_their_bodies(self) -> None:
        messages = _session(("c1", "exec", BIG), ("c2", "exec", BIG), ("c3", "exec", BIG))
        out = _prune(messages, keep_recent_pairs=2)

        assert out.messages[2]["content"].startswith("[tool:exec")
        assert out.messages[4]["content"] == BIG
        assert out.messages[6]["content"] == BIG

    def test_fewer_exchanges_than_the_recent_count_means_none_are_touched(self) -> None:
        out = _prune(_session(("c1", "exec", BIG), ("c2", "exec", BIG)), keep_recent_pairs=3)

        assert out.pruned_count == 0

    def test_the_callers_floor_holds_back_everything_above_it(self) -> None:
        messages = _session(("c1", "exec", BIG), ("c2", "exec", BIG))
        out = _prune(messages, protect_from=3)

        assert out.messages[2]["content"].startswith("[tool:exec")
        assert out.messages[4]["content"] == BIG

    def test_a_pinned_hole_is_not_pruned(self) -> None:
        """A pinned body is kept verbatim precisely because its absence is
        silently wrong; a ref the model must spend a call on is not the same
        thing as having the text."""
        messages = _session(("c1", "use_skill", BIG), ("c2", "exec", BIG))
        out = _prune(messages, protected_ids=frozenset({1, 2}))

        assert out.messages[2]["content"] == BIG
        assert out.messages[4]["content"].startswith("[tool:exec")

    def test_a_long_user_or_assistant_message_is_not_a_target(self) -> None:
        messages = [{"role": "user", "content": BIG}, {"role": "assistant", "content": BIG}]
        out = _prune(messages)

        assert out.pruned_count == 0
        assert [m["content"] for m in out.messages] == [BIG, BIG]

    def test_turning_it_off_is_a_no_op(self) -> None:
        messages = _session(("c1", "exec", BIG))
        out = _prune(messages, enabled=False)

        assert out.messages is messages
        assert (out.pruned_count, out.reclaimed_tokens) == (0, 0)


class TestStructure:
    def test_nothing_is_removed_or_reordered(self) -> None:
        messages = _session(("c1", "exec", BIG), ("c2", "read_file", BIG))
        out = _prune(messages)

        assert len(out.messages) == len(messages)
        assert [m["role"] for m in out.messages] == [m["role"] for m in messages]
        assert HistoryTrimmer.structural_errors(out.messages) == []

    def test_the_session_log_it_was_handed_is_not_written_through(self) -> None:
        """The list is a shallow copy of ``session.messages``: mutating a dict
        in it would edit the append-only log the refs point into."""
        messages = _session(("c1", "exec", BIG))
        before = copy.deepcopy(messages)

        out = _prune(messages)

        assert messages == before
        assert out.messages[2] is not messages[2]
        assert out.messages[0] is messages[0]


class TestIdempotence:
    def test_a_second_pass_finds_nothing(self) -> None:
        once = _prune(_session(("c1", "exec", BIG), ("c2", "exec", BIG)))
        twice = _prune(once.messages)

        assert once.pruned_count == 2
        assert twice.pruned_count == 0
        assert twice.messages == once.messages

    def test_the_threshold_is_above_what_a_pass_writes(self) -> None:
        """The whole guarantee, as one comparison. A ``minChars`` at or below
        the line length would let the second pass select the first pass's
        output, and the config refuses to build."""
        assert ContextPruneConfig().min_chars > PRUNE_LINE_MAX_CHARS


class TestDedup:
    def test_an_earlier_identical_result_goes_and_the_last_one_stays(self) -> None:
        """Reading the same file twice is the common case, and the copy still
        being worked from is the later one."""
        body = "auth.ts contents\n" * 40
        messages = _session(("c1", "read_file", body), ("c2", "read_file", body))
        out = _prune(messages)

        assert len(body) < ContextPruneConfig().min_chars
        assert out.messages[2]["content"].startswith("[tool:read_file")
        assert out.messages[4]["content"] == body

    def test_a_re_read_this_turn_retires_the_copy_from_ten_turns_ago(self) -> None:
        """The later copy being protected is the point: it is present and
        whole, so the older one is the redundant half even though the pruner
        cannot touch the copy that made it redundant."""
        body = "auth.ts contents\n" * 40
        messages = _session(("c1", "read_file", body), ("c2", "read_file", body))
        out = _prune(messages, keep_recent_pairs=1)

        assert out.messages[2]["content"].startswith("[tool:read_file")
        assert out.messages[4]["content"] == body

    def test_turning_dedup_off_keeps_both(self) -> None:
        body = "auth.ts contents\n" * 40
        out = _prune(_session(("c1", "read_file", body), ("c2", "read_file", body)), dedup=False)

        assert out.pruned_count == 0

    def test_a_duplicate_too_small_to_be_worth_it_is_kept(self) -> None:
        """Replacing forty characters with a hundred and sixty is not a
        reclaim."""
        out = _prune(_session(("c1", "read_file", "same"), ("c2", "read_file", "same")))

        assert out.pruned_count == 0


class TestCallArguments:
    def _write(self, content: str) -> list[dict]:
        return _session(("c1", "write_file", "ok"), arguments={"path": "big.py", "content": content})

    def test_an_oversized_argument_is_shortened_in_place(self) -> None:
        """The result of a ``write_file`` was never the expensive half."""
        out = _prune(self._write("x" * 50_000))

        arguments = json.loads(out.messages[1]["tool_calls"][0]["function"]["arguments"])
        assert len(arguments["content"]) < 2_000
        assert arguments["path"] == "big.py"

    def test_what_it_writes_is_still_valid_json(self) -> None:
        """Cutting the serialised string instead would be rejected by the
        provider on this turn and on every turn after it."""
        out = _prune(self._write("x" * 50_000))

        raw = out.messages[1]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(json.loads(raw), dict)

    def test_the_shortened_argument_says_where_the_rest_is(self) -> None:
        out = _prune(self._write("x" * 50_000))

        arguments = json.loads(out.messages[1]["tool_calls"][0]["function"]["arguments"])
        assert SourceRef.parse(arguments["content"]) == SourceRef("session", SCOPE, 1, 1)

    def test_arguments_within_the_limit_are_left_as_they_were(self) -> None:
        out = _prune(self._write("x" * 100))

        assert out.messages[1] is not None
        assert json.loads(out.messages[1]["tool_calls"][0]["function"]["arguments"])["content"] == "x" * 100

    def test_turning_it_off_leaves_the_call_whole(self) -> None:
        out = _prune(self._write("x" * 50_000), prune_call_args=False)

        assert json.loads(out.messages[1]["tool_calls"][0]["function"]["arguments"])["content"] == "x" * 50_000

    def test_unparseable_arguments_are_not_rewritten(self) -> None:
        messages = _session(("c1", "write_file", "ok"))
        messages[1]["tool_calls"][0]["function"]["arguments"] = "{not json" + "x" * 50_000

        out = _prune(messages)

        assert out.messages[1] is messages[1]


class TestAccounting:
    def test_it_reports_what_it_reclaimed(self) -> None:
        out = _prune(_session(("c1", "exec", BIG)))

        assert out.reclaimed_tokens > 1_000
        assert out.pruned_count == len(out.refs) == 1

    def test_nothing_pruned_is_nothing_reclaimed(self) -> None:
        out = _prune(_session(("c1", "exec", "small")))

        assert (out.pruned_count, out.reclaimed_tokens, out.refs, out.changed) == (0, 0, (), False)


@pytest.mark.parametrize("messages", [[], [{"role": "user", "content": "hi"}]])
def test_a_session_with_nothing_to_prune_is_handed_straight_back(messages: list[dict]) -> None:
    out = _prune(messages)

    assert out.messages is messages
    assert out.pruned_count == 0


class TestTheFortyKilobyteResult:
    def test_it_becomes_one_bounded_line_whose_ref_reads_back_the_original(self) -> None:
        """The acceptance case: 40K characters of output go to one line, and
        the ordinal the ref names is the message the log still holds whole."""
        body = ("Traceback (most recent call last):\n  File auth.ts, line 214\n" * 800)[:40_000]
        messages = _session(("c1", "exec", body), arguments={"command": "pytest -x"})
        out = _prune(messages)

        line = out.messages[2]["content"]
        assert len(body) == 40_000
        assert len(line) <= PRUNE_LINE_MAX_CHARS and "\n" not in line
        ref = SourceRef.parse(line)
        assert ref is not None and (ref.store, ref.scope, ref.lo, ref.hi) == ("session", SCOPE, 2, 2)
        assert messages[ref.lo]["content"] == body


class TestReapply:
    """What makes a prune sticky: the ordinals a pass chose are rewritten to the
    same bytes on every later turn, under the trigger or not."""

    def test_the_same_ordinals_come_out_as_the_same_bytes(self) -> None:
        messages = _session(("c1", "exec", BIG), ("c2", "read_file", BIG))
        first = _prune(messages)

        again = _pruner().reapply(messages, revision=REVISION, ordinals=first.ordinals)

        assert again.ordinals == first.ordinals == frozenset({2, 4})
        assert again.messages == first.messages

    def test_it_does_not_ask_the_thresholds_again(self) -> None:
        """A later turn with a roomier configuration must not put the body back:
        the ordinal is here because an earlier pass already decided it."""
        messages = _session(("c1", "exec", BIG))
        first = _prune(messages)

        again = ToolResultPruner(ContextPruneConfig(min_chars=10_000_000, keep_recent_pairs=50)).reapply(
            messages, revision=REVISION, ordinals=first.ordinals
        )

        assert again.messages[2]["content"] == first.messages[2]["content"]

    def test_a_body_that_became_pinned_is_held_whole(self) -> None:
        messages = _session(("c1", "use_skill", BIG))
        again = _pruner().reapply(messages, revision=REVISION, ordinals=frozenset({2}), protected_ids=frozenset({1, 2}))

        assert again.messages[2]["content"] == BIG
        assert again.ordinals == frozenset()

    def test_ordinals_past_the_end_of_the_log_are_ignored(self) -> None:
        messages = _session(("c1", "exec", BIG))
        again = _pruner().reapply(messages, revision=REVISION, ordinals=frozenset({2, 99}))

        assert again.ordinals == frozenset({2})

    def test_turning_the_pruner_off_stops_the_reapplication_too(self) -> None:
        messages = _session(("c1", "exec", BIG))
        again = _pruner(enabled=False).reapply(messages, revision=REVISION, ordinals=frozenset({2}))

        assert again.messages is messages

    def test_reapplying_shortened_arguments_is_stable(self) -> None:
        messages = _session(("c1", "write_file", "ok"), arguments={"path": "big.py", "content": "x" * 50_000})
        first = _prune(messages)
        assert 1 in first.ordinals

        again = _pruner().reapply(messages, revision=REVISION, ordinals=first.ordinals)

        assert again.messages[1] == first.messages[1]


class TestReusedToolCallIds:
    def test_each_result_is_named_after_the_call_it_answered(self) -> None:
        """Nothing makes an id unique across a session. Paired by id, the second
        ``call_0`` would rename the first result after the later call."""
        messages = _session(("call_0", "exec", BIG), ("call_0", "grep", BIG))
        out = _prune(messages)

        assert out.messages[2]["content"].startswith("[tool:exec")
        assert out.messages[4]["content"].startswith("[tool:grep")
        assert HistoryTrimmer.structural_errors(out.messages) == []


class TestReusedIdsWithDifferentArguments:
    def test_each_record_names_the_argument_of_its_own_call(self) -> None:
        """Same id, same tool, different command: only positional pairing can
        tell the two results apart, because the tool name alone is identical."""
        messages: list[dict] = [{"role": "user", "content": "run both"}]
        messages += _exchange("call_0", "exec", BIG, {"command": "pytest -x"})
        messages += _exchange("call_0", "exec", BIG + " ", {"command": "make all"})
        messages.append({"role": "assistant", "content": "done"})

        out = _prune(messages)

        assert "command=pytest -x" in out.messages[2]["content"]
        assert "command=make all" in out.messages[4]["content"]
