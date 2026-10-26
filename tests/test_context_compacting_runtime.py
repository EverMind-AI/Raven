"""The compacting history builder, end to end -- a turn, a compaction, an overflow.

The pieces are tested apart in the trigger, retention, pruner and checkpoint
files. What is only visible here is the pipeline: a prompt measured whole, a
selection made from it, a message list a provider would accept coming out of
the other end -- and the same list coming out again on the next turn, and in
the next process.

That last part is what this file adds over the pieces. The history a provider
cached is only worth caching if the next turn sends it again byte for byte, so
three things are asserted across turns rather than within one: a pruned body
stays pruned, a retired span stays retired, and a builder that has never seen
the session reads both back off the workspace instead of recompacting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.config.raven import (
    ContextCacheConfig,
    ContextCompactionConfig,
    ContextConfig,
    ContextHistoryConfig,
    ContextPruneConfig,
    ContextSummaryConfig,
)
from raven.context_engine import headroom
from raven.context_engine.history.compacting import CompactingHistoryBuilder
from raven.context_engine.history.compacting.store import CHECKPOINT_DIR, STATE_FILE, CheckpointStore
from raven.context_engine.history.markers import HISTORY_CHECKPOINT_KEY, HISTORY_PINNED_KEY, HISTORY_PRIVATE_KEYS
from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.contracts.assembled import TokenBudget
from raven.contracts.context import AssembledPrefix, AssemblyContext, SessionRevision
from raven.providers.prompt_cache import COMPACTION_BOUNDARY_KEY
from raven.utils.refs import SourceRef
from raven.utils.tokens import estimate_prompt_tokens

WINDOW = 32_768
LOG_ID = "0123456789abcdef" * 2
PINNED_SKILL = "local/subagent-dag-orchestration"
# Over ``prune.minChars`` on purpose: a pinned body has to be exempt from the
# pruner as well as from removal, and one too small to prune would not say so.
PINNED_BODY = "PINNED GUIDE BODY: every dag wiring rule, verbatim, " + ("and then some " * 400)
FILLER = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod " * 20
BIG_RESULT = ("Traceback (most recent call last):\n  File auth.ts, line 214, in refresh\n" * 600)[:40_000]


class _StubProvider:
    api_key = "test"

    def get_default_model(self) -> str:
        return "stub"

    def wire_model_id(self, model: str) -> str:
        return model


@pytest.fixture(autouse=True)
def _forget_learned_headroom():
    headroom.reset()
    yield
    headroom.reset()


def _pinned_fetch(call_id: str = "pin-1") -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "use_skill", "arguments": json.dumps({"skill_id": PINNED_SKILL})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "name": "use_skill", "content": PINNED_BODY},
    ]


def _session(exchanges: int, *, pinned: bool = False, start: int = 0) -> list[dict]:
    """A conversation large enough to matter against ``WINDOW``."""
    messages: list[dict] = []
    for index in range(start, start + exchanges):
        messages.append({"role": "user", "content": f"question {index} {FILLER}"})
        messages.append({"role": "assistant", "content": f"answer {index} {FILLER}"})
        if pinned and index == exchanges // 2:
            messages += _pinned_fetch()
    return messages


def _tool_exchange(call_id: str, tool: str, result: str, arguments: str = "{}") -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool, "arguments": arguments}}],
        },
        {"role": "tool", "tool_call_id": call_id, "name": tool, "content": result},
    ]


def _tool_heavy(*, tokens: int = 30_000) -> list[dict]:
    """A session just over the line, where one tool result is most of the
    excess -- so shortening it is enough and nothing has to be removed.

    Sized by measuring rather than by counting messages, so it stays that case
    if the estimator changes. The three small exchanges at the end are what
    ``keepRecentPairs`` protects; without them the big one is among the last
    three and is protected too.
    """
    messages: list[dict] = [{"role": "user", "content": "why is auth failing?"}]
    messages += _tool_exchange("big-1", "exec", BIG_RESULT, '{"command": "pytest -x"}')
    index = 0
    while estimate_prompt_tokens(messages) < tokens:
        messages.append({"role": "user", "content": f"and then {index} {FILLER}"})
        messages.append({"role": "assistant", "content": f"next {index} {FILLER}"})
        index += 1
    for recent in range(3):
        messages += _tool_exchange(f"r{recent}", "read_file", f"line {recent}")
    return messages


def _budget(window: int = WINDOW, model: str = "stub") -> TokenBudget:
    reserved = headroom.reply_headroom(model, window=window)
    return TokenBudget(
        context_length=window,
        reserved_output=reserved,
        reserved_tools=0,
        reserved_system=200,
        available_history=window - reserved - 200,
    )


def _config(**compacting) -> ContextConfig:
    return ContextConfig(history=ContextHistoryConfig(compacting=ContextCompactionConfig(**compacting)))


def _fold_only() -> ContextConfig:
    return _config(summary=ContextSummaryConfig(mode="off"))


def _builder(
    workspace: Path,
    *,
    config: ContextConfig | None = None,
    window: int = WINDOW,
    provider: object | None = None,
) -> CompactingHistoryBuilder:
    return CompactingHistoryBuilder(
        workspace,
        config or ContextConfig(),
        provider or _StubProvider(),  # type: ignore[arg-type]
        "stub",
        window,
        lambda: [],
    )


def _revision(session: list[dict], *, log_id: str = LOG_ID, generation: int = 0) -> SessionRevision:
    return SessionRevision(log_id=log_id, generation=generation)


def _ctx(
    session: list[dict],
    *,
    session_key: str = "s1",
    window: int = WINDOW,
    revision: SessionRevision | None = None,
    scoped: bool = True,
    user: str = "what now",
) -> AssemblyContext:
    return AssemblyContext(
        session_key=session_key,
        current_message="what now",
        media=None,
        channel=None,
        chat_id=None,
        session_messages=session,
        budget=_budget(window),
        prefix=AssembledPrefix(
            system_prefix="system prompt",
            user_message={"role": "user", "content": user},
            tool_defs=[],
        ),
        session_revision=(revision or _revision(session)) if scoped else None,
    )


def _request(history: list[dict]) -> list[dict]:
    """The request a loop would send for this history, private keys stripped."""
    clean = [{k: v for k, v in m.items() if k not in HISTORY_PRIVATE_KEYS} for m in history]
    return [{"role": "system", "content": "system prompt"}, *clean, {"role": "user", "content": "what now"}]


def _checkpoints(history: list[dict]) -> list[dict]:
    return [m for m in history if isinstance(m.get("content"), str) and "<compacted-" in m["content"]]


# ---------------------------------------------------------------------------
# A turn
# ---------------------------------------------------------------------------


class TestATurn:
    async def test_an_empty_session_is_an_empty_history(self, tmp_path: Path) -> None:
        segment = await _builder(tmp_path).build(_ctx([]))
        assert segment.history == []
        assert segment.text == ""

    async def test_it_contributes_nothing_to_the_system_prompt(self, tmp_path: Path) -> None:
        """The history slot and nothing else: a per-turn string in the system
        prompt would sit ahead of the whole conversation in every cache prefix."""
        segment = await _builder(tmp_path).build(_ctx(_session(4)))
        assert segment.text == ""

    async def test_a_prompt_under_the_line_is_handed_over_whole(self, tmp_path: Path) -> None:
        session = _session(4)
        segment = await _builder(tmp_path).build(_ctx(session))
        assert segment.meta["path"] == "compacting"
        assert segment.meta["compaction_action"] == "pass"
        assert segment.meta["compacted_away"] == 0
        assert segment.history == session

    async def test_phase_a_is_refused(self, tmp_path: Path) -> None:
        """Only phase B reads the history slot, so a builder run without a
        prefix would have its history silently dropped."""
        ctx = _ctx(_session(2))
        with pytest.raises(RuntimeError, match="prefix"):
            await _builder(tmp_path).build(AssemblyContext(**{**ctx.__dict__, "prefix": None}))

    async def test_it_satisfies_the_segment_builder_protocol(self, tmp_path: Path) -> None:
        from raven.contracts.context import SegmentBuilder

        builder = _builder(tmp_path)
        assert isinstance(builder, SegmentBuilder)
        assert (builder.name, builder.needs_prefix, builder.stable) == ("compacting", True, False)

    async def test_without_a_revision_it_budgets_and_mints_nothing(self, tmp_path: Path) -> None:
        """Every ref names a revision of the log; with none there is nothing a
        ref could resolve against, so nothing is pruned, folded or stored."""
        session = _tool_heavy(tokens=60_000)
        segment = await _builder(tmp_path).build(_ctx(session, scoped=False))

        assert segment.meta["compaction_action"] == "unscoped"
        assert "[[ref " not in json.dumps(segment.history)
        assert HistoryTrimmer.structural_errors(segment.history) == []
        assert not (tmp_path / "memory").exists()

    async def test_a_model_switch_reaches_its_trimmer(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        builder.set_context_window(4_096)
        assert (builder.context_window_tokens, builder.trimmer.context_window_tokens) == (4_096, 4_096)

        new_provider = _StubProvider()
        builder.set_provider(new_provider, "other-model")  # type: ignore[arg-type]
        assert builder.provider is new_provider
        assert (builder.model, builder.trimmer.model) == ("other-model", "other-model")


# ---------------------------------------------------------------------------
# A compaction
# ---------------------------------------------------------------------------


class TestACompaction:
    async def test_a_prompt_over_the_line_comes_back_under_target(self, tmp_path: Path) -> None:
        segment = await _builder(tmp_path).build(_ctx(_session(80)))
        meta = segment.meta
        assert meta["compaction_action"] == "compact"
        assert meta["prompt_tokens"] > meta["trigger_tokens"]
        assert meta["history_tokens"] <= meta["target_tokens"]
        assert meta["history_kept"] < meta["history_total"]

    async def test_the_trigger_is_the_window_less_the_reply_headroom_here(self, tmp_path: Path) -> None:
        """A 32768 window is below the 40960 crossover, so the static floor
        governs and the 0.90 ratio is inert."""
        segment = await _builder(tmp_path).build(_ctx(_session(80)))
        assert segment.meta["trigger_tokens"] == WINDOW - headroom.DEFAULT_MIN_REPLY_HEADROOM_TOKENS
        assert segment.meta["headroom_clamped"] is True

    async def test_a_large_window_triggers_on_the_adaptive_ratio_instead(self, tmp_path: Path) -> None:
        segment = await _builder(tmp_path, window=200_000).build(_ctx(_session(4), window=200_000))
        assert segment.meta["trigger_tokens"] == 176_136
        assert segment.meta["headroom_clamped"] is False

    async def test_what_survives_is_a_list_a_provider_would_accept(self, tmp_path: Path) -> None:
        segment = await _builder(tmp_path).build(_ctx(_session(80)))
        assert HistoryTrimmer.structural_errors(segment.history) == []
        assert segment.history[0]["role"] == "user"

    async def test_the_recent_messages_are_the_ones_kept(self, tmp_path: Path) -> None:
        session = _session(80)
        segment = await _builder(tmp_path).build(_ctx(session))
        assert segment.history[-1]["content"] == session[-1]["content"]
        assert segment.history[-2]["content"] == session[-2]["content"]

    async def test_the_opening_exchange_is_kept_the_first_time_and_not_after(self, tmp_path: Path) -> None:
        """Head decay: the anchor is worth its tokens once, and forever would
        make it the one thing that never ages. It takes a *second* compaction
        to see, which is why the session grows between the two calls."""
        builder = _builder(tmp_path, config=_fold_only())
        session = _session(80)
        first = await builder.build(_ctx(session))
        second = await builder.build(_ctx(_session(120)))
        assert first.history[0]["content"] == session[0]["content"]
        assert second.history[0]["content"] != session[0]["content"]
        assert second.meta["compaction_generation"] == 2

    async def test_replaying_a_compacted_session_does_not_compact_it_again(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path, config=_fold_only())
        session = _session(80)
        first = await builder.build(_ctx(session))
        second = await builder.build(_ctx(session))
        assert (first.meta["compaction_action"], second.meta["compaction_action"]) == ("compact", "pass")
        assert second.meta["compaction_generation"] == 1
        assert second.history == first.history

    async def test_each_session_counts_its_own_compactions(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path, config=_fold_only())
        session = _session(80)
        await builder.build(_ctx(session, session_key="a"))
        segment = await builder.build(_ctx(session, session_key="b"))
        assert segment.meta["compaction_generation"] == 1

    async def test_the_same_input_compacts_the_same_way(self, tmp_path: Path) -> None:
        """No model in the loop, so the selection is reproducible -- which is
        what makes a compaction bug reproducible too."""
        session = _session(80)
        first = await _builder(tmp_path / "a", config=_fold_only()).build(_ctx(session))
        second = await _builder(tmp_path / "b", config=_fold_only()).build(_ctx(session))
        assert first.history == second.history

    async def test_the_checkpoint_never_reaches_the_session_log(self, tmp_path: Path) -> None:
        session = _session(80)
        await _builder(tmp_path, config=_fold_only()).build(_ctx(session))
        assert not any("<compacted-" in str(m.get("content")) for m in session)
        assert not any(set(m) & HISTORY_PRIVATE_KEYS for m in session)


class TestTheSummaryBreakpointFlag:
    """Where the next summary is expected to end, flagged for the cache optimizer.

    One turn's half of it. That the next summary really replays the mark is
    asserted where the summarising request is, in the checkpoint tests.
    """

    PAST_TARGET = 40
    """Exchanges that put the prompt past the compaction target and under the trigger."""

    ON = ContextConfig(cache=ContextCacheConfig(compaction_breakpoint=True))

    @staticmethod
    def _flagged(history: list[dict]) -> list[int]:
        return [i for i, m in enumerate(history) if m.get(COMPACTION_BOUNDARY_KEY)]

    async def test_a_prompt_under_the_target_flags_nothing(self, tmp_path: Path) -> None:
        segment = await _builder(tmp_path, config=self.ON).build(_ctx(_session(4)))
        assert self._flagged(segment.history) == []

    async def test_past_the_target_one_message_before_the_tail_is_flagged_and_nothing_else_changes(
        self, tmp_path: Path
    ) -> None:
        session = _session(self.PAST_TARGET)
        segment = await _builder(tmp_path, config=self.ON).build(_ctx(session))

        meta = segment.meta
        assert meta["compaction_action"] == "pass"
        assert meta["target_tokens"] < meta["prompt_tokens"] < meta["trigger_tokens"]
        [flagged] = self._flagged(segment.history)
        assert 0 < flagged < len(session) - ContextCompactionConfig().tail_keep_messages
        assert [{k: v for k, v in m.items() if k != COMPACTION_BOUNDARY_KEY} for m in segment.history] == session

    async def test_the_flag_only_moves_forward_as_the_log_grows(self, tmp_path: Path) -> None:
        """A longer log is cut at or past where a shorter one would be, which
        is the whole reason a mark placed a turn early is still inside the cut."""
        builder = _builder(tmp_path, config=self.ON)
        positions = []
        for exchanges in range(self.PAST_TARGET, 56, 2):
            segment = await builder.build(_ctx(_session(exchanges)))
            assert segment.meta["compaction_action"] == "pass"
            positions += self._flagged(segment.history)

        assert len(positions) == len(range(self.PAST_TARGET, 56, 2))
        assert positions == sorted(positions)

    async def test_the_flag_is_off_by_default(self, tmp_path: Path) -> None:
        """On Anthropic the flag evicts the mark the next turn reads its history
        back through, so a default-on flag re-writes the history every turn."""
        segment = await _builder(tmp_path).build(_ctx(_session(self.PAST_TARGET)))
        assert self._flagged(segment.history) == []

    async def test_a_turn_that_removed_messages_flags_nothing(self, tmp_path: Path) -> None:
        """It has just come down to the target; the next summary is a whole
        climb to the trigger away, past any cache lifetime."""
        config = _fold_only()
        config.cache.compaction_breakpoint = True
        segment = await _builder(tmp_path, config=config).build(_ctx(_session(80)))
        assert segment.meta["compaction_action"] == "compact"
        assert self._flagged(segment.history) == []


class TestPinnedBodies:
    async def test_a_pinned_body_survives_compaction_verbatim(self, tmp_path: Path) -> None:
        segment = await _builder(tmp_path, config=_fold_only()).build(_ctx(_session(80, pinned=True)))
        assert segment.meta["compaction_action"] == "compact"
        assert segment.meta["pinned_held"] == 2
        assert PINNED_BODY in [m.get("content") for m in segment.history]

    async def test_it_survives_after_the_head_stops_being_protected(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path, config=_fold_only())
        session = _session(80, pinned=True)
        await builder.build(_ctx(session))
        segment = await builder.build(_ctx([*session, *_session(40, start=200)]))
        assert segment.meta["compaction_generation"] == 2
        assert PINNED_BODY in [m.get("content") for m in segment.history]
        assert segment.history[0]["role"] == "user"
        assert HistoryTrimmer.structural_errors(segment.history) == []

    async def test_the_call_that_fetched_it_survives_with_it(self, tmp_path: Path) -> None:
        segment = await _builder(tmp_path, config=_fold_only()).build(_ctx(_session(80, pinned=True)))
        calls = [m for m in segment.history if m.get("tool_calls")]
        assert any(tc["id"] == "pin-1" for m in calls for tc in m["tool_calls"])

    async def test_the_pinned_exchange_is_marked_for_the_window(self, tmp_path: Path) -> None:
        """In-turn window management cannot tell a pinned exchange from any
        other tool result without being told."""
        segment = await _builder(tmp_path, config=_fold_only()).build(_ctx(_session(80, pinned=True)))
        marked = [m for m in segment.history if m.get(HISTORY_PINNED_KEY)]
        assert [m["role"] for m in marked] == ["assistant", "tool"]
        assert marked[1]["content"] == PINNED_BODY

    async def test_turning_pinning_off_lets_the_body_go(self, tmp_path: Path) -> None:
        config = _config(honor_pinned=False, summary=ContextSummaryConfig(mode="off"))
        segment = await _builder(tmp_path, config=config).build(_ctx(_session(80, pinned=True)))
        assert segment.meta["pinned_held"] == 0
        assert PINNED_BODY not in [m.get("content") for m in segment.history]


# ---------------------------------------------------------------------------
# The ladder: shorten first, remove only what shortening could not buy
# ---------------------------------------------------------------------------


class TestTheLadder:
    async def test_shortening_one_result_is_enough_to_stay_whole(self, tmp_path: Path) -> None:
        session = _tool_heavy()
        segment = await _builder(tmp_path).build(_ctx(session))

        assert segment.meta["pruned_messages"] >= 1
        assert segment.meta["compacted_away"] == 0
        assert segment.meta["compaction_action"] == "pass"
        assert len(segment.history) == len(session)
        assert HistoryTrimmer.structural_errors(segment.history) == []

    async def test_the_forty_kilobyte_result_is_one_line_that_reads_back_whole(self, tmp_path: Path) -> None:
        session = _tool_heavy()
        segment = await _builder(tmp_path).build(_ctx(session))

        record = segment.history[2]["content"]
        assert BIG_RESULT not in json.dumps(segment.history)
        assert len(record) <= 160
        ref = SourceRef.parse(record)
        assert ref == SourceRef("session", _revision(session).scope, 2, 2)
        assert session[ref.lo]["content"] == BIG_RESULT

    async def test_it_says_what_shortening_bought(self, tmp_path: Path) -> None:
        segment = await _builder(tmp_path).build(_ctx(_tool_heavy()))

        assert segment.meta["prune_reclaimed_tokens"] > 5_000
        assert segment.meta["prompt_tokens"] < segment.meta["trigger_tokens"]

    async def test_without_the_pruner_the_same_turn_loses_messages(self, tmp_path: Path) -> None:
        config = _config(prune=ContextPruneConfig(enabled=False), summary=ContextSummaryConfig(mode="off"))
        segment = await _builder(tmp_path, config=config).build(_ctx(_tool_heavy()))

        assert segment.meta["pruned_messages"] == 0
        assert segment.meta["compacted_away"] > 0
        assert segment.meta["compaction_action"] == "compact"

    async def test_pruning_twice_changes_nothing(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        session = _tool_heavy()

        first = await builder.build(_ctx(session))
        second = await builder.build(_ctx(session))

        assert first.history == second.history
        assert second.meta["pruned_messages"] == 0
        assert second.meta["pruned_total"] == first.meta["pruned_total"] >= 1
        assert second.meta["compaction_generation"] == 0

    async def test_a_turn_under_the_line_is_handed_over_untouched(self, tmp_path: Path) -> None:
        session = [{"role": "user", "content": "read it"}, *_tool_exchange("c1", "read_file", BIG_RESULT)]
        segment = await _builder(tmp_path, window=200_000).build(_ctx(session, window=200_000))

        assert segment.meta["compaction_action"] == "pass"
        assert segment.meta["pruned_messages"] == 0
        assert segment.history[2]["content"] == BIG_RESULT

    async def test_a_pinned_body_is_not_shortened_either(self, tmp_path: Path) -> None:
        session = _tool_heavy()
        session[1:1] = _pinned_fetch()
        segment = await _builder(tmp_path).build(_ctx(session))

        assert len(PINNED_BODY) > ContextPruneConfig().min_chars
        assert PINNED_BODY in [m.get("content") for m in segment.history]

    async def test_when_shortening_is_not_enough_messages_still_go(self, tmp_path: Path) -> None:
        session = _tool_heavy(tokens=60_000)
        segment = await _builder(tmp_path, config=_fold_only()).build(_ctx(session))

        assert segment.meta["pruned_messages"] >= 1
        assert segment.meta["compacted_away"] > 0
        assert segment.meta["history_tokens"] <= segment.meta["target_tokens"]


# ---------------------------------------------------------------------------
# Across turns: the prefix a provider cached is the prefix it is sent again
# ---------------------------------------------------------------------------


def _grow(session: list[dict], turn: int) -> list[dict]:
    return [
        *session,
        {"role": "user", "content": f"small follow-up {turn}"},
        {"role": "assistant", "content": f"small answer {turn}"},
    ]


class TestStickyHistory:
    async def test_a_pruned_body_never_flows_back_in(self, tmp_path: Path) -> None:
        """The defect this guards: a turn that pruned under pressure is followed
        by one that is under the trigger even with the body -- here because the
        first turn's own message was long -- so a strategy that re-decides
        every turn ships the original body again, and the prefix a provider
        had just cached changes under it."""
        builder = _builder(tmp_path)
        session = _tool_heavy(tokens=26_000)
        first = await builder.build(_ctx(session, user="paste " + FILLER * 25))
        assert first.meta["pruned_messages"] >= 1

        for turn in range(3):
            session = _grow(session, turn)
            segment = await builder.build(_ctx(session))
            assert segment.meta["compaction_action"] == "pass"
            assert segment.meta["pruned_messages"] == 0, "under the trigger, so nothing new is pruned"
            assert json.dumps(segment.history[: len(first.history)]) == json.dumps(first.history)
            assert BIG_RESULT not in json.dumps(segment.history)

    async def test_three_turns_after_a_compaction_send_the_same_prefix(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path, config=_fold_only())
        session = _session(80)
        compacted = await builder.build(_ctx(session))
        assert compacted.meta["compaction_action"] == "compact"

        for turn in range(3):
            session = _grow(session, turn)
            segment = await builder.build(_ctx(session))
            assert segment.meta["compaction_action"] == "pass"
            assert segment.meta["compaction_generation"] == 1
            assert json.dumps(segment.history[: len(compacted.history)]) == json.dumps(compacted.history)

    async def test_a_new_process_resumes_without_recompacting(self, tmp_path: Path) -> None:
        """Everything that decides the history is read back off the workspace,
        so a builder that never saw the session sends the same bytes."""
        session = _session(80)
        before = await _builder(tmp_path, config=_fold_only()).build(_ctx(session))

        after = await _builder(tmp_path, config=_fold_only()).build(_ctx(session))

        assert after.meta["compaction_action"] == "pass"
        assert after.meta["compaction_generation"] == 1
        assert after.meta["checkpoint_id"] == before.meta["checkpoint_id"]
        assert json.dumps(after.history) == json.dumps(before.history)

    async def test_a_new_process_keeps_the_pruned_lines_too(self, tmp_path: Path) -> None:
        session = _tool_heavy()
        before = await _builder(tmp_path).build(_ctx(session))

        after = await _builder(tmp_path).build(_ctx(_grow(session, 0)))

        assert after.meta["pruned_messages"] == 0
        assert json.dumps(after.history[: len(before.history)]) == json.dumps(before.history)

    async def test_the_state_on_disk_names_what_it_restores(self, tmp_path: Path) -> None:
        session = _tool_heavy(tokens=60_000)
        segment = await _builder(tmp_path, config=_fold_only()).build(_ctx(session))

        state = json.loads((CheckpointStore(tmp_path / CHECKPOINT_DIR).folder("s1") / STATE_FILE).read_text())
        assert state["scope"] == _revision(session).scope
        assert state["generation"] == 1
        assert state["checkpoint_id"] == segment.meta["checkpoint_id"]
        assert state["pruned"]
        chain = CheckpointStore(tmp_path / CHECKPOINT_DIR).chain("s1")
        assert [cp.checkpoint_id for cp in chain] == [segment.meta["checkpoint_id"]]
        assert chain[0].retired

    async def test_a_new_log_starts_from_nothing(self, tmp_path: Path) -> None:
        """``/new`` gives the log a new id, and an undo a new generation: either
        way a stored ordinal may now name a different message."""
        session = _session(80)
        builder = _builder(tmp_path, config=_fold_only())
        await builder.build(_ctx(session))

        for revision in (
            _revision(session, log_id="f" * 32),
            _revision(session, generation=1),
        ):
            segment = await _builder(tmp_path, config=_fold_only()).build(_ctx(session, revision=revision))
            assert segment.meta["compaction_generation"] == 1
            assert segment.meta["compaction_action"] == "compact"
            assert segment.meta["checkpoint_covers"].startswith(f"[[ref session/{revision.scope}#")

    async def test_a_checkpoint_past_the_end_of_the_log_is_ignored(self, tmp_path: Path) -> None:
        session = _session(80)
        builder = _builder(tmp_path, config=_fold_only())
        compacted = await builder.build(_ctx(session))
        covers = SourceRef.parse(compacted.meta["checkpoint_covers"])
        assert covers is not None

        short = session[: covers.hi]
        segment = await _builder(tmp_path, config=_fold_only()).build(_ctx(short, revision=_revision(session)))

        assert segment.meta["checkpoint_id"] != compacted.meta["checkpoint_id"]
        assert compacted.meta["checkpoint_id"] not in json.dumps(segment.history)

    async def test_nothing_is_written_until_something_has_to_be_remembered(self, tmp_path: Path) -> None:
        await _builder(tmp_path).build(_ctx(_session(4)))
        assert not (tmp_path / "memory").exists()

    async def test_the_checkpoint_is_marked_for_the_window(self, tmp_path: Path) -> None:
        segment = await _builder(tmp_path, config=_fold_only()).build(_ctx(_session(80)))
        (checkpoint,) = _checkpoints(segment.history)
        assert checkpoint[HISTORY_CHECKPOINT_KEY] is True
        assert checkpoint["role"] == "user"
        assert not any(m.get(HISTORY_CHECKPOINT_KEY) for m in segment.history if m is not checkpoint)


# ---------------------------------------------------------------------------
# A session that reuses tool-call ids
# ---------------------------------------------------------------------------


def _reused_ids(exchanges: int) -> list[dict]:
    messages: list[dict] = []
    for index in range(exchanges):
        messages.append({"role": "user", "content": f"question {index} {FILLER}"})
        messages += _tool_exchange("call_0", "exec", f"output {index} {FILLER}", '{"command": "make"}')
        messages.append({"role": "assistant", "content": f"answer {index}"})
    return messages


class TestReusedToolCallIds:
    async def test_compacting_it_terminates_with_a_valid_list(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path, config=_fold_only())
        session = _reused_ids(60)
        segment = await builder.build(_ctx(session))

        assert segment.meta["compaction_action"] == "compact"
        assert HistoryTrimmer.structural_errors(segment.history) == []
        assert HistoryTrimmer.structural_errors(_request(segment.history)) == []

        again = await builder.build(_ctx(_reused_ids(90)))
        assert HistoryTrimmer.structural_errors(_request(again.history)) == []

    async def test_with_large_results_pruning_and_removal_both_stay_valid(self, tmp_path: Path) -> None:
        session: list[dict] = []
        for index in range(30):
            session.append({"role": "user", "content": f"step {index} {FILLER}"})
            session += _tool_exchange("call_0", "exec", f"{index} " + "x" * 6_000)
        segment = await _builder(tmp_path, config=_fold_only()).build(_ctx(session))

        assert segment.meta["pruned_messages"] >= 1
        assert HistoryTrimmer.structural_errors(_request(segment.history)) == []

    async def test_the_overflow_path_stays_valid_too(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        messages = [{"role": "system", "content": "system prompt"}, *_reused_ids(60)]
        headroom.learn_from_overflow("stub", window=WINDOW, prompt_tokens=estimate_prompt_tokens(messages), step=8_192)

        outcome = await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW)

        assert outcome is not None
        assert HistoryTrimmer.structural_errors(outcome.messages) == []


# ---------------------------------------------------------------------------
# An overflow
# ---------------------------------------------------------------------------


class TestOverflow:
    """Sessions here are sized to *pass* the trigger, because that is the
    interesting case: the strategy read the prompt as fitting and the provider
    disagreed."""

    async def _refused(self, builder: CompactingHistoryBuilder, session: list[dict]) -> list[dict]:
        segment = await builder.build(_ctx(session))
        assert segment.meta["compaction_action"] == "pass", "fixture must be under the line to be interesting"
        messages = [{"role": "system", "content": "system prompt"}, *segment.history]
        headroom.learn_from_overflow(
            "stub",
            window=WINDOW,
            prompt_tokens=estimate_prompt_tokens(messages),
            step=8_192,
        )
        return messages

    async def test_a_refusal_is_answered_with_a_smaller_prompt(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        messages = await self._refused(builder, _session(60))

        outcome = await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW)

        assert outcome is not None
        assert outcome is not None
        assert outcome.reclaimed_tokens > 0
        assert len(outcome.messages) < len(messages)
        assert estimate_prompt_tokens(outcome.messages) < estimate_prompt_tokens(messages)

    async def test_the_reclaimed_prompt_is_still_a_valid_one(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        messages = await self._refused(builder, _session(60, pinned=True))

        outcome = await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW)

        assert outcome is not None
        assert outcome.messages[0]["role"] == "system"
        assert HistoryTrimmer.structural_errors(outcome.messages) == []
        assert PINNED_BODY in [m.get("content") for m in outcome.messages]
        assert any(m.get(HISTORY_PINNED_KEY) for m in outcome.messages)

    async def test_the_overflow_does_not_recur_on_the_next_turn(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path, config=_fold_only())
        session = _session(60)
        messages = await self._refused(builder, session)
        learned = headroom.required_headroom("stub")
        assert learned > headroom.DEFAULT_MIN_REPLY_HEADROOM_TOKENS

        await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW)
        segment = await builder.build(_ctx(session))

        assert segment.meta["trigger_tokens"] == WINDOW - learned
        assert segment.meta["compaction_action"] == "compact"
        assert segment.meta["history_tokens"] <= WINDOW - learned

    async def test_an_overflow_is_not_counted_as_a_compaction(self, tmp_path: Path) -> None:
        """Counting it would decay the head of a history no checkpoint stands
        for: the next real compaction would drop the opening exchange the first
        time instead of the second."""
        builder = _builder(tmp_path, config=_fold_only())
        session = _session(60)
        messages = await self._refused(builder, session)
        outcome = await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW)
        assert outcome is not None

        segment = await builder.build(_ctx(session))

        assert segment.meta["compaction_generation"] == 1
        assert segment.history[0]["content"] == session[0]["content"]

    async def test_a_prompt_the_strategy_thinks_fits_is_left_to_the_loop(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        segment = await builder.build(_ctx(_session(2)))
        messages = [{"role": "system", "content": "system prompt"}, *segment.history]

        assert await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW) is None

    async def test_a_history_with_no_user_message_is_declined(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        messages = [{"role": "system", "content": "system prompt"}]
        messages += [{"role": "assistant", "content": f"a{index} {FILLER}"} for index in range(200)]
        headroom.learn_from_overflow("stub", window=WINDOW, prompt_tokens=estimate_prompt_tokens(messages), step=8_192)

        assert await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW) is None

    async def test_a_message_list_it_does_not_recognise_is_declined(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        assert await builder.on_context_overflow("s1", [], attempt=1, window=WINDOW) is None
        assert await builder.on_context_overflow("s1", [{"role": "user"}], attempt=1, window=WINDOW) is None

    async def test_the_standing_checkpoint_survives_an_overflow_reclaim(self, tmp_path: Path) -> None:
        """Found by its marker, or by its bytes once the marker was stripped,
        and held: it is the only account of everything already compacted."""
        builder = _builder(tmp_path, config=_fold_only())
        segment = await builder.build(_ctx(_session(80)))
        (standing,) = _checkpoints(segment.history)

        for history in (segment.history, _request(segment.history)[1:-1]):
            messages = [{"role": "system", "content": "system prompt"}, *history, *_session(40, start=500)]
            headroom.learn_from_overflow(
                "stub", window=WINDOW, prompt_tokens=estimate_prompt_tokens(messages), step=8_192
            )
            outcome = await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW)

            assert outcome is not None
            assert len(outcome.messages) < len(messages)
            assert any(m.get("content") == standing["content"] for m in outcome.messages)


# ---------------------------------------------------------------------------
# What is retired is what the planner removed, and nothing the trimmer dropped
# ---------------------------------------------------------------------------


def _big_tail() -> list[dict]:
    """A session whose tail holds one result larger than the window, so the
    trimmer has to drop more than the planner removed."""
    session = _session(40)
    session.append({"role": "user", "content": "QUESTION-IN-TAIL please run the big thing"})
    session += _tool_exchange("big", "exec", "".join(f"Y{i} " for i in range(14_000)), '{"command": "dump"}')
    session.append({"role": "assistant", "content": "ANSWER-IN-TAIL it was big"})
    return session


class TestRetirementFollowsThePlanner:
    async def test_nothing_past_the_covered_span_is_retired(self, tmp_path: Path) -> None:
        segment = await _builder(tmp_path, config=_fold_only()).build(_ctx(_big_tail()))
        assert segment.meta["budget_trimmed"] > 0, "the fixture must make the trimmer drop something"

        (checkpoint,) = CheckpointStore(tmp_path / CHECKPOINT_DIR).chain("s1")
        assert checkpoint.retired
        assert checkpoint.covers.lo == checkpoint.retired[0]
        assert checkpoint.covers.hi == checkpoint.retired[-1]

    async def test_what_the_trimmer_dropped_is_decided_again_next_turn(self, tmp_path: Path) -> None:
        """Retired with no ref pointing at it, the tail question would never
        come back; re-decided, it is back as soon as there is room."""
        builder = _builder(tmp_path, config=_fold_only())
        session = _big_tail()
        first = await builder.build(_ctx(session))
        assert "QUESTION-IN-TAIL" not in json.dumps(first.history)

        for turn in range(5):
            session = _grow(session, turn)
        later = await builder.build(_ctx(session))

        assert "QUESTION-IN-TAIL" in json.dumps(later.history)
        assert HistoryTrimmer.structural_errors(_request(later.history)) == []

    async def test_the_checkpoint_keeps_its_place_when_the_head_is_trimmed(self, tmp_path: Path) -> None:
        """Placed by its own first retired ordinal, which every later turn reads
        back unchanged, rather than by whatever this turn happened to drop: the
        history up to and including it is the same bytes turn after turn."""
        config = _config(head_keep_decays=False, summary=ContextSummaryConfig(mode="off"))
        builder = _builder(tmp_path, config=config)
        session = _big_tail()
        first = await builder.build(_ctx(session))
        assert first.meta["budget_trimmed"] > 0

        def through_checkpoint(history: list[dict]) -> str:
            at = next(i for i, m in enumerate(history) if m.get(HISTORY_CHECKPOINT_KEY))
            return json.dumps(history[: at + 1])

        (checkpoint,) = CheckpointStore(tmp_path / CHECKPOINT_DIR).chain("s1")
        assert {0, 1}.isdisjoint(checkpoint.retired), "the protected head is trimmed, not retired"
        for _turn in range(3):
            segment = await builder.build(_ctx(session))
            assert segment.meta["checkpoint_id"] == first.meta["checkpoint_id"]
            assert through_checkpoint(segment.history) == through_checkpoint(first.history)

    async def test_a_turn_that_writes_no_checkpoint_retires_nothing_new(self, tmp_path: Path) -> None:
        """A deliberate departure from the strategy this was ported from, which
        extended the standing checkpoint's retired set whenever it removed
        anything: with folds and summaries off nothing speaks for this turn's
        removal, so it is re-decided every turn instead."""
        session = _session(80)
        await _builder(tmp_path, config=_fold_only()).build(_ctx(session))
        (standing,) = CheckpointStore(tmp_path / CHECKPOINT_DIR).chain("s1")

        nothing = _config(summary=ContextSummaryConfig(mode="off", fold=False))
        grown = [*session, *_session(60, start=300)]
        segment = await _builder(tmp_path, config=nothing).build(_ctx(grown))

        assert segment.meta["compacted_away"] > len(standing.retired)
        assert segment.meta["checkpoint_id"] == standing.checkpoint_id
        (still,) = CheckpointStore(tmp_path / CHECKPOINT_DIR).chain("s1")
        assert still.retired == standing.retired


# ---------------------------------------------------------------------------
# A reader that is behind another process writes nothing
# ---------------------------------------------------------------------------


class TestALaggingReader:
    async def test_a_shorter_log_of_the_same_revision_reads_but_never_writes(self, tmp_path: Path) -> None:
        session = _tool_heavy(tokens=60_000)
        writer = _builder(tmp_path, config=_fold_only())
        first = await writer.build(_ctx(session))
        folder = CheckpointStore(tmp_path / CHECKPOINT_DIR).folder("s1")
        files = {path.name: path.read_bytes() for path in folder.glob("*.json")}

        covers = SourceRef.parse(first.meta["checkpoint_covers"])
        assert covers is not None
        # Short enough that the standing checkpoint names ordinals this reader
        # does not have, and still over the trigger: a reader that decided
        # anything here would recompact and overwrite the writer's state.
        behind = session[: covers.hi]
        assert estimate_prompt_tokens(behind) > first.meta["trigger_tokens"]
        lagging = await _builder(tmp_path, config=_fold_only()).build(_ctx(behind, revision=_revision(session)))

        assert {path.name: path.read_bytes() for path in folder.glob("*.json")} == files
        assert lagging.meta["compaction_action"] == "pass"
        assert BIG_RESULT not in json.dumps(lagging.history), "the sticky prune still applies"
        assert HistoryTrimmer.structural_errors(_request(lagging.history)) == []

        again = await writer.build(_ctx(session))
        assert json.dumps(again.history) == json.dumps(first.history)
        assert again.meta["compaction_action"] == "pass"

    async def test_two_writers_add_to_each_others_pruned_set(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path / CHECKPOINT_DIR)
        revision = _revision(_session(10))
        from raven.context_engine.history.compacting.store import CompactingState

        store.save("s1", CompactingState(scope=revision.scope, message_count=20, pruned=frozenset({3})))
        store.save("s1", CompactingState(scope=revision.scope, message_count=20, pruned=frozenset({7})))

        loaded = store.load("s1", revision, message_count=20)
        assert loaded.pruned == frozenset({3, 7})


# ---------------------------------------------------------------------------
# The store against corrupt and crowded folders
# ---------------------------------------------------------------------------


class TestTheStoreUnderStress:
    async def test_a_huge_pruned_run_is_clipped_before_it_is_expanded(self, tmp_path: Path) -> None:
        import time

        session = _tool_heavy()
        await _builder(tmp_path).build(_ctx(session))
        path = CheckpointStore(tmp_path / CHECKPOINT_DIR).folder("s1") / STATE_FILE
        state = json.loads(path.read_text())
        state["pruned"] = [[0, 10**12]]
        path.write_text(json.dumps(state))

        started = time.perf_counter()
        loaded = CheckpointStore(tmp_path / CHECKPOINT_DIR).load("s1", _revision(session), message_count=len(session))

        assert time.perf_counter() - started < 1.0
        assert loaded.pruned == frozenset(range(len(session)))

    async def test_a_huge_retired_run_makes_the_checkpoint_unreadable(self, tmp_path: Path) -> None:
        import time

        session = _session(80)
        await _builder(tmp_path, config=_fold_only()).build(_ctx(session))
        store = CheckpointStore(tmp_path / CHECKPOINT_DIR)
        (path,) = [p for p in store.folder("s1").glob("*.json") if p.name != STATE_FILE]
        data = json.loads(path.read_text())
        data["retired"] = [[0, 10**12]]
        path.write_text(json.dumps(data))

        started = time.perf_counter()
        assert store.chain("s1") == []
        assert store.load("s1", _revision(session), message_count=len(session)).checkpoint is None
        assert time.perf_counter() - started < 1.0

    async def test_of_two_files_of_one_generation_the_newest_written_stands(self, tmp_path: Path) -> None:
        from dataclasses import replace

        session = _session(80)
        await _builder(tmp_path, config=_fold_only()).build(_ctx(session))
        store = CheckpointStore(tmp_path / CHECKPOINT_DIR)
        (original,) = store.chain("s1")
        for suffix in ("zzzz0000", "0000aaaa"):
            store.write(replace(original, checkpoint_id=f"cp-001-{suffix}"))
        path = store.folder("s1") / STATE_FILE
        state = json.loads(path.read_text())
        state["checkpoint_id"] = "cp-001-missing"
        path.write_text(json.dumps(state))

        loaded = store.load("s1", _revision(session), message_count=len(session))

        assert loaded.checkpoint is not None
        assert loaded.checkpoint.checkpoint_id == "cp-001-0000aaaa"

    async def test_without_a_named_checkpoint_the_folder_is_not_read(self, tmp_path: Path, monkeypatch) -> None:
        session = _tool_heavy()
        await _builder(tmp_path).build(_ctx(session))
        store = CheckpointStore(tmp_path / CHECKPOINT_DIR)
        reads: list[Path] = []
        monkeypatch.setattr(CheckpointStore, "_read", staticmethod(lambda path, **_: reads.append(path)))

        loaded = store.load("s1", _revision(session), message_count=len(session))

        assert loaded.checkpoint is None and loaded.pruned
        assert reads == []

    async def test_a_new_revision_clears_the_files_of_the_old_one(self, tmp_path: Path) -> None:
        session = _session(80)
        await _builder(tmp_path, config=_fold_only()).build(_ctx(session))
        renewed = _revision(session, log_id="f" * 32)
        await _builder(tmp_path, config=_fold_only()).build(_ctx(session, revision=renewed))

        chain = CheckpointStore(tmp_path / CHECKPOINT_DIR).chain("s1")
        assert [cp.scope for cp in chain] == [renewed.scope]

    async def test_keys_that_sanitise_alike_keep_separate_state(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path / CHECKPOINT_DIR)
        assert store.folder("cli:a") != store.folder("cli_a")

        session = _tool_heavy()
        await _builder(tmp_path).build(_ctx(session, session_key="cli:a"))
        loaded = store.load("cli_a", _revision(session), message_count=len(session))

        assert loaded.pruned == frozenset()


# ---------------------------------------------------------------------------
# Cost of a long session, and what the overflow path hands back
# ---------------------------------------------------------------------------


class TestCostAndShape:
    async def test_a_pass_turn_prices_only_what_is_new(self, tmp_path: Path, monkeypatch) -> None:
        from raven.context_engine.history.compacting import ledger as ledger_module
        from raven.context_engine.history.compacting import runtime as runtime_module

        builder = _builder(tmp_path)
        session = _tool_heavy()
        await builder.build(_ctx(session))

        calls: list[int] = []
        real = runtime_module.estimate_message_tokens

        def counted(message):
            calls.append(1)
            return real(message)

        monkeypatch.setattr(runtime_module, "estimate_message_tokens", counted)
        monkeypatch.setattr(ledger_module, "estimate_message_tokens", counted)
        await builder.build(_ctx(_grow(session, 0)))

        assert len(calls) <= 2, "two new messages; everything else is already priced"

    async def test_an_overflow_reclaim_keeps_every_key_the_loop_put_on_a_message(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        history = [
            {**message, "_turn_log_seq": index, "timestamp": f"t{index}", "_turn_scratch": True}
            for index, message in enumerate(_session(60))
        ]
        messages = [{"role": "system", "content": "system prompt"}, *history]
        headroom.learn_from_overflow("stub", window=WINDOW, prompt_tokens=estimate_prompt_tokens(messages), step=8_192)

        outcome = await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW)

        assert outcome is not None
        for message in outcome.messages[1:]:
            seq = message["_turn_log_seq"]
            assert message == history[seq]


# ---------------------------------------------------------------------------
# Two processes one log generation apart
# ---------------------------------------------------------------------------


def _store_checkpoint(checkpoint_id: str, scope: str, *, generation: int = 1):
    from raven.context_engine.history.compacting.checkpoint import CompactionCheckpoint, empty_sections

    return CompactionCheckpoint(
        checkpoint_id=checkpoint_id,
        session_key="tui:k",
        created_at="2026-10-20T00:00:00",
        generation=generation,
        covers=SourceRef("session", scope, 1, 10),
        supersedes=None,
        sections=empty_sections(),
        model="m",
        byte_prefix_reused=False,
        retired=tuple(range(1, 11)),
    )


class TestGenerationsOfOneLog:
    """An undo another process merged and this one has not yet makes the two
    disagree about the revision only by its generation. The one behind must
    neither delete the other's checkpoint nor overwrite its state."""

    LOG = "ab" * 16

    def _names(self, store: CheckpointStore) -> list[str]:
        return sorted(path.name for path in store.folder("tui:k").glob("*.json"))

    def test_the_process_behind_cannot_ping_pong_the_one_ahead(self, tmp_path: Path) -> None:
        from raven.context_engine.history.compacting.store import CompactingState

        store = CheckpointStore(tmp_path)
        ahead = _store_checkpoint("aaaa", f"{self.LOG}@1")
        store.write(ahead)
        store.save(
            "tui:k",
            CompactingState(
                scope=f"{self.LOG}@1", message_count=40, generation=1, pruned=frozenset({3, 4}), checkpoint=ahead
            ),
        )
        before = self._names(store)

        behind = _store_checkpoint("bbbb", f"{self.LOG}@0")
        assert store.write(behind) is None
        assert not store.save(
            "tui:k", CompactingState(scope=f"{self.LOG}@0", message_count=38, generation=1, checkpoint=behind)
        )

        assert self._names(store) == before
        reloaded = store.load("tui:k", SessionRevision(log_id=self.LOG, generation=1), message_count=41)
        assert reloaded.checkpoint is not None and reloaded.checkpoint.checkpoint_id == "aaaa"
        assert reloaded.pruned == frozenset({3, 4})

    def test_the_process_behind_reads_its_state_as_lagging(self, tmp_path: Path) -> None:
        from raven.context_engine.history.compacting.store import CompactingState

        store = CheckpointStore(tmp_path)
        store.write(_store_checkpoint("aaaa", f"{self.LOG}@1"))
        store.save("tui:k", CompactingState(scope=f"{self.LOG}@1", message_count=40, generation=1))

        loaded = store.load("tui:k", SessionRevision(log_id=self.LOG, generation=0), message_count=45)
        assert loaded.lagging is True
        assert (loaded.checkpoint, loaded.pruned) == (None, frozenset())

    def test_a_later_checkpoint_file_alone_marks_the_reader_behind(self, tmp_path: Path) -> None:
        """A crash between writing a checkpoint and naming it leaves no state;
        the file itself is evidence enough."""
        store = CheckpointStore(tmp_path)
        store.write(_store_checkpoint("aaaa", f"{self.LOG}@2"))

        loaded = store.load("tui:k", SessionRevision(log_id=self.LOG, generation=1), message_count=45)
        assert loaded.lagging is True

    def test_a_newer_generation_is_never_deleted_by_an_older_one(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path)
        store.write(_store_checkpoint("aaaa", f"{self.LOG}@3"))
        for generation in (0, 1, 2):
            store.write(_store_checkpoint(f"old{generation}", f"{self.LOG}@{generation}"))

        assert [cp.checkpoint_id for cp in store.chain("tui:k")] == ["aaaa"]

    def test_the_writer_ahead_clears_earlier_generations_and_other_logs(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path)
        store.write(_store_checkpoint("gen0", f"{self.LOG}@0"))
        store.write(_store_checkpoint("other", f"{'cd' * 16}@5"))
        store.write(_store_checkpoint("gen1", f"{self.LOG}@1"))

        assert [cp.checkpoint_id for cp in store.chain("tui:k")] == ["gen1"]

    async def test_a_builder_behind_by_a_generation_trims_and_writes_nothing(self, tmp_path: Path) -> None:
        session = _session(80)
        ahead = _revision(session, generation=1)
        first = await _builder(tmp_path, config=_fold_only()).build(_ctx(session, revision=ahead))
        assert first.meta["compaction_action"] == "compact"
        folder = CheckpointStore(tmp_path / CHECKPOINT_DIR).folder("s1")
        files = {path.name: path.read_bytes() for path in folder.glob("*.json")}

        behind = await _builder(tmp_path, config=_fold_only()).build(
            _ctx([*session, *_session(4, start=900)], revision=_revision(session, generation=0))
        )

        assert behind.meta["compaction_action"] == "pass"
        assert {path.name: path.read_bytes() for path in folder.glob("*.json")} == files
        assert HistoryTrimmer.structural_errors(_request(behind.history)) == []
        again = await _builder(tmp_path, config=_fold_only()).build(_ctx(session, revision=ahead))
        assert json.dumps(again.history) == json.dumps(first.history)


# ---------------------------------------------------------------------------
# The overflow path holds the turn's own words
# ---------------------------------------------------------------------------


def _working_list(*, rounds: int = 24, inject_at: int | None = None, picture_at: int | None = None) -> list[dict]:
    """What the loop hands the overflow path mid-turn: the session's history,
    the turn's question, then its tool loop."""
    messages: list[dict] = [{"role": "system", "content": "system prompt"}, *_session(3)]
    messages.append({"role": "user", "content": "TURN-QUESTION what failed?"})
    for index in range(rounds):
        messages += _tool_exchange(f"c{index}", "exec", f"{index} {FILLER * 12}", '{"command": "make"}')
        if index == inject_at:
            messages.append({"role": "user", "content": "MID-TURN only the auth module", "_mid_turn_user": True})
        if index == picture_at:
            messages.append({"role": "user", "content": "[image]", "_attached_image": True})
    return messages


class TestTheTurnsOwnWords:
    def test_the_marks_are_spelled_as_the_loop_spells_them(self) -> None:
        from raven.agent.loop import _shared, turn_path
        from raven.agent.window.images import ATTACHED_IMAGE_KEY
        from raven.context_engine.history.compacting.runtime import _NOT_THE_QUESTION_KEYS

        assert _shared._MID_TURN_USER_KEY in _NOT_THE_QUESTION_KEYS
        assert _shared._HOOK_INJECTED_KEY in _NOT_THE_QUESTION_KEYS
        assert ATTACHED_IMAGE_KEY in _NOT_THE_QUESTION_KEYS
        assert set(turn_path._TURN_TRANSIENT_KEYS) <= set(_NOT_THE_QUESTION_KEYS)

    def test_the_question_is_the_last_plain_user_message(self) -> None:
        from raven.context_engine.history.compacting.runtime import _turn_held

        history = _working_list(rounds=4, inject_at=1, picture_at=2)[1:]
        held = _turn_held(history)
        contents = [history[index]["content"] for index in sorted(held)]
        assert contents == ["TURN-QUESTION what failed?", "MID-TURN only the auth module"]

    def test_a_picture_carrier_is_neither_the_question_nor_held(self) -> None:
        from raven.context_engine.history.compacting.runtime import _turn_held

        history = _working_list(rounds=4, picture_at=3)[1:]
        held = _turn_held(history)
        assert [history[index]["content"] for index in sorted(held)] == ["TURN-QUESTION what failed?"]

    def test_runtime_scaffolding_is_not_the_question_but_is_held_after_it(self) -> None:
        from raven.context_engine.history.compacting.runtime import _turn_held

        history = _working_list(rounds=2)[1:]
        history.append({"role": "user", "content": "nudge", "_recovery_synthetic": True})
        history.append({"role": "user", "content": "hook", "_hook_injected": True})
        held = _turn_held(history)
        assert [history[index]["content"] for index in sorted(held)] == ["TURN-QUESTION what failed?", "nudge", "hook"]

    def test_with_no_plain_user_message_nothing_is_held(self) -> None:
        from raven.context_engine.history.compacting.runtime import _turn_held

        assert _turn_held([{"role": "user", "content": "[image]", "_attached_image": True}]) == frozenset()

    async def test_a_reclaim_keeps_the_question_and_the_correction(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        messages = _working_list(inject_at=10, picture_at=14)
        headroom.learn_from_overflow("stub", window=WINDOW, prompt_tokens=estimate_prompt_tokens(messages), step=8_192)

        outcome = await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW)

        assert outcome is not None
        contents = [str(m.get("content")) for m in outcome.messages]
        assert "TURN-QUESTION what failed?" in contents
        assert "MID-TURN only the auth module" in contents
        assert len(outcome.messages) < len(messages), "tool exchanges between them still go"
        assert HistoryTrimmer.structural_errors(outcome.messages) == []
