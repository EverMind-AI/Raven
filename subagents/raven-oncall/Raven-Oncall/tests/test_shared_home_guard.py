"""The shared-home guard must report real drift and stay quiet otherwise.

Exercised against temp directories, never the developer's real ~/.raven -- a
guard that needed the real path to test itself would be the very hazard it
exists to catch.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests._shared_home_guard import describe_drift, failure_report, snapshot


def _home(tmp_path: Path, *, jobs: list[dict] | None = None, campaigns: list[str] = ()) -> Path:
    home = tmp_path / ".raven"
    if jobs is not None:
        (home / "cron").mkdir(parents=True, exist_ok=True)
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    for name in campaigns:
        (home / "ops" / name).mkdir(parents=True, exist_ok=True)
    return home


def test_absent_home_snapshots_cleanly(tmp_path: Path) -> None:
    before = snapshot(tmp_path / "nothing-here")
    assert describe_drift(before, snapshot(tmp_path / "nothing-here")) == []


def test_unchanged_state_reports_no_drift(tmp_path: Path) -> None:
    home = _home(tmp_path, jobs=[{"id": "a"}], campaigns=["armb-embed-r2"])
    before = snapshot(home)
    assert describe_drift(before, snapshot(home)) == []


def test_deleted_job_is_reported(tmp_path: Path) -> None:
    """The round-2 incident: a subprocess wrote back a store with the job gone."""
    home = _home(tmp_path, jobs=[{"id": "recheck"}])
    before = snapshot(home)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")

    problems = describe_drift(before, snapshot(home))
    assert len(problems) == 1
    assert "rewritten with different content" in problems[0]


def test_identical_rewrite_is_not_reported(tmp_path: Path) -> None:
    """A byte-identical rewrite changes mtime but not state, so it must stay quiet
    -- otherwise the guard cries wolf on every atomic save and gets disabled."""
    home = _home(tmp_path, jobs=[{"id": "a"}])
    before = snapshot(home)
    payload = (home / "cron" / "jobs.json").read_bytes()
    (home / "cron" / "jobs.json").write_bytes(payload)

    assert describe_drift(before, snapshot(home)) == []


def test_created_store_is_reported(tmp_path: Path) -> None:
    home = tmp_path / ".raven"
    before = snapshot(home)
    _home(tmp_path, jobs=[{"id": "a"}])

    problems = describe_drift(before, snapshot(home))
    assert any("was created" in p for p in problems)


def test_new_and_lost_campaign_dirs_are_reported(tmp_path: Path) -> None:
    home = _home(tmp_path, campaigns=["keep-me", "delete-me"])
    before = snapshot(home)
    (home / "ops" / "appeared").mkdir(parents=True)
    (home / "ops" / "delete-me").rmdir()

    problems = describe_drift(before, snapshot(home))
    assert any("gained campaign dir(s): appeared" in p for p in problems)
    assert any("lost campaign dir(s): delete-me" in p for p in problems)


def test_failure_report_names_the_likely_cause() -> None:
    text = failure_report(["something moved"])
    assert "SHARED STATE VIOLATION" in text
    assert "something moved" in text
    assert "spawned a real raven subprocess" in text
