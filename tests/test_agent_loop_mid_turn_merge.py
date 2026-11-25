"""``merge_mid_turn``: what the provider is handed for a mid-turn arrival.

The loop appends one user message per arrival and labels them at the call seam,
so this pass is the only thing standing between "the reader corrected me while I
worked" and "the reader asked a second question". These pin the rules the seam
depends on: which messages it touches, which it must leave alone, and that
running it twice over the same list changes nothing.
"""

from __future__ import annotations

from typing import Any

from raven.agent.loop import AgentLoop
from raven.agent.loop._shared import _MID_TURN_HEADER, _MID_TURN_USER_KEY, merge_mid_turn
from raven.agent.loop.bundles import ToolWiring
from raven.providers.base import LLMProvider, LLMResponse
from raven.providers.prompt_cache import cache_control
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.token_wise.cache_optimizer import CacheOptimizer


def _mid(text: str) -> dict:
    return {"role": "user", "content": text, _MID_TURN_USER_KEY: True}


def test_an_unmarked_list_is_handed_back_untouched() -> None:
    """The common path: every turn's call goes through here and almost none
    carries an arrival, so the same object comes back rather than a copy."""
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]

    assert merge_mid_turn(messages) is messages


def test_the_merged_message_keeps_the_mark() -> None:
    """A turn that is thrown away and run again seeds itself from the question
    plus whatever carries this key -- so dropping it here loses the correction
    on exactly the re-run that was meant to act on it."""
    [merged] = merge_mid_turn([_mid("only the last quarter")])

    assert merged[_MID_TURN_USER_KEY] is True
    assert merged["role"] == "user"
    assert merged["content"] == f"{_MID_TURN_HEADER}\n\nonly the last quarter"


def test_a_second_pass_adds_no_second_header() -> None:
    """The merged message keeps the mark, so it is a candidate again; the label
    is a statement about the message, and stating it twice states it wrong."""
    once = merge_mid_turn([_mid("skip 2023"), _mid("Q4 only")])

    assert merge_mid_turn(once) == once


def test_only_adjacent_arrivals_fold_together() -> None:
    """The work between two gaps is what the second correction is about."""
    merged = merge_mid_turn(
        [
            {"role": "user", "content": "summarise the report"},
            _mid("skip 2023"),
            {"role": "assistant", "content": "reading it"},
            _mid("Q4 only"),
        ]
    )

    assert [m["content"] for m in merged] == [
        "summarise the report",
        f"{_MID_TURN_HEADER}\n\nskip 2023",
        "reading it",
        f"{_MID_TURN_HEADER}\n\nQ4 only",
    ]


def test_the_live_path_does_not_label_a_stored_entry() -> None:
    """Only the private spelling means "this arrived during the turn now
    running". The live path leaves a saved entry's plain ``mid_turn`` alone; the
    replay path folds it, because that is the shape the turn was sent with."""
    messages = [{"role": "user", "content": "only the last quarter", "mid_turn": True}]

    assert merge_mid_turn(messages) is messages


def test_the_two_spellings_of_the_stored_mark_agree() -> None:
    """The loop owns the name; the history strategy spells it locally rather
    than loading the loop. Nothing but this test holds the two together."""
    from raven.agent.loop._shared import _STORED_MID_TURN_KEY
    from raven.context_engine.history_trimmer import STORED_MID_TURN_KEY

    assert STORED_MID_TURN_KEY == _STORED_MID_TURN_KEY


def test_a_replayed_run_is_folded_the_way_it_was_sent() -> None:
    """A saved entry carries the plain ``mid_turn``. On the replay path that is
    the same fact the private spelling states live, and the provider's cached
    prefix only matches when the run is folded back into one message."""
    stored = [
        {"role": "user", "content": "summarise the report"},
        {"role": "user", "content": "skip 2023", "mid_turn": True},
        {"role": "user", "content": "Q4 only", "mid_turn": True},
    ]

    replayed = merge_mid_turn(stored, replay=True)

    assert [m["content"] for m in replayed] == [
        "summarise the report",
        f"{_MID_TURN_HEADER}\n\nskip 2023\n\nQ4 only",
    ]


def test_the_live_path_still_ignores_a_stored_mark() -> None:
    """Without ``replay`` the plain spelling says nothing: the turn now running
    added nothing, and the list comes back untouched."""
    stored = [{"role": "user", "content": "Q4 only", "mid_turn": True}]

    assert merge_mid_turn(stored) is stored


def test_two_replayed_runs_stay_separate_when_work_sits_between() -> None:
    stored = [
        {"role": "user", "content": "go", "mid_turn": True},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": "change of plan", "mid_turn": True},
    ]

    replayed = merge_mid_turn(stored, replay=True)

    assert [m["content"] for m in replayed] == [
        f"{_MID_TURN_HEADER}\n\ngo",
        "working",
        f"{_MID_TURN_HEADER}\n\nchange of plan",
    ]


def _text(content: Any) -> str:
    """What a provider reads from one message's content, a str or a text-block list."""
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")


async def test_an_arrival_the_cache_strategy_wrapped_folds_as_its_text() -> None:
    """The fold runs after the strategies, and ``CacheOptimizer`` -- on by default
    -- rewrites the newest messages' content from a str into a text-block list to
    hang a breakpoint on it. Folding that list with ``str()`` put the Python repr
    of the block in front of the model in place of what the reader typed. The
    fold reads the text, keeps the breakpoint on the run's last arrival, and
    says the same bytes the next turn's replay of the stored arrivals says."""
    arrivals = [
        {"role": "system", "content": "you are raven"},
        {"role": "user", "content": "summarise the report"},
        _mid("skip 2023"),
        _mid("Q4 only"),
    ]
    marked, _tools, _model = await CacheOptimizer(supports_caching=lambda _model: True).before_llm_call(
        arrivals, None, "stub"
    )
    assert isinstance(marked[-1]["content"], list), "the strategy really wrapped the arrival"

    merged = merge_mid_turn(marked, replay=True)[-1]

    assert _text(merged["content"]) == f"{_MID_TURN_HEADER}\n\nskip 2023\n\nQ4 only"
    assert isinstance(merged["content"], list), "the breakpoint survives the fold"
    assert merged["content"][-1]["cache_control"] == cache_control()
    assert _text(merged["content"]) == merge_mid_turn(arrivals, replay=True)[-1]["content"]


class _Recorder(LLMProvider):
    """Answers every main-loop call at once and keeps what each was sent."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.main_calls: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        if tools is None:
            return LLMResponse(content="aside", finish_reason="stop")
        self.main_calls.append([dict(m) for m in messages])
        return LLMResponse(content="noted", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


def _turn(text: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=text,
    )


async def test_the_model_reads_a_cached_arrival_as_text_on_both_turns(tmp_path) -> None:
    """The real loop, with the real strategy: the arrival is the newest message
    when the call goes out, so the strategy wraps it before the fold reads it.
    The model has to read the header and the reader's words, and the next turn
    has to replay the same bytes from the stored arrival."""
    provider = _Recorder()
    agent = AgentLoop(provider=provider, workspace=tmp_path, model="stub", tools=ToolWiring(restrict_to_workspace=True))
    # Only the capability probe is pinned, because the test model is not a wire id.
    agent.strategies.register(CacheOptimizer(max_breakpoints=4, supports_caching=lambda _model: True))
    pending = [_turn("only the last quarter")]

    def drain() -> list[TurnRequest]:
        taken = list(pending)
        pending.clear()
        return taken

    await agent._process_message(_turn("summarise the report"), session_key="test:c1", drain=drain)
    await agent._process_message(_turn("and the totals"), session_key="test:c1")

    expected = f"{_MID_TURN_HEADER}\n\nonly the last quarter"
    live = provider.main_calls[0][-1]
    assert _text(live["content"]) == expected
    assert isinstance(live["content"], list), "the strategy's breakpoint rides the fold"
    replayed = [m for m in provider.main_calls[1] if _text(m.get("content") or "").startswith(_MID_TURN_HEADER)]
    assert [_text(m["content"]) for m in replayed] == [expected], "the next turn replays the bytes the call sent"
