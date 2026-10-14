"""raven.acp_client.remote_runs: what this computer keeps of a run on a machine, while it is in flight."""

from __future__ import annotations

import json
import os
import threading
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


def test_a_record_is_read_back_whole(_home: Path) -> None:
    path = remote_runs.record(_run(received=7, started=1.5))

    assert path == _home / "machines" / "runs" / "echo@box-1a2b3c4d.json"
    (back,) = remote_runs.pending()
    assert back == _run(received=7, started=1.5)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == remote_runs.VERSION


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not available on Windows")
def test_a_record_and_its_directory_are_owner_only(_home: Path) -> None:
    path = remote_runs.record(_run())

    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700


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


def test_the_tracker_keeps_the_prompt_s_id_and_the_frames_read_before_it_and_removes_the_record_when_the_turn_ends() -> (
    None
):
    tracker = remote_runs.Tracker(_run(prompt_id=0, next_id=1))
    assert remote_runs.pending()[0].prompt_id == 0

    tracker.received(3)
    tracker.received(2)  # a frame counted twice is not a step back
    tracker.prompt_sent(5)
    tracker.received(9)  # the turn's own frames are the pick-up's to read again: the record stays at the prompt

    (kept,) = remote_runs.pending()
    assert (kept.prompt_id, kept.next_id, kept.received) == (5, 6, 3)
    tracker.close(keep=False)
    assert remote_runs.pending() == [] and tracker.kept is False


def test_a_dispatch_s_record_is_forgotten_by_the_manager_s_id_with_its_claim() -> None:
    """The manager's end of a run that returned: the record the backend left with the reply goes once the announce was taken."""
    remote_runs.record(_run(task_id="d1"))
    remote_runs.record(_run("other", task_id="d2"))
    assert remote_runs.claim("echo@box-1a2b3c4d")

    assert remote_runs.forget_dispatch("d1") is True
    assert remote_runs.forget_dispatch("d1") is False, "nothing is left of it"

    assert [run.task for run in remote_runs.pending()] == ["other"]
    assert not remote_runs.claim_path("echo@box-1a2b3c4d").exists()


def test_a_tracker_closed_to_keep_leaves_the_record_for_the_next_start() -> None:
    tracker = remote_runs.Tracker(_run())

    tracker.close(keep=True)

    assert [run.task for run in remote_runs.pending()] == ["echo@box-1a2b3c4d"] and tracker.kept is True


def test_a_record_that_cannot_be_updated_is_named_not_raised(logs, monkeypatch) -> None:
    tracker = remote_runs.Tracker(_run(prompt_id=0))

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


def test_a_claim_is_taken_once_per_live_process_and_goes_with_the_record() -> None:
    """Two processes of one home starting together: the one that claims a run picks it up; the other leaves it."""
    remote_runs.record(_run())

    assert remote_runs.claim("echo@box-1a2b3c4d") is True
    assert remote_runs.claim("echo@box-1a2b3c4d") is False, "held by a process that is alive: this one"
    assert remote_runs.claim_path("echo@box-1a2b3c4d").read_text(encoding="utf-8") == str(os.getpid())
    remote_runs.release("echo@box-1a2b3c4d")
    assert remote_runs.claim("echo@box-1a2b3c4d") is True, "released for a later start, it can be taken again"
    remote_runs.forget("echo@box-1a2b3c4d")
    assert not remote_runs.claim_path("echo@box-1a2b3c4d").exists() and remote_runs.pending() == []
    remote_runs.release("never-claimed")


def test_a_claim_left_by_a_process_that_is_gone_is_taken_over(monkeypatch) -> None:
    remote_runs.claim_path("gone").write_text("4000000", encoding="utf-8")
    remote_runs.claim_path("held").write_text("12345", encoding="utf-8")
    remote_runs.claim_path("blank").write_text("", encoding="utf-8")
    monkeypatch.setattr(remote_runs, "pid_alive", lambda pid: pid == 12345)

    assert remote_runs.claim("gone") is True and remote_runs.claim_path("gone").read_text() == str(os.getpid())
    assert remote_runs.claim("held") is False and remote_runs.claim_path("held").read_text() == "12345"
    assert remote_runs.claim("blank") is True, "a claim that names nobody holds nothing"


def test_two_starters_taking_over_one_dead_holder_leave_one_claim(monkeypatch) -> None:
    """A judged the claim dead and took it; B, who judged it dead a moment earlier, must not remove A's.

    Judging and taking are two steps: B's liveness check is held until A has
    claimed, which is the interleaving in which B used to unlink A's fresh
    claim and win its own retry, two processes picking one run up.
    """
    stale = 4000000
    remote_runs.claim_path("t").write_text(str(stale), encoding="utf-8")
    a_inside = threading.Event()
    a_done = threading.Event()
    real_take, real_alive = remote_runs._take, remote_runs.pid_alive

    def take(path):
        if threading.current_thread().name == "A":
            a_inside.set()
        return real_take(path)

    def alive(pid):
        if pid == stale:
            if threading.current_thread().name == "B":
                a_done.wait(5)
            return False
        return real_alive(pid)

    monkeypatch.setattr(remote_runs, "_take", take)
    monkeypatch.setattr(remote_runs, "pid_alive", alive)
    results: dict[str, bool] = {}

    def run(name: str) -> None:
        results[name] = remote_runs.claim("t")
        if name == "A":
            a_done.set()

    a = threading.Thread(target=run, args=("A",), name="A")
    b = threading.Thread(target=run, args=("B",), name="B")
    a.start()
    assert a_inside.wait(5)
    b.start()
    a.join(10)
    b.join(10)

    assert sorted(results.values()) == [False, True], results
    assert remote_runs.claim_path("t").read_text(encoding="utf-8") == str(os.getpid())


def test_a_claim_is_made_whole_without_hard_links_too_and_one_that_cannot_be_made_is_named(
    tmp_path, monkeypatch, logs
) -> None:
    def no_links(src, dst, *a, **k):
        raise OSError("hard links are not supported here")

    monkeypatch.setattr(os, "link", no_links)
    assert remote_runs.claim("plain") is True
    assert remote_runs.claim_path("plain").read_text(encoding="utf-8") == str(os.getpid())
    assert remote_runs.claim("plain") is False

    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    monkeypatch.setattr(remote_runs, "runs_dir", lambda: tmp_path / "a-file" / "runs")
    assert remote_runs.claim("nowhere") is False
    assert "a claim could not be made" in "\n".join(logs)


def test_a_record_carries_what_the_announce_after_a_restart_needs_and_an_older_one_reads_with_defaults(
    _home: Path,
) -> None:
    """``kind``, the task as authored, the summary and the dispatch's origin ride the record; one without them reads with defaults."""
    origin = {"node_id": "n1", "instance": None, "handle": "t1"}
    remote_runs.record(
        _run(
            "echo@box-cli1",
            kind="cli",
            session_id="",
            authored_task="count the rows",
            task_summary="count",
            origin=origin,
        )
    )
    older = {k: v for k, v in _run().as_json().items() if k not in ("kind", "authored_task", "task_summary", "origin")}
    (remote_runs.runs_dir() / "echo@box-1a2b3c4d.json").write_text(json.dumps(older), encoding="utf-8")

    back = {run.task: run for run in remote_runs.pending()}

    cli = back["echo@box-cli1"]
    assert (cli.kind, cli.authored_task, cli.task_summary, cli.origin) == ("cli", "count the rows", "count", origin)
    acp = back["echo@box-1a2b3c4d"]
    assert (acp.kind, acp.authored_task, acp.task_summary, acp.origin) == ("acp", "", "", None)


def test_a_cli_record_needs_no_acp_session_and_an_acp_one_still_does() -> None:
    assert remote_runs.RemoteRun.from_json(_run(kind="cli", session_id="").as_json()) is not None
    assert remote_runs.RemoteRun.from_json(_run(session_id="").as_json()) is None


def test_annotate_adds_the_manager_s_fields_to_the_record_of_a_dispatch_and_says_when_there_is_none(
    _home: Path,
) -> None:
    remote_runs.record(_run(task_id="d1"))

    assert remote_runs.annotate("d1", origin={"node_id": "n1", "workspace": Path("/w")}, task_summary="say hi") is True

    (back,) = remote_runs.pending()
    assert back.origin == {"node_id": "n1", "workspace": "/w"} and back.task_summary == "say hi"
    assert remote_runs.annotate("nobody", task_summary="x") is False
