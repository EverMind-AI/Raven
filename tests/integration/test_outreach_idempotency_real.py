"""A send that was interrupted, in a process that really died.

The hazard this guards against is not a bug in a function: it is a run that
writes to somebody, dies before it records that it did, and is retried. The
gap between "the interaction was written" and "the send completed" is where
the double-send happens, so the test has to cross it for real.

So it does. A child interpreter prepares a send, prints the token, and is
killed outright -- no cleanup, no exception handler, no chance to tidy up.
The parent then opens the same store and has to cope with what is left: a
prepared interaction, an outcome nobody knows, and a second attempt that
must be refused.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from raven.outbound import (
    NOT_SENT,
    SENT,
    UnresolvedSendError,
    find_by_token,
    prepare_send,
    reconcile,
    unresolved,
)
from raven.records import RecordStore, RecordType
from raven.records.queries import already_contacted

pytestmark = pytest.mark.integration


_CRASHING_CHILD = textwrap.dedent(
    """
    import os, sys
    from raven.records import RecordStore
    from raven.outbound import prepare_send

    db_path, mode = sys.argv[1], sys.argv[2]
    with RecordStore(db_path) as store:
        prepared = prepare_send(
            store,
            subject="acme-co",
            subject_type="company",
            channel="email",
            body="Hello there",
        )
        print(prepared.token, flush=True)
        if mode == "crash":
            # Die outright: no exception, no finally, no record of the outcome.
            os._exit(9)
        from raven.outbound import complete_send
        complete_send(store, prepared, message_id="<sent@example>")
    """
)


def _run_child(db_path: Path, mode: str) -> str:
    """Run the child; return the token it printed."""
    result = subprocess.run(
        [sys.executable, "-c", _CRASHING_CHILD, str(db_path), mode],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == (9 if mode == "crash" else 0), result.stderr
    return result.stdout.strip()


def test_a_process_that_died_mid_send_leaves_a_prepared_interaction(tmp_path: Path) -> None:
    db_path = tmp_path / "records.db"
    token = _run_child(db_path, "crash")

    with RecordStore(db_path) as store:
        interaction = find_by_token(store, token)
        assert interaction is not None, "the interaction was written before the crash"
        assert interaction.state == "prepared"
        assert interaction.fields["subject"] == "acme-co"
        assert interaction.fields["correlation_token"] == token


def test_a_second_attempt_after_a_crash_is_refused(tmp_path: Path) -> None:
    """The whole point: a retry must not write to the same person twice."""
    db_path = tmp_path / "records.db"
    _run_child(db_path, "crash")

    with RecordStore(db_path) as store:
        assert len(unresolved(store)) == 1
        with pytest.raises(UnresolvedSendError):
            prepare_send(
                store,
                subject="acme-co",
                subject_type="company",
                channel="email",
                body="Hello again",
            )


def test_a_crash_that_is_resolved_as_not_sent_frees_the_subject(tmp_path: Path) -> None:
    """Deciding "it did not go out" removes the record and lets a retry through."""
    db_path = tmp_path / "records.db"
    token = _run_child(db_path, "crash")

    with RecordStore(db_path) as store:
        result = reconcile(store, lambda interaction: NOT_SENT)
        assert len(result.not_sent) == 1
        assert result.unresolved == 0
        assert find_by_token(store, token) is None

        # The subject is free again, and a retry now goes out.
        prepared = prepare_send(
            store,
            subject="acme-co",
            subject_type="company",
            channel="email",
            body="Hello again",
        )
        assert prepared.token != token


def test_a_crash_that_is_resolved_as_sent_keeps_the_subject_claimed(tmp_path: Path) -> None:
    """Deciding "it did go out" is the claim that must survive the crash."""
    db_path = tmp_path / "records.db"
    token = _run_child(db_path, "crash")

    with RecordStore(db_path) as store:
        result = reconcile(store, lambda interaction: SENT)
        assert len(result.sent) == 1
        assert result.unresolved == 0

        interaction = find_by_token(store, token)
        assert interaction is not None
        assert interaction.state == SENT
        # And the store now says, in its own words, that we wrote to them.
        assert already_contacted(store, "acme-co", RecordType.COMPANY)


def test_a_crash_nobody_can_resolve_stays_ambiguous_and_blocks_the_retry(tmp_path: Path) -> None:
    """The safe default: nobody can tell, so nothing is guessed."""
    db_path = tmp_path / "records.db"
    _run_child(db_path, "crash")

    with RecordStore(db_path) as store:
        result = reconcile(store)  # no probe: nothing can be told
        assert result.unresolved == 1
        assert result.resolved == 0
        assert len(unresolved(store)) == 1

        with pytest.raises(UnresolvedSendError):
            prepare_send(
                store,
                subject="acme-co",
                subject_type="company",
                channel="email",
                body="Hello again",
            )


def test_a_send_that_completed_is_not_left_behind_as_unresolved(tmp_path: Path) -> None:
    """The ordinary case: a send that finished leaves nothing to decide."""
    db_path = tmp_path / "records.db"
    token = _run_child(db_path, "complete")

    with RecordStore(db_path) as store:
        assert unresolved(store) == []
        interaction = find_by_token(store, token)
        assert interaction is not None
        assert interaction.state == SENT
        assert interaction.fields["message_id"] == "<sent@example>"
