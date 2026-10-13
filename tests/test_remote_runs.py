"""raven.acp_client.remote_runs: what this computer keeps of a run on a machine, while it is in flight."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.acp_client import remote_runs


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def logs():
    """What raven logged while the test ran, as loguru's sink hears it."""
    from loguru import logger

    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="INFO")
    try:
        yield records
    finally:
        logger.remove(sink)


def _run(task: str = "echo@box-1a2b3c4d", **kw) -> remote_runs.RemoteRun:
    fields = dict(
        machine="box",
        task=task,
        agent="echo@box",
        session_key="tui:s1",
        handle="t1",
        task_id="t1",
        session_id="sess-1",
        prompt_id=3,
        next_id=4,
        initialize={"agentCapabilities": {}},
        launch_key="k1",
        config_key="c1",
        session_cwd="/root/raven-work/t1",
        stateful=False,
    )
    fields.update(kw)
    return remote_runs.RemoteRun(**fields)


def test_a_record_is_written_owner_only_and_read_back_whole(_home: Path) -> None:
    path = remote_runs.record(_run(received=7, started=1.5))

    assert path == _home / "machines" / "runs" / "echo@box-1a2b3c4d.json"
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    (back,) = remote_runs.pending()
    assert back == _run(received=7, started=1.5)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == remote_runs.VERSION


def test_pending_lists_the_runs_oldest_first_and_names_what_it_cannot_read(_home: Path, logs) -> None:
    remote_runs.record(_run("later", started=20.0))
    remote_runs.record(_run("earlier", started=10.0))
    (remote_runs.runs_dir() / "broken.json").write_text("{not json", encoding="utf-8")
    (remote_runs.runs_dir() / "other-version.json").write_text(json.dumps({"version": 99}), encoding="utf-8")
    (remote_runs.runs_dir() / "bare.json").write_text(json.dumps({"version": 1, "machine": "box"}), encoding="utf-8")

    assert [run.task for run in remote_runs.pending()] == ["earlier", "later"]
    assert "broken.json could not be read" in "\n".join(logs)
    assert "other-version.json is not one this version reads" in "\n".join(logs)


def test_forget_removes_the_record_and_is_quiet_about_one_already_gone() -> None:
    remote_runs.record(_run())

    remote_runs.forget("echo@box-1a2b3c4d")
    remote_runs.forget("echo@box-1a2b3c4d")

    assert remote_runs.pending() == []


def test_the_tracker_keeps_the_prompt_s_id_and_the_frames_read_current_and_removes_the_record_when_the_turn_ends() -> (
    None
):
    tracker = remote_runs.Tracker(_run(prompt_id=0, next_id=1))
    assert remote_runs.pending()[0].prompt_id == 0

    tracker.prompt_sent(5)
    tracker.received(3)
    tracker.received(2)  # a frame counted twice is not a step back

    (kept,) = remote_runs.pending()
    assert (kept.prompt_id, kept.next_id, kept.received) == (5, 6, 3)
    tracker.close(keep=False)
    assert remote_runs.pending() == [] and tracker.kept is False


def test_a_tracker_closed_to_keep_leaves_the_record_for_the_next_start() -> None:
    tracker = remote_runs.Tracker(_run())

    tracker.close(keep=True)

    assert [run.task for run in remote_runs.pending()] == ["echo@box-1a2b3c4d"] and tracker.kept is True


def test_a_record_that_cannot_be_updated_is_named_not_raised(logs, monkeypatch) -> None:
    tracker = remote_runs.Tracker(_run())

    def refuse(run):
        raise OSError("disk full")

    monkeypatch.setattr(remote_runs, "record", refuse)
    tracker.received(9)
    tracker.prompt_sent(9)

    assert "\n".join(logs).count("could not be updated: disk full") == 2


def test_a_dispatch_left_on_its_machine_is_read_as_left_once() -> None:
    remote_runs.mark_left("t9")

    assert remote_runs.was_left("t9") is True
    assert remote_runs.was_left("t9") is False
    assert remote_runs.was_left("never") is False


def test_the_directory_is_made_under_the_raven_home(_home: Path) -> None:
    assert remote_runs.runs_dir() == _home / "machines" / "runs"
    assert os.path.isdir(_home / "machines" / "runs")
