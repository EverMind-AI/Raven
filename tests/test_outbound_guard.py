"""The outbound guard: a send that cannot happen twice by accident.

The cases that matter are the ones a retry creates. A send that was prepared
and never resolved must stop a second attempt, and an outcome nobody can
decide must be surfaced rather than guessed at -- guessed either way, since
"probably did not go out" is the guess that double-sends.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.outbound import (
    NOT_SENT,
    SENT,
    UNKNOWN,
    UnresolvedSendError,
    complete_send,
    find_by_token,
    guard_send,
    is_token,
    new_token,
    prepare_send,
    reconcile,
    token_from_text,
    unresolved,
)
from raven.records.lifecycle import INTERACTION_CHAIN
from raven.records.store import RecordStore
from raven.records.types import RecordType


@pytest.fixture
def store(tmp_path: Path) -> RecordStore:
    return RecordStore(tmp_path / "records.db")


def _prepare(store: RecordStore, subject: str = "acme-co", body: str = "Hello there"):
    return prepare_send(
        store,
        subject=subject,
        subject_type="company",
        channel="email",
        body=body,
    )


# ---------------------------------------------------------------------------
# The states this package writes are the interaction chain's own steps
# ---------------------------------------------------------------------------


def test_the_guard_writes_the_interaction_chains_own_steps():
    from raven.outbound import PREPARED

    assert PREPARED in INTERACTION_CHAIN
    assert SENT in INTERACTION_CHAIN


# ---------------------------------------------------------------------------
# A send is preceded by a persisted interaction
# ---------------------------------------------------------------------------


def test_preparing_writes_the_interaction_before_anything_else(store):
    prepared = _prepare(store)
    interaction = find_by_token(store, prepared.token)
    assert interaction is not None
    assert interaction.type is RecordType.INTERACTION
    assert interaction.state == "prepared"
    assert interaction.fields["subject"] == "acme-co"
    assert interaction.fields["channel"] == "email"
    assert interaction.fields["body"] == "Hello there"


def test_the_guard_writes_the_record_before_the_effect_can_happen(store):
    """The record exists while the block runs, not after it returns."""
    seen: list[str] = []

    with guard_send(
        store, subject="acme-co", subject_type="company", channel="email", body="Hi"
    ) as prepared:
        seen.append(str(len(unresolved(store))))
        interaction = find_by_token(store, prepared.token)
        assert interaction is not None

    assert seen == ["1"]
    assert unresolved(store) == []


def test_completing_moves_the_interaction_to_sent(store):
    prepared = _prepare(store)
    completed = complete_send(store, prepared, message_id="<abc@example>")
    assert completed.state == SENT
    assert completed.fields["message_id"] == "<abc@example>"
    assert completed.fields["sent_at"]
    assert unresolved(store) == []


def test_completing_twice_is_not_an_error(store):
    prepared = _prepare(store)
    complete_send(store, prepared)
    again = complete_send(store, prepared)
    assert again.state == SENT


# ---------------------------------------------------------------------------
# A second attempt is refused while the first is unresolved
# ---------------------------------------------------------------------------


def test_a_second_attempt_at_the_same_subject_is_refused(store):
    _prepare(store)
    with pytest.raises(UnresolvedSendError) as caught:
        _prepare(store)
    assert "acme-co" in str(caught.value)


def test_the_refusal_names_the_unresolved_send(store):
    first = _prepare(store)
    with pytest.raises(UnresolvedSendError) as caught:
        _prepare(store)
    assert first.token in str(caught.value)


def test_a_second_attempt_at_a_different_subject_is_allowed(store):
    _prepare(store, subject="acme-co")
    other = _prepare(store, subject="globex-co")
    assert other.token != ""


def test_a_second_attempt_is_allowed_once_the_first_is_resolved(store):
    first = _prepare(store)
    complete_send(store, first)
    second = _prepare(store)
    assert second.token != first.token


def test_a_send_whose_record_is_gone_frees_the_subject(store):
    """A claim on a send that is no longer there does not hold the subject."""
    first = _prepare(store)
    store.delete(first.record_id)

    second = _prepare(store)

    assert second.token != first.token


# ---------------------------------------------------------------------------
# The correlation token
# ---------------------------------------------------------------------------


def test_a_token_is_a_short_handle_and_is_recognisable(store):
    prepared = _prepare(store)
    assert is_token(prepared.token)
    assert not is_token("nope")
    assert not is_token("")
    assert is_token(new_token())


def test_two_sends_get_two_tokens(store):
    first = _prepare(store, subject="acme-co")
    complete_send(store, first)
    second = _prepare(store, subject="globex-co")
    assert first.token != second.token


def test_the_token_is_quoted_in_the_message(store):
    prepared = _prepare(store)
    message = prepared.message_with_token()
    assert prepared.token in message
    assert token_from_text(message) == prepared.token


def test_a_reply_quoting_the_token_resolves_to_the_send(store):
    prepared = _prepare(store)
    reply = f"Thanks for getting in touch.\n\n{prepared.token}"
    found = token_from_text(reply)
    assert found == prepared.token
    interaction = find_by_token(store, found)
    assert interaction is not None
    assert interaction.fields["subject"] == "acme-co"


def test_a_reply_without_a_token_resolves_to_nothing(store):
    assert token_from_text("Just saying hello, no reference here.") is None
    assert token_from_text("") is None


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def test_reconciling_with_nothing_that_can_tell_leaves_everything_ambiguous(store):
    _prepare(store)
    result = reconcile(store)
    assert result.unresolved == 1
    assert result.resolved == 0
    assert len(unresolved(store)) == 1


def test_a_probe_that_says_sent_moves_the_record(store):
    prepared = _prepare(store)
    result = reconcile(store, lambda interaction: SENT)
    assert len(result.sent) == 1
    assert result.unresolved == 0
    assert find_by_token(store, prepared.token).state == SENT


def test_a_probe_that_says_not_sent_removes_the_record(store):
    prepared = _prepare(store)
    result = reconcile(store, lambda interaction: NOT_SENT)
    assert len(result.not_sent) == 1
    assert result.unresolved == 0
    # A record of a message that never left does not belong in the store.
    assert find_by_token(store, prepared.token) is None


def test_a_probe_that_cannot_answer_leaves_the_record_prepared(store):
    _prepare(store)
    result = reconcile(store, lambda interaction: UNKNOWN)
    assert result.unresolved == 1
    assert len(unresolved(store)) == 1


def test_a_probe_that_raises_is_treated_as_cannot_answer(store):
    """A probe that breaks is not evidence that the message did not go out."""

    def broken(interaction):
        raise RuntimeError("the channel is down")

    _prepare(store)
    result = reconcile(store, broken)
    assert result.unresolved == 1
    assert len(unresolved(store)) == 1


def test_an_ambiguous_send_still_blocks_a_second_attempt(store):
    _prepare(store)
    reconcile(store)
    with pytest.raises(UnresolvedSendError):
        _prepare(store)


# ---------------------------------------------------------------------------
# The shape of the guard's own refusals
# ---------------------------------------------------------------------------


def test_a_send_needs_a_subject_and_a_channel(store):
    with pytest.raises(ValueError):
        prepare_send(store, subject="", subject_type="company", channel="email", body="x")
    with pytest.raises(ValueError):
        prepare_send(store, subject="acme-co", subject_type="company", channel="", body="x")


def test_completing_a_send_that_is_not_in_the_store_is_refused(store):
    prepared = _prepare(store)
    store.delete(prepared.record_id)
    with pytest.raises(UnresolvedSendError):
        complete_send(store, prepared)
