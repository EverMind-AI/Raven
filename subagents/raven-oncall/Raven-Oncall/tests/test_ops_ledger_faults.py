"""Fault injection on the ledger: what survives a crash, and what does not.

The ledger's whole purpose is to be readable after the process dies, so its
failure modes are worth testing directly rather than inferring from the write
path. Three faults are injected here, in order of how bad they are for a
resident on-call process:

  - killed mid-write -- the temp-file-plus-rename pattern should make this a
    non-event, and it does;
  - a leftover temp file from that kill -- must never be mistaken for the
    ledger;
  - a torn or empty ledger -- possible when the host loses power rather than
    the process being killed, because the rename is atomic but the contents are
    never fsynced. This is the one that stops the loop from starting at all.

A loop that refuses to start is worse than a loop with a stale ledger: nobody
is watching and nothing says so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.ops.backend import JobHandle, JobStatus
from raven.ops.ledger import Ledger, LedgerCorruptError


def _seeded(path: Path) -> Ledger:
    ledger = Ledger(path)
    ledger.record("job-a", campaign="c1")
    ledger.set_handle("job-a", JobHandle("scripted", "scripted-1"))
    ledger.set_status("job-a", JobStatus.SUCCEEDED)
    ledger.record("job-b", campaign="c1")
    return ledger


def test_a_kill_mid_write_leaves_the_previous_ledger_readable(tmp_path: Path):
    path = tmp_path / "ledger.json"
    _seeded(path)
    # The process died after the temp file was written but before the rename.
    (tmp_path / "ledger.json.tmp").write_text('{"version": 1, "records": {"job-c"', encoding="utf-8")

    reopened = Ledger(path)
    assert reopened.has("job-a")
    assert reopened.get("job-a").status is JobStatus.SUCCEEDED
    assert reopened.has("job-b")
    assert reopened.get("job-c") is None, "an unrenamed write must not be visible"


def test_a_leftover_temp_file_does_not_survive_the_next_write(tmp_path: Path):
    path = tmp_path / "ledger.json"
    ledger = _seeded(path)
    stale = tmp_path / "ledger.json.tmp"
    stale.write_text("garbage", encoding="utf-8")

    ledger.record("job-c", campaign="c1")

    assert not stale.exists(), "the rename consumes the temp file, so no garbage accumulates"


def test_a_terminal_job_is_never_resubmitted_after_reopening(tmp_path: Path):
    path = tmp_path / "ledger.json"
    _seeded(path)

    reopened = Ledger(path)
    pending = [r.idem_key for r in reopened.pending()]
    assert pending == ["job-b"], "the succeeded job must not come back as work to do"


def test_an_empty_ledger_file_refuses_to_open_rather_than_start_empty(tmp_path: Path):
    """Host power loss can leave a zero-length file: the rename is atomic but
    the contents were never fsynced, so the name is durable and the bytes are
    not."""
    path = tmp_path / "ledger.json"
    _seeded(path)
    path.write_text("", encoding="utf-8")

    with pytest.raises(LedgerCorruptError):
        Ledger(path)


def test_a_torn_ledger_file_refuses_to_open_rather_than_start_empty(tmp_path: Path):
    path = tmp_path / "ledger.json"
    _seeded(path)
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: len(text) // 2], encoding="utf-8")

    with pytest.raises(LedgerCorruptError):
        Ledger(path)


def test_a_record_missing_its_fields_refuses_to_open_rather_than_start_empty(tmp_path: Path):
    path = tmp_path / "ledger.json"
    _seeded(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["records"]["job-a"]["status"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LedgerCorruptError):
        Ledger(path)
