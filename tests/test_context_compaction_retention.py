"""Retention — where the three ranges fall, and what leaves the middle.

Two layers, tested apart. The ledger answers structural questions about a
history: what each message costs, and where the conversation may legally be
cut. The planner turns those answers into ``[HEAD][COMPACT][TAIL]`` plus the
holes pinned bodies punch in the middle, and then empties COMPACT until the
turn fits.

The properties worth holding are the ones whose absence is silent: a cut that
separates a tool call from its result produces a request the provider refuses,
and a pinned body that quietly goes missing produces an agent that rebuilds a
guide from memory. Both are asserted directly rather than through the assembled
turn.
"""

from __future__ import annotations

import json

import pytest

from raven.config.raven import ContextCompactionConfig
from raven.context_engine.history.compacting.ledger import MessageLedger
from raven.context_engine.history.compacting.retention import RetentionPlanner, project
from raven.context_engine.history.compacting.trigger import TriggerPolicy
from raven.context_engine.history_trimmer import HistoryTrimmer

PINNED_SKILL = "local/subagent-dag-orchestration"
PINNED_BODY = "the dag orchestration guide, verbatim"


def _exchange(index: int, *, tool: str = "read_file", skill_id: str | None = None) -> list[dict]:
    """One assistant tool call plus its single result."""
    args = {"skill_id": skill_id} if skill_id else {"path": f"f{index}"}
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": f"c{index}", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"c{index}",
            "name": tool,
            "content": PINNED_BODY if skill_id else f"result {index}",
        },
    ]


def _conversation(turns: int = 10, *, pin_at: int | None = None) -> list[dict]:
    """``turns`` rounds of user / tool call / tool result / assistant."""
    messages: list[dict] = []
    for index in range(turns):
        messages.append({"role": "user", "content": f"question {index}"})
        if pin_at == index:
            messages += _exchange(index, tool="use_skill", skill_id=PINNED_SKILL)
        else:
            messages += _exchange(index)
        messages.append({"role": "assistant", "content": f"answer {index}"})
    return messages


# Small on purpose. Retention reads the window only through the tail's token
# floor, and a toy conversation against a real window is entirely tail -- which
# is correct behaviour and a vacuous test. 1024 puts the split in the middle of
# these forty messages, where the interesting cases are.
TEST_WINDOW = 1_024


def _plan(window: int = TEST_WINDOW, prompt_tokens: int = 0, cfg: ContextCompactionConfig | None = None):
    return TriggerPolicy(cfg or ContextCompactionConfig()).evaluate(
        prompt_tokens=prompt_tokens,
        window=window,
        reply_headroom=0,
    )


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class TestLedger:
    def test_every_message_is_priced_and_the_total_is_their_sum(self) -> None:
        ledger = MessageLedger(_conversation(3))
        assert len(ledger) == 12
        assert all(entry.tokens > 0 for entry in ledger.entries)
        assert ledger.total_tokens == sum(entry.tokens for entry in ledger.entries)

    def test_an_empty_history_is_priced_at_nothing(self) -> None:
        ledger = MessageLedger([])
        assert (len(ledger), ledger.total_tokens) == (0, 0)

    def test_walking_back_stops_at_the_budget(self) -> None:
        ledger = MessageLedger(_conversation(5))
        start = ledger.walk_back_until(ledger.total_tokens // 2)
        assert 0 < start < len(ledger)
        assert ledger.tokens_for(set(range(start, len(ledger)))) <= ledger.total_tokens // 2

    def test_walking_back_past_the_whole_history_stops_at_the_start(self) -> None:
        ledger = MessageLedger(_conversation(2))
        assert ledger.walk_back_until(10_000_000) == 0

    def test_a_budget_too_small_for_one_message_keeps_none(self) -> None:
        """``len`` rather than 0: the caller asked for a token floor, and
        answering "the whole history" would turn a floor into its opposite."""
        ledger = MessageLedger(_conversation(2))
        assert ledger.walk_back_until(0) == len(ledger)

    def test_a_cut_never_falls_between_a_tool_call_and_its_result(self) -> None:
        """Index 1 is the assistant that called; index 2 is the result. A cut
        at 2 would hand the tail an orphan, so it moves either way to 1 or 3."""
        ledger = MessageLedger(_conversation(2))
        assert ledger.messages[1].get("tool_calls") and ledger.messages[2]["role"] == "tool"
        assert ledger.align_backward(2, "tool_pair") == 1
        assert ledger.align_forward(2, "tool_pair") == 3

    def test_a_legal_cut_is_left_where_it_is(self) -> None:
        ledger = MessageLedger(_conversation(3))
        assert ledger.align_backward(4, "tool_pair") == 4
        assert ledger.align_forward(4, "tool_pair") == 4

    def test_the_turn_boundary_snaps_to_a_user_message(self) -> None:
        """Coarser than ``tool_pair``: it never splits one round of work, at
        the cost of compacting in whole turns only."""
        ledger = MessageLedger(_conversation(3))
        assert [e.role for e in ledger.entries][:5] == ["user", "assistant", "tool", "assistant", "user"]
        assert ledger.align_backward(3, "turn") == 0
        assert ledger.align_forward(1, "turn") == 4

    def test_a_group_is_the_whole_exchange(self) -> None:
        ledger = MessageLedger(_conversation(2))
        assert ledger.group_of(1) == frozenset({1, 2})
        assert ledger.group_of(0) == frozenset({0})


# ---------------------------------------------------------------------------
# Splitting into ranges
# ---------------------------------------------------------------------------


class TestSplit:
    def test_the_three_ranges_tile_the_history_without_overlapping(self) -> None:
        ledger = MessageLedger(_conversation(10))
        ranges = RetentionPlanner(ContextCompactionConfig()).split(ledger, _plan(), generation=0)
        assert ranges.head.start == 0
        assert ranges.head.stop == ranges.compact.start
        assert ranges.compact.stop == ranges.tail.start
        assert ranges.tail.stop == len(ledger)

    def test_the_head_holds_the_opening_exchange(self) -> None:
        """Two messages asked for, three kept: the second is a tool call, and a
        head that ended there would leave its result parentless."""
        ledger = MessageLedger(_conversation(10))
        ranges = RetentionPlanner(ContextCompactionConfig()).split(ledger, _plan(), generation=0)
        assert list(ranges.head) == [0, 1, 2]
        assert ledger.messages[0]["content"] == "question 0"

    def test_the_head_decays_to_nothing_after_the_first_compaction(self) -> None:
        """Copied from the design deliberately: those turns are represented by
        what compaction produced by then, and protecting them forever makes the
        head the one part of the history that can never age out."""
        ledger = MessageLedger(_conversation(10))
        planner = RetentionPlanner(ContextCompactionConfig())
        assert len(planner.split(ledger, _plan(), generation=1).head) == 0
        assert len(planner.split(ledger, _plan(), generation=5).head) == 0

    def test_the_head_can_be_kept_forever_by_configuration(self) -> None:
        ledger = MessageLedger(_conversation(10))
        planner = RetentionPlanner(ContextCompactionConfig(head_keep_decays=False))
        assert list(planner.split(ledger, _plan(), generation=9).head) == [0, 1, 2]

    def test_the_head_is_aligned_forward_off_a_tool_boundary(self) -> None:
        """Aligning only ever adds. Asking for one message keeps one, because a
        cut after the opening question splits nothing; asking for two keeps
        three, because a cut there would split the exchange that follows."""
        ledger = MessageLedger(_conversation(10))
        one = RetentionPlanner(ContextCompactionConfig(head_keep_messages=1))
        two = RetentionPlanner(ContextCompactionConfig(head_keep_messages=2))
        assert list(one.split(ledger, _plan(), generation=0).head) == [0]
        assert list(two.split(ledger, _plan(), generation=0).head) == [0, 1, 2]

    def test_the_tail_takes_the_count_when_the_ratio_would_take_less(self) -> None:
        """A window small enough that the token floor reaches back only a
        message or two: the count floor is then what binds, and the union of
        the two is what either one asks for."""
        ledger = MessageLedger(_conversation(10))
        cfg = ContextCompactionConfig()
        ranges = RetentionPlanner(cfg).split(ledger, _plan(window=64), generation=0)
        assert len(ranges.tail) >= cfg.tail_keep_messages

    def test_the_tail_takes_the_ratio_when_it_reaches_further_back(self) -> None:
        """The union again, from the other side: a tail share big enough to
        cover the whole history keeps all of it, count floor or not."""
        ledger = MessageLedger(_conversation(10))
        cfg = ContextCompactionConfig(tail_keep_messages=2, tail_keep_ratio=0.39)
        ranges = RetentionPlanner(cfg).split(ledger, _plan(window=100_000, cfg=cfg), generation=1)
        assert ranges.tail.start == 0
        assert len(ranges.compact) == 0

    def test_a_tiny_window_leaves_the_tail_at_the_count_floor(self) -> None:
        ledger = MessageLedger(_conversation(10))
        cfg = ContextCompactionConfig()
        ranges = RetentionPlanner(cfg).split(ledger, _plan(window=8, cfg=cfg), generation=1)
        assert len(ranges.tail) >= cfg.tail_keep_messages

    def test_head_and_tail_never_cross(self) -> None:
        """Two messages of history and a head that wants two of them: the head
        yields, because a negative compact range is not a range."""
        ledger = MessageLedger([{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}])
        ranges = RetentionPlanner(ContextCompactionConfig()).split(ledger, _plan(), generation=0)
        assert ranges.compact.start <= ranges.compact.stop
        assert len(ranges.compact) == 0


# ---------------------------------------------------------------------------
# Pinned bodies as holes
# ---------------------------------------------------------------------------


class TestPinnedHoles:
    def _split(self, cfg: ContextCompactionConfig | None = None, *, skills: list[str] | None = None):
        messages = _conversation(10, pin_at=4)
        ledger = MessageLedger(messages)
        planner = RetentionPlanner(
            cfg or ContextCompactionConfig(),
            [PINNED_SKILL] if skills is None else skills,
        )
        return ledger, planner, planner.split(ledger, _plan(), generation=1)

    def test_a_pinned_fetch_becomes_a_hole_in_the_compacted_range(self) -> None:
        ledger, _planner, ranges = self._split()
        holes = ranges.hole_ids
        assert holes, "the pinned exchange sits mid-history, which is where COMPACT is"
        assert holes <= set(ranges.compact)
        assert all(ledger.messages[index].get("role") in {"assistant", "tool"} for index in holes)

    def test_the_hole_covers_the_call_as_well_as_the_result(self) -> None:
        """Half an exchange is worse than none: the surviving result has no
        parent, and that is a request the provider refuses outright."""
        ledger, _planner, ranges = self._split()
        holes = ranges.hole_ids
        assert len(holes) == 2
        parent = min(holes)
        assert ledger.messages[parent].get("tool_calls")
        assert ledger.messages[parent + 1]["content"] == PINNED_BODY

    def test_a_hole_survives_a_compaction_that_empties_everything_around_it(self) -> None:
        ledger, planner, ranges = self._split()
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=0)
        assert set(ranges.hole_ids) <= set(selection.kept)
        assert PINNED_BODY in [ledger.messages[index].get("content") for index in selection.kept]

    def test_the_body_is_kept_verbatim_rather_than_referenced(self) -> None:
        ledger, planner, ranges = self._split()
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=0)
        projected = project(ledger.messages, selection.kept)
        assert [m for m in projected if m.get("content") == PINNED_BODY]

    def test_holes_can_be_turned_off(self) -> None:
        cfg = ContextCompactionConfig(honor_pinned=False)
        _ledger, _planner, ranges = self._split(cfg)
        assert ranges.holes == ()

    def test_no_pinned_ids_configured_means_no_holes(self) -> None:
        _ledger, _planner, ranges = self._split(skills=[])
        assert ranges.holes == ()

    def test_an_unpinned_skill_read_is_not_a_hole(self) -> None:
        messages = _conversation(10, pin_at=4)
        ledger = MessageLedger(messages)
        planner = RetentionPlanner(ContextCompactionConfig(), ["local/something-else"])
        assert planner.split(ledger, _plan(), generation=1).holes == ()


# ---------------------------------------------------------------------------
# Emptying COMPACT
# ---------------------------------------------------------------------------


class TestKeepWithin:
    def test_a_prompt_already_under_budget_keeps_everything(self) -> None:
        ledger = MessageLedger(_conversation(10))
        planner = RetentionPlanner(ContextCompactionConfig())
        ranges = planner.split(ledger, _plan(), generation=0)
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=10_000_000)
        assert selection.dropped == ()
        assert len(selection.kept) == len(ledger)
        assert selection.exhausted is False

    def test_the_oldest_compactable_exchange_goes_first(self) -> None:
        ledger = MessageLedger(_conversation(10))
        planner = RetentionPlanner(ContextCompactionConfig())
        ranges = planner.split(ledger, _plan(), generation=0)
        target = ledger.total_tokens - ledger.tokens_for({ranges.compact.start}) - 1
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=target)
        assert ranges.compact.start in selection.dropped
        assert max(selection.dropped) < ranges.tail.start

    def test_neither_the_head_nor_the_tail_is_touched(self) -> None:
        ledger = MessageLedger(_conversation(10))
        planner = RetentionPlanner(ContextCompactionConfig())
        ranges = planner.split(ledger, _plan(), generation=0)
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=0)
        assert set(ranges.head) <= set(selection.kept)
        assert set(ranges.tail) <= set(selection.kept)
        assert set(selection.dropped) <= set(ranges.compact)

    def test_a_target_it_cannot_reach_is_reported_rather_than_forced(self) -> None:
        """The head and tail are not the compactor's to spend. Saying so lets
        the caller warn that the window cannot host the policy, instead of
        pretending the target was met."""
        ledger = MessageLedger(_conversation(10))
        planner = RetentionPlanner(ContextCompactionConfig())
        ranges = planner.split(ledger, _plan(), generation=0)
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=0)
        assert selection.exhausted is True
        assert selection.kept

    def test_exchanges_are_dropped_whole(self) -> None:
        ledger = MessageLedger(_conversation(10))
        planner = RetentionPlanner(ContextCompactionConfig())
        ranges = planner.split(ledger, _plan(), generation=0)
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=0)
        for index in selection.dropped:
            assert ledger.group_of(index) <= set(selection.dropped)

    def test_what_survives_is_a_valid_message_list(self) -> None:
        ledger = MessageLedger(_conversation(10, pin_at=4))
        planner = RetentionPlanner(ContextCompactionConfig(), [PINNED_SKILL])
        ranges = planner.split(ledger, _plan(), generation=1)
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=0)
        assert HistoryTrimmer.structural_errors(project(ledger.messages, selection.kept)) == []

    def test_the_survivors_start_at_a_user_message(self) -> None:
        """Not cosmetic: adjacency closure discards everything before the first
        user message, so a selection opening on a pinned tool exchange would
        lose the pinned body on the way out."""
        ledger = MessageLedger(_conversation(10, pin_at=4))
        planner = RetentionPlanner(ContextCompactionConfig(), [PINNED_SKILL])
        ranges = planner.split(ledger, _plan(), generation=1)
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=0)
        assert ledger.entries[selection.kept[0]].role == "user"
        assert set(ranges.hole_ids) <= {m for m in range(len(ledger))} & set(selection.kept)
        assert PINNED_BODY in [m.get("content") for m in project(ledger.messages, selection.kept)]

    def test_fixed_overhead_counts_against_the_budget(self) -> None:
        """The trigger measures the whole prompt, so the compactor has to as
        well -- system, tools and the turn's own message are not free."""
        ledger = MessageLedger(_conversation(10))
        planner = RetentionPlanner(ContextCompactionConfig())
        ranges = planner.split(ledger, _plan(), generation=0)
        budget = ledger.total_tokens
        assert planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=budget).dropped == ()
        assert planner.keep_within(ledger, ranges, fixed_tokens=500, budget_tokens=budget).dropped != ()

    def test_an_empty_history_selects_nothing(self) -> None:
        ledger = MessageLedger([])
        planner = RetentionPlanner(ContextCompactionConfig())
        ranges = planner.split(ledger, _plan(), generation=0)
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=0)
        assert (selection.kept, selection.dropped) == ((), ())

    @pytest.mark.parametrize("boundary", ["tool_pair", "turn"])
    def test_both_boundary_modes_produce_a_valid_list(self, boundary: str) -> None:
        ledger = MessageLedger(_conversation(10))
        cfg = ContextCompactionConfig(boundary=boundary)
        planner = RetentionPlanner(cfg)
        ranges = planner.split(ledger, _plan(cfg=cfg), generation=0)
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=0)
        assert HistoryTrimmer.structural_errors(project(ledger.messages, selection.kept)) == []

    def test_the_turn_boundary_cuts_only_at_user_messages(self) -> None:
        ledger = MessageLedger(_conversation(10))
        cfg = ContextCompactionConfig(boundary="turn")
        ranges = RetentionPlanner(cfg).split(ledger, _plan(cfg=cfg), generation=0)
        for edge in (ranges.compact.start, ranges.compact.stop):
            assert edge in (0, len(ledger)) or ledger.entries[edge].role == "user"


# ---------------------------------------------------------------------------
# A session that reuses tool-call ids
# ---------------------------------------------------------------------------


def _reused_ids(turns: int = 10) -> list[dict]:
    """Every exchange answers ``call_0`` -- what a streaming upstream that
    numbers calls per response leaves in the log."""
    messages: list[dict] = []
    for index in range(turns):
        messages.append({"role": "user", "content": f"question {index}"})
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_0", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": "call_0", "name": "read_file", "content": f"r{index}"})
        messages.append({"role": "assistant", "content": f"answer {index}"})
    return messages


class TestReusedToolCallIds:
    def test_each_result_pairs_with_the_call_just_before_it(self) -> None:
        ledger = MessageLedger(_reused_ids(3))
        assert ledger.group_of(5) == frozenset({5, 6})
        assert ledger.group_of(1) == frozenset({1, 2})

    def test_every_exchange_boundary_is_still_a_cut(self) -> None:
        ledger = MessageLedger(_reused_ids(3))
        for start in (0, 4, 8):
            assert ledger.align_backward(start, "tool_pair") == start
        assert ledger.align_backward(6, "tool_pair") == 5

    def test_emptying_compact_still_produces_a_valid_list(self) -> None:
        ledger = MessageLedger(_reused_ids(10))
        planner = RetentionPlanner(ContextCompactionConfig())
        ranges = planner.split(ledger, _plan(), generation=0)
        selection = planner.keep_within(ledger, ranges, fixed_tokens=0, budget_tokens=0)

        assert selection.dropped
        assert HistoryTrimmer.structural_errors(project(ledger.messages, selection.kept)) == []
