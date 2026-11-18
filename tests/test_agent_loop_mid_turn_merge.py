"""``merge_mid_turn``: what the provider is handed for a mid-turn arrival.

The loop appends one user message per arrival and labels them at the call seam,
so this pass is the only thing standing between "the reader corrected me while I
worked" and "the reader asked a second question". These pin the rules the seam
depends on: which messages it touches, which it must leave alone, and that
running it twice over the same list changes nothing.
"""

from __future__ import annotations

from raven.agent.loop._shared import _MID_TURN_HEADER, _MID_TURN_USER_KEY, merge_mid_turn


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
