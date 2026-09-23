"""The exec tool's background lane: harness custody for long local commands.

What separates this from ``nohup`` in the command string is everything the
tests below pin: the task has a handle, the log has a managed path the result
names, the child is tracked and reapable, and the lane changes none of the
rules -- the permission gate (which rules on the call before either lane is
chosen) and the sandbox boundary still have the last word.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from raven.agent.tools import background_exec
from raven.agent.tools.shell import ExecTool


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---- the module: custody itself ----


def _task_id_from(out: str) -> str:
    """The handle out of whichever note the launch produced.

    Both notes name it the same way, but the ended one puts the command after
    it, so slicing on a trailing space would take part of the command too.
    """
    return out.split(" as ", 1)[1].split(" ", 1)[0].rstrip(",.")


def test_a_started_task_runs_detached_and_its_log_is_managed(home):
    task = background_exec.start("echo hello-from-behind")

    assert task.task_id.startswith("bg-")
    assert Path(task.log_path).parent == home / "background"
    assert _wait_for(lambda: "hello-from-behind" in background_exec.tail(task.task_id))
    record = json.loads((home / "background" / f"{task.task_id}.json").read_text())
    assert record["pid"] == task.pid and record["command"] == "echo hello-from-behind"


def test_status_reports_liveness_and_reap_ends_the_group(home):
    task = background_exec.start("sleep 30")

    live = background_exec.status(task.task_id)
    assert live is not None and live["running"] is True
    assert any(t["task_id"] == task.task_id for t in background_exec.running())

    assert background_exec.reap(task.task_id) is True
    assert _wait_for(lambda: not background_exec.status(task.task_id)["running"])
    assert background_exec.reap(task.task_id) is False, "a dead task is not signalled again"


def test_an_unknown_task_is_none_not_a_traceback(home):
    assert background_exec.status("bg-nope") is None
    assert background_exec.tail("bg-nope") == ""


def test_the_start_note_hands_over_the_handle_and_the_log(home):
    task = background_exec.start("sleep 30")
    note = background_exec.start_note(task)

    assert task.task_id in note
    assert task.log_path in note
    assert "later one" in note, "the model is told how completion becomes visible"
    assert "reminder" in note, "the road back without holding the turn is named"

    background_exec.reap(task.task_id)


def test_a_command_that_exits_at_once_is_reported_as_ended_not_started(home):
    """The bug this file grew the check for: `start` returns when `Popen` does,
    so a command that never ran -- a port taken, a binary missing -- was handed
    back as running, and the model went looking for its own bug in a server that
    had never bound (measured 2026-09-22, session 692e5c)."""
    task = background_exec.start("echo one-line-then-out; exit 3")
    note = background_exec.start_note(task)

    assert "ended immediately" in note
    assert "exited with code 3" in note
    assert "one-line-then-out" in note, "the log's tail is the diagnosis and comes with it"
    assert "not running now" in note
    assert "This turn is not held open" not in note, "nothing is running to read a log of"


def test_a_command_killed_by_a_signal_is_named_by_that_signal(home):
    task = background_exec.start("kill -TERM $$")
    note = background_exec.start_note(task)

    assert "killed by SIGTERM" in note
    assert "ended immediately" in note


def test_a_command_that_wrote_nothing_says_so_rather_than_showing_an_empty_log(home):
    task = background_exec.start("exit 1")
    note = background_exec.start_note(task)

    assert "It wrote nothing to its log." in note
    assert "exited with code 1" in note


def test_a_binary_that_is_not_there_ends_the_launch_rather_than_hanging_around(home):
    task = background_exec.start("raven-test-no-such-binary --serve")
    note = background_exec.start_note(task)

    assert "ended immediately" in note
    assert "not found" in note, "the shell's own words are the whole explanation"


def test_a_command_that_keeps_running_is_not_claimed_to_have_ended(home):
    """The check may not become a guess in the other direction: a task still up
    after the window is reported as running, which is all the window shows."""
    task = background_exec.start("sleep 30")
    note = background_exec.start_note(task)

    assert "Started in the background" in note
    assert "ended immediately" not in note
    assert background_exec.status(task.task_id)["running"] is True

    background_exec.reap(task.task_id)


def test_an_ended_launch_leaves_nothing_to_reap(home):
    task = background_exec.start("exit 7")
    note = background_exec.start_note(task)

    assert "nothing will be reaped later" in note
    assert background_exec.status(task.task_id)["running"] is False
    assert background_exec.reap(task.task_id) is False


# ---- the tool wiring: the lane changes custody, never the rules ----


@pytest.mark.asyncio
async def test_exec_background_returns_the_note_without_holding_the_turn(home, tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    t0 = time.monotonic()

    out = await tool.execute(command="sleep 10", run_in_background=True)

    assert time.monotonic() - t0 < 5, "the turn is not held for the command"
    assert "Started in the background as bg-" in out
    task_id = _task_id_from(out)
    assert background_exec.reap(task_id) is True


@pytest.mark.asyncio
async def test_the_launch_check_waits_off_the_event_loop(home, tmp_path):
    """The note waits up to a second for the command to contradict it. A launch
    that keeps running spends that whole second, and on the loop's own thread
    every other turn in the process would stall for it."""
    tool = ExecTool(working_dir=str(tmp_path))
    beats = [time.monotonic()]

    async def ticker():
        while True:
            await asyncio.sleep(0.05)
            beats.append(time.monotonic())

    beat = asyncio.create_task(ticker())
    try:
        out = await tool.execute(command="sleep 30", run_in_background=True)
        await asyncio.sleep(0.1)
    finally:
        beat.cancel()

    gaps = [later - earlier for earlier, later in zip(beats, beats[1:])]
    assert "Started in the background as bg-" in out
    assert max(gaps) < 0.5, f"the loop stalled for {max(gaps):.2f}s while the launch was confirmed"
    assert background_exec.reap(_task_id_from(out)) is True


@pytest.mark.asyncio
async def test_machine_and_background_never_combine(home, tmp_path, monkeypatch):
    monkeypatch.setattr("raven.ops.connections.load", lambda: [{"id": "conn_gpu"}])
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="rsync a b:", machine="conn_gpu", run_in_background=True)

    assert "do not combine" in out
    assert "ops_submit" in out, "background work on a registered machine is a job"
    assert "Nothing was run" in out


@pytest.mark.asyncio
async def test_a_sandboxed_session_refuses_the_lane(home, tmp_path):
    class _Sandboxed:
        is_sandboxed = True

        async def exec(self, *a, **kw):
            raise AssertionError("the refusal must come before any execution")

    tool = ExecTool(working_dir=str(tmp_path), executor=_Sandboxed())

    out = await tool.execute(command="echo hi", run_in_background=True)

    assert "not available in a sandboxed session" in out
    assert "outside the sandbox" in out


@pytest.mark.asyncio
async def test_the_deny_list_still_has_the_last_word(home, tmp_path):
    """The refusal comes from the permission gate at the registry door, which is
    upstream of both lanes, so the background one is not a way around it."""
    from raven.agent.tools.registry import ToolRegistry
    from raven.config.schema import PermissionsConfig
    from raven.permissions import BuiltinRulings, PermissionGate

    registry = ToolRegistry(
        permission_gate=PermissionGate(
            config_source=lambda: PermissionsConfig(mode="full"),
            builtin=BuiltinRulings(),
        )
    )
    registry.register(ExecTool(working_dir=str(tmp_path)))

    out = await registry.execute("exec", {"command": "mkfs /dev/sda", "run_in_background": True})

    assert "safety guard" in str(out)
    assert not background_exec.running(), "a blocked command must not start in the background either"


@pytest.mark.asyncio
async def test_the_background_child_gets_the_baseline_env_not_the_host_env(home, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "must-not-leak")
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="env", run_in_background=True)

    assert "PATH=" in out, "the child got an allowlisted environment and its output came back"
    assert "must-not-leak" not in out, "the detached child keeps the executor's allowlist hygiene"
    assert not background_exec.running(), "the command is over; nothing is left to reap"


def test_session_exit_reaps_only_its_own_tasks(home, monkeypatch):
    task = background_exec.start("sleep 30")
    monkeypatch.setattr(background_exec, "_OWN", [])

    background_exec._reap_own()

    assert background_exec.status(task.task_id)["running"] is True, (
        "a survivor from another session is the operator's to keep or broom (reap_all)"
    )
    background_exec.reap(task.task_id)


def test_a_recorded_pid_this_process_did_not_start_is_believed_only_while_it_leads_its_own_group(home):
    """Reviewer 2026-09-07: a record naming a pid this process never started
    was reported running, listed, and reaped -- and nothing ever retired it, so
    once the kernel reused the number that was an unrelated process group.
    A task launched by ``start`` leads its own session; a recycled pid does
    not, so the probe asks for that, and a record found dead is retired."""
    import subprocess

    bystander = subprocess.Popen(["sleep", "30"])  # in OUR group: what a recycled pid looks like
    try:
        record = {
            "task_id": "bg-stale-0",
            "command": "sleep 30",
            "pid": bystander.pid,
            "log_path": str(home / "background" / "bg-stale-0.log"),
            "started_at": 0.0,
        }
        (home / "background").mkdir(parents=True, exist_ok=True)
        (home / "background" / "bg-stale-0.json").write_text(json.dumps(record))

        assert background_exec.status("bg-stale-0")["running"] is False
        assert background_exec.reap("bg-stale-0") is False
        assert bystander.poll() is None, "a bare pid from a stale record is not signalled"
        assert all(t["task_id"] != "bg-stale-0" for t in background_exec.running())
        assert not (home / "background" / "bg-stale-0.json").exists(), "a record found dead is retired"
    finally:
        bystander.kill()
        bystander.wait()


def test_a_task_from_another_session_is_still_seen_and_reaped_through_its_group(home, monkeypatch):
    """The other half: a real survivor -- leading its own session, as start()
    launches it -- is still reported, listed and stopped when this process
    holds no handle for it."""
    task = background_exec.start("sleep 30")
    handle = background_exec._PROCS[task.task_id]  # kept so the reaped child can be waited on below
    monkeypatch.setattr(background_exec, "_PROCS", {})
    monkeypatch.setattr(background_exec, "_OWN", [])

    assert background_exec.status(task.task_id)["running"] is True
    assert any(t["task_id"] == task.task_id for t in background_exec.running())
    assert background_exec.reap(task.task_id) is True
    # This process is still its parent, so until it is waited on the child is
    # a zombie, and a zombie still answers getpgid on Linux. Another session's
    # real survivor has another parent (or init) to do this.
    assert handle.wait(timeout=5) < 0 or handle.returncode != 0
    assert not background_exec._leads_its_own_group(task.pid)


def test_the_schema_advertises_the_lane(home, tmp_path):
    prop = ExecTool(working_dir=str(tmp_path)).parameters["properties"]["run_in_background"]
    assert prop["type"] == "boolean"
    assert "ops_submit" in prop["description"], "the boundary is taught where the parameter is read"
    assert "timeout" in prop["description"], "that the lane reads no timeout is said where the model can see it"
