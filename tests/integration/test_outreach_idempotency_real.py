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

The same hazard has a second shape: two runs that are alive at the same
time, both finding the same person with no send to them yet. The guard has
to refuse one of them there too, so the test runs several processes against
one database and lets them race.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
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


# ---------------------------------------------------------------------------
# Two live processes, one subject, one send
# ---------------------------------------------------------------------------

_RACING_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from raven.records import RecordStore
    from raven.outbound import UnresolvedSendError, prepare_send

    db_path, ready_path, go_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with RecordStore(db_path) as store:
        Path(ready_path).write_text("ready", encoding="utf-8")
        while not Path(go_path).exists():
            time.sleep(0.0002)
        try:
            prepared = prepare_send(
                store,
                subject="acme-co",
                subject_type="company",
                channel="email",
                body="Hello there",
            )
        except UnresolvedSendError:
            print("REFUSED", flush=True)
        else:
            print("PREPARED " + prepared.token, flush=True)
    """
)


def _wait_for(paths: list[Path], timeout: float = 120.0) -> None:
    """Wait until every one of the paths is there."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(path.exists() for path in paths):
            return
        time.sleep(0.005)
    raise AssertionError("the children did not all get ready in time")


def test_eight_processes_racing_on_one_subject_leave_one_send(tmp_path: Path) -> None:
    """One send, seven refusals: the double-send this package exists to prevent."""
    db_path = tmp_path / "records.db"
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    go_path = tmp_path / "go"

    with RecordStore(db_path):
        pass

    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _RACING_CHILD,
                str(db_path),
                str(ready_dir / f"ready-{index}"),
                str(go_path),
            ],
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(8)
    ]
    _wait_for([ready_dir / f"ready-{index}" for index in range(8)])
    go_path.write_text("go", encoding="utf-8")

    outcomes: list[str] = []
    for child in children:
        stdout, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, stderr
        outcomes.append(stdout.strip())

    prepared = [line for line in outcomes if line.startswith("PREPARED")]
    refused = [line for line in outcomes if line == "REFUSED"]
    assert len(prepared) == 1, outcomes
    assert len(refused) == 7, outcomes

    token = prepared[0].split(" ", 1)[1]
    with RecordStore(db_path) as store:
        assert len(unresolved(store, subject="acme-co")) == 1
        interaction = find_by_token(store, token)
        assert interaction is not None
        assert interaction.state == "prepared"
