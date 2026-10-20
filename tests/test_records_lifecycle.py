"""Which moves a record may make, and what happens to one that may not.

The chain of states is what two runs agree on. A run that resumes has to
know whether the message it is about to send has already gone out, and the
only way to know is if the states are declared rather than improvised. So the
declared moves are pinned here, including the ones that are refused, because
a refused move is information and not a failure to work around.
"""

from __future__ import annotations

import pytest

from raven.records import (
    LifecycleError,
    Record,
    RecordType,
    allowed_transitions,
    can_transition,
    check_transition,
    is_terminal,
    transition,
)


def _opportunity() -> Record:
    return Record(type=RecordType.OPPORTUNITY, dedupe_key="acme", fields={"name": "Acme"})


def test_the_prospect_chain_is_walked_in_order() -> None:
    record = _opportunity()
    for nxt in ("discovered", "qualified", "researched", "artifact_built", "pitched"):
        record = transition(record, nxt)

    assert record.state == "pitched"


def test_a_state_may_move_to_the_next_step_and_to_no_other() -> None:
    assert can_transition(RecordType.OPPORTUNITY, "discovered", "qualified") is True
    assert can_transition(RecordType.OPPORTUNITY, "discovered", "researched") is False
    assert can_transition(RecordType.OPPORTUNITY, "discovered", "pitched") is False


def test_an_outcome_may_be_reached_from_anywhere_in_the_chain() -> None:
    assert can_transition(RecordType.OPPORTUNITY, "discovered", "won") is True
    assert can_transition(RecordType.OPPORTUNITY, "discovered", "lost") is True
    assert can_transition(RecordType.OPPORTUNITY, "discovered", "engaged") is False


def test_a_state_may_not_move_backwards() -> None:
    assert can_transition(RecordType.OPPORTUNITY, "researched", "new") is False
    assert can_transition(RecordType.OPPORTUNITY, "researched", "discovered") is False


def test_a_move_that_is_not_declared_is_refused() -> None:
    with pytest.raises(LifecycleError) as raised:
        check_transition(RecordType.OPPORTUNITY, "discovered", "pitched")

    assert "may not move" in str(raised.value)
    assert "qualified" in str(raised.value)


def test_a_refused_move_leaves_the_record_where_it_was() -> None:
    record = _opportunity()
    with pytest.raises(LifecycleError):
        transition(record, "pitched")

    assert record.state == "new"


def test_a_move_returns_a_new_record_and_leaves_the_old_one_alone() -> None:
    record = _opportunity()
    moved = transition(record, "discovered")

    assert moved is not record
    assert record.state == "new"
    assert moved.state == "discovered"
    assert moved.created_at == record.created_at


def test_the_last_states_are_finished() -> None:
    assert is_terminal("won") is True
    assert is_terminal("lost") is True
    assert is_terminal("delivered") is True
    assert is_terminal("discovered") is False
    assert can_transition(RecordType.OPPORTUNITY, "won", "pitched") is False


def test_an_unknown_state_is_refused_rather_than_assumed() -> None:
    with pytest.raises(LifecycleError):
        allowed_transitions(RecordType.OPPORTUNITY, "somewhere")
    assert can_transition(RecordType.OPPORTUNITY, "somewhere", "new") is False


def test_the_other_kinds_walk_their_own_shorter_chains() -> None:
    assert can_transition(RecordType.ARTIFACT, "built", "qa_passed") is True
    assert can_transition(RecordType.ARTIFACT, "built", "delivered") is False
    assert can_transition(RecordType.ARTIFACT, "qa_passed", "delivered") is True
    assert can_transition(RecordType.INTERACTION, "sent", "bounced") is True
    assert can_transition(RecordType.INTERACTION, "sent", "prepared") is False
    assert can_transition(RecordType.COMPANY, "discovered", "qualified") is True
    assert can_transition(RecordType.COMPANY, "discovered", "pitched") is False


def test_the_qa_gate_cannot_be_stepped_over() -> None:
    """The reason the chain is ordered: an artefact is not delivered before it passed."""
    with pytest.raises(LifecycleError):
        check_transition(RecordType.ARTIFACT, "built", "delivered")
