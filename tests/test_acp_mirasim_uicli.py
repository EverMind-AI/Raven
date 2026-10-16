"""One ``mirasim ui-cli`` command: reading its answer, building its argv, and ending its tree."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from raven.acp.mirasim import uicli
from raven.acp.mirasim.uicli import (
    TURN_TIMEOUT_MS,
    Invocation,
    Outcome,
    UiCli,
    UiCliError,
    child_env,
    end_tree,
    last_document,
    prompt_args,
    route_args,
    session_gone,
)
from tests.mirasim_stub_cli import TOKEN_LINE
from tests.mirasim_stub_cli import install as stub_executable

posix_only = pytest.mark.skipif(os.name == "nt", reason="process groups and /bin/sh wrappers are POSIX")


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    return stat.exists() and stat.read_text().rpartition(")")[2].split()[0] == "Z"


def test_the_document_is_read_from_the_end_past_the_server_log() -> None:
    """The log comes first and last; the answer is the one JSON value with only log after it."""
    doc = {"sessionKey": "k1", "text": "a reply\n{ not json }", "settled": True}
    stdout = "\n".join(
        [
            "[server 12:31:37] ui-cli server on http://127.0.0.1:4970/?token=abc",
            '{"stray": "a line that parses but is not last"}',
            json.dumps(doc, indent=2),
            "[server 12:31:39] shutting down",
            "",
        ]
    )
    assert last_document(stdout) == doc


def test_a_stdout_with_no_document_reads_as_none() -> None:
    assert last_document("[server 12:31:37] up\n[server 12:31:38] down\n") is None
    # Text after the last value is not a log line, so it is not the answer either.
    assert last_document('{"a": 1}\ntrailing words\n') is None


def test_the_document_preserves_agent_owned_tokens() -> None:
    line = "open http://127.0.0.1:4970/?token=example-token&x=1 or token=other"
    document = {"text": line, "settled": True}
    assert last_document(f"{TOKEN_LINE}\n{json.dumps(document)}") == document


def test_a_servers_port_is_read_from_its_announcement_on_loopback_only() -> None:
    line = "[server 12:31:37] mirasim server v0.0.465: http://localhost:34403/?token=x (workdir /w)"
    assert uicli.server_port(line) == 34403
    assert uicli.server_port(line.replace("localhost", "127.0.0.1")) == 34403
    assert uicli.server_port(line.replace("localhost", "[::1]")) == 34403
    # Anywhere but loopback is a server this adapter did not start.
    assert uicli.server_port(line.replace("localhost", "192.168.7.132")) is None
    assert uicli.server_port(line.replace(":34403/", ":70000/")) is None
    assert uicli.server_port(TOKEN_LINE) is None
    assert uicli.server_port("") is None


@posix_only
@pytest.mark.slow
async def test_a_running_command_announces_its_port_before_it_answers(tmp_path: Path) -> None:
    """The whole reason a turn can be watched while it runs: the port, not only at the end."""
    cli = UiCli(executable=str(stub_executable(tmp_path, mode="asks")))
    invocation = cli.start(prompt_args(text="paint it", agent="claude", cwd=str(tmp_path)))
    running = asyncio.create_task(invocation.run())
    try:
        port = await asyncio.wait_for(invocation.announced(), 20)
        assert port is not None and invocation.port == port
        assert not running.done(), "the turn settled before its port was known, so nothing was observed"
        # The stand-in names the turn it then shows after the port it announced,
        # and writes it a moment after the announcement.
        shown = tmp_path / "stub" / "turns" / f"{port}.json"
        deadline = time.monotonic() + 15
        while not shown.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert shown.exists()
    finally:
        await invocation.cancel()
        await asyncio.wait_for(running, 15)


@posix_only
async def test_a_command_with_no_server_announces_nothing(tmp_path: Path) -> None:
    invocation = UiCli(executable=str(stub_executable(tmp_path))).start(["version"])
    await invocation.run()
    assert await invocation.announced() is None


def test_a_command_sent_to_a_server_names_the_port_before_the_verb() -> None:
    cli = UiCli(executable="/usr/bin/true")
    assert cli.start(["agents"]).argv == ["/usr/bin/true", "ui-cli", "agents"]
    assert cli.start(["agents"], port=4970).argv == ["/usr/bin/true", "ui-cli", "--port=4970", "agents"]


def test_a_command_that_starts_a_server_names_the_directory_it_runs_in() -> None:
    cli = UiCli(executable="/usr/bin/true", workdir="/srv/mirasim")
    assert cli.start(["agents"]).argv == ["/usr/bin/true", "ui-cli", "--workdir=/srv/mirasim", "agents"]
    # Sent to a server that is running, it starts none, so there is no directory to name.
    assert cli.start(["agents"], port=4970).argv == ["/usr/bin/true", "ui-cli", "--port=4970", "agents"]


def test_the_frames_of_a_long_turn_are_asked_for_one_at_a_time() -> None:
    assert uicli.sessions_args(5) == ["raw", '{"type": "listSessions"}', "--await=sessions", "--seconds=5"]
    assert uicli.snapshot_args("k1", 5) == [
        "raw",
        '{"type": "subscribe", "sessionKey": "k1"}',
        "--await=snapshot",
        "--seconds=5",
    ]
    assert uicli.answer_args("p1", "select", "Red") == ["answer", "--prompt-id=p1", "--action=select", "--value=Red"]
    assert uicli.answer_args("p1", "skip") == ["answer", "--prompt-id=p1", "--action=skip"]


def test_the_session_a_server_is_running_is_the_one_carrying_a_transport() -> None:
    doc = {
        "sent": {"type": "listSessions"},
        "responses": [
            {
                "type": "sessions",
                "sessions": [
                    {"sessionKey": "claude:other", "agent": "claude", "runState": "running"},
                    {"sessionKey": "pi:mine", "agent": "pi", "runState": "streaming", "transport": {"inFlight": 1}},
                ],
            }
        ],
    }
    assert uicli.live_session(doc) == "pi:mine"
    # Two of them is a server this adapter did not start, and nothing is guessed at.
    doc["responses"][0]["sessions"].append({"sessionKey": "pi:other", "transport": {}})
    assert uicli.live_session(doc) is None
    assert uicli.live_session(None) is None and uicli.live_session({"responses": []}) is None


def test_a_live_snapshot_is_read_only_for_a_session_the_server_is_running() -> None:
    snapshot = {"phase": "streaming", "interactions": [{"promptId": "p"}]}
    doc = {
        "responses": [
            {"type": "outbox", "sessionKey": "k1", "items": []},
            {"type": "snapshot", "sessionKey": "k1", "snapshot": snapshot},
        ]
    }
    assert uicli.snapshot_of(doc, "k1") == snapshot
    # A server answers a subscribe for a session it is not running with no snapshot at all.
    assert uicli.snapshot_of(doc, "k9") is None
    assert uicli.snapshot_of({"responses": [{"type": "outbox"}]}, "k1") is None


def test_a_new_conversation_names_the_agent_and_a_continued_one_only_its_key() -> None:
    fresh = prompt_args(text="-v looks like a flag", agent="claude", cwd="/w", model="opus", effort="high")
    assert fresh == [
        "prompt",
        "--text=-v looks like a flag",
        f"--timeout={TURN_TIMEOUT_MS}",
        "--agent=claude",
        "--cwd=/w",
        "--model=opus",
        "--effort=high",
    ]
    continued = prompt_args(text="go on", agent="claude", cwd="/w", session_key="k1", files=["/tmp/a.md"])
    assert continued == ["prompt", "--text=go on", f"--timeout={TURN_TIMEOUT_MS}", "--session=k1", "--file=/tmp/a.md"]


def test_a_route_is_named_by_a_new_conversation_and_switched_for_a_continued_one() -> None:
    fresh = prompt_args(text="hi", agent="claude", cwd="/w", route="platform")
    assert fresh[-1] == "--route=platform"
    # Mirasim reads --route on a new session's first turn only, so a continued
    # one is put on its route with a command of its own before the turn.
    continued = prompt_args(text="hi", agent="claude", cwd="/w", session_key="k1", route="platform")
    assert not any(arg.startswith("--route") for arg in continued)
    assert route_args("k1", "own") == ["session", "route", "--session=k1", "--to=own"]


def test_a_codex_home_inside_mirasims_state_is_dropped_and_any_other_kept(tmp_path: Path) -> None:
    state = tmp_path / "mirasim-home"
    borrowed = child_env({"MIRASIM_HOME": str(state), "CODEX_HOME": str(state / "agent-homes" / "codex-relay")})
    assert "CODEX_HOME" not in borrowed
    own = child_env({"MIRASIM_HOME": str(state), "CODEX_HOME": str(tmp_path / "my-codex"), "PATH": "/bin"})
    assert own["CODEX_HOME"] == str(tmp_path / "my-codex")
    assert own["PATH"] == "/bin"


def test_a_failure_preserves_the_last_non_log_stderr_line() -> None:
    outcome = Outcome(1, None, "Error: bad --session token=abc\n[server 12:31:39] shutting down\n")
    assert outcome.failure == "Error: bad --session token=abc"
    assert session_gone("ui-cli: --session: not one of this server's sessions")


@posix_only
async def test_query_reads_the_document_and_names_a_failure(tmp_path: Path) -> None:
    cli = UiCli(executable=str(stub_executable(tmp_path)))
    agents = await cli.query(["agents"])
    assert [row["id"] for row in agents["agents"]] == ["claude", "zcode", "qwen"]
    with pytest.raises(UiCliError, match="unknown command"):
        await cli.query(["nonsense"])
    assert await cli.version() == "0.0.465"


async def test_version_errors_preserve_the_commands_content(monkeypatch):
    from unittest.mock import AsyncMock

    class FailedVersion:
        returncode = 1

        async def communicate(self):
            return b"", b"version failed: https://agent.invalid/?token=agent-example\n"

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=FailedVersion()))
    with pytest.raises(UiCliError) as caught:
        await UiCli(executable="mirasim").version()
    assert str(caught.value) == "`mirasim version` exited 1: version failed: https://agent.invalid/?token=agent-example"


async def test_a_command_that_cannot_start_is_a_uicli_error(tmp_path: Path) -> None:
    cli = UiCli(executable=str(tmp_path / "no-such-mirasim"))
    with pytest.raises(UiCliError, match="cannot start"):
        await cli.query(["agents"])
    with pytest.raises(UiCliError, match="cannot start"):
        await cli.version()


async def test_a_cancel_before_the_command_starts_runs_nothing() -> None:
    invocation = Invocation(["/definitely/not/run"], env=dict(os.environ), cwd=None)
    await invocation.cancel()
    outcome = await invocation.run()
    assert outcome.cancelled and outcome.exit_code is None


@posix_only
@pytest.mark.slow
async def test_a_cancel_ends_the_command_and_the_grandchild_in_its_own_session(tmp_path: Path) -> None:
    """The measured hole: an agent in a session of its own outlived a killed ``ui-cli``.

    The stand-in starts its grandchild with ``start_new_session``, as Mirasim
    starts its agent, so only a tree read before the first signal can reach it.
    """
    cli = UiCli(executable=str(stub_executable(tmp_path, mode="hang")))
    invocation = cli.start(prompt_args(text="wait", agent="claude", cwd=str(tmp_path)))
    running = asyncio.create_task(invocation.run())
    marker = tmp_path / "stub" / "grandchild.pid"
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    grandchild = int(marker.read_text())
    assert not _gone(grandchild)

    await invocation.cancel()
    outcome = await asyncio.wait_for(running, 10)

    assert outcome.cancelled
    deadline = time.monotonic() + 5
    while not _gone(grandchild) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert _gone(grandchild), "the grandchild outlived the cancel"


_ADAPTER = """
import asyncio, sys
from raven.acp.mirasim.uicli import UiCli, prompt_args
cli = UiCli(executable=sys.argv[1])
asyncio.run(cli.start(prompt_args(text="wait", agent="claude", cwd=sys.argv[2])).run())
"""


@posix_only
@pytest.mark.slow
async def test_a_killed_adapter_still_takes_the_turn_down_with_it(tmp_path: Path) -> None:
    """Raven ends a connection with SIGKILL to the adapter's group, which no handler sees.

    The adapter here is a stand-in process holding one running turn, killed the
    way ``AcpClient.close`` kills one. The guard reads that as its stdin closing.
    """
    exe = stub_executable(tmp_path, mode="hang")
    adapter = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _ADAPTER, str(exe), str(tmp_path), start_new_session=True
    )
    marker = tmp_path / "stub" / "grandchild.pid"
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    grandchild = int(marker.read_text())

    os.killpg(adapter.pid, 9)
    await adapter.wait()

    deadline = time.monotonic() + 8
    while not _gone(grandchild) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert _gone(grandchild), "the turn outlived the adapter that started it"


@posix_only
@pytest.mark.slow
async def test_end_tree_leaves_this_process_group_alone(tmp_path: Path) -> None:
    """``ui-cli`` shares the adapter's group, so the group is never signalled whole."""
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
    assert os.getpgid(proc.pid) == os.getpgrp()
    await end_tree(proc, grace_s=2)
    assert proc.returncode is not None
    # Still here to assert it: this process was in the group and was not signalled.


def test_the_process_table_finds_this_process_and_its_parent() -> None:
    table = uicli._process_table()
    assert os.getpid() in table
    assert table[os.getpid()][0] == os.getppid()


def test_ps_output_is_read_without_its_zombies() -> None:
    """The ``ps`` branch, which a machine with ``/proc`` never takes: macOS and the BSDs."""
    listing = "\n".join(
        [
            "    1     0     1 Ss",
            "  501     1   501 S",
            "  502   501   502 Z",
            "  503   501   501 R+",
            "  garbage line",
            "",
        ]
    )
    assert uicli._ps_table(listing) == {1: (0, 1), 501: (1, 501), 503: (501, 501)}


async def _wait_for_pid(marker: Path, *, limit: float = 15) -> int:
    deadline = time.monotonic() + limit
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return int(marker.read_text())


async def _read_all(fd: int) -> bytes:
    def drain() -> bytes:
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)

    return await asyncio.to_thread(drain)


@posix_only
async def test_the_guard_relays_output_and_answers_with_the_exit_status() -> None:
    from raven.acp.mirasim.guard import guard

    hang_r, hang_w = os.pipe()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    script = "import sys; print('to stdout'); print('to stderr', file=sys.stderr); sys.exit(3)"
    try:
        code = await guard([sys.executable, "-c", script], stdin_fd=hang_r, stdout_fd=out_w, stderr_fd=err_w)
    finally:
        for fd in (out_w, err_w, hang_w):
            os.close(fd)
    assert code == 3
    assert await _read_all(out_r) == b"to stdout\n"
    assert await _read_all(err_r) == b"to stderr\n"
    for fd in (hang_r, out_r, err_r):
        os.close(fd)


@posix_only
@pytest.mark.slow
async def test_the_guard_ends_the_tree_when_its_stdin_closes(tmp_path: Path) -> None:
    """In-process, so the hang-up branch is measured where the coverage gate can see it."""
    from raven.acp.mirasim.guard import guard

    exe = stub_executable(tmp_path, mode="hang")
    hang_r, hang_w = os.pipe()
    out_r, out_w = os.pipe()
    running = asyncio.create_task(
        guard([str(exe), "ui-cli", "prompt", "--text=wait"], stdin_fd=hang_r, stdout_fd=out_w, stderr_fd=out_w)
    )
    grandchild = await _wait_for_pid(tmp_path / "stub" / "grandchild.pid")
    os.close(hang_w)
    code = await asyncio.wait_for(running, 15)
    os.close(out_w)
    assert code == 128 + 15
    assert b"turn still running" in await _read_all(out_r)
    deadline = time.monotonic() + 5
    while not _gone(grandchild) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert _gone(grandchild)
    os.close(hang_r)
    os.close(out_r)


async def test_a_guard_that_cannot_start_its_command_says_so_and_answers_127(tmp_path: Path) -> None:
    from raven.acp.mirasim.guard import guard

    hang_r, hang_w = os.pipe()
    err_r, err_w = os.pipe()
    code = await guard([str(tmp_path / "missing")], stdin_fd=hang_r, stdout_fd=err_w, stderr_fd=err_w)
    os.close(err_w)
    assert code == 127
    assert b"cannot start" in await _read_all(err_r)
    for fd in (hang_r, hang_w, err_r):
        os.close(fd)


def test_the_guard_entry_needs_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    from raven.acp.mirasim import guard as guard_mod

    assert guard_mod.main([]) == 2
    assert guard_mod.main(["--"]) == 2
    seen: list[list[str]] = []

    async def fake(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(guard_mod, "guard", fake)
    assert guard_mod.main(["--", "mirasim", "ui-cli", "agents"]) == 0
    assert seen == [["mirasim", "ui-cli", "agents"]]
    monkeypatch.setattr(sys, "argv", ["guard"])
    monkeypatch.delitem(sys.modules, "raven.acp.mirasim.guard")
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("raven.acp.mirasim.guard", run_name="__main__")
    assert stopped.value.code == 2


@posix_only
@pytest.mark.slow
async def test_a_terminated_guard_still_ends_the_tree(tmp_path: Path) -> None:
    """Without the guard's own SIGTERM handler it would die and leave the stand-in writing into a dead pipe."""
    import signal

    cli = UiCli(executable=str(stub_executable(tmp_path, mode="hang")))
    invocation = cli.start(prompt_args(text="wait", agent="claude", cwd=str(tmp_path)))
    running = asyncio.create_task(invocation.run())
    grandchild = await _wait_for_pid(tmp_path / "stub" / "grandchild.pid")
    assert invocation._proc is not None
    os.kill(invocation._proc.pid, signal.SIGTERM)
    outcome = await asyncio.wait_for(running, 15)
    assert outcome.exit_code == 128 + 15
    deadline = time.monotonic() + 5
    while not _gone(grandchild) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert _gone(grandchild)


@posix_only
@pytest.mark.slow
async def test_a_helper_holding_the_commands_output_does_not_hold_the_answer(tmp_path: Path) -> None:
    """The relay's first reason: with the output inherited, this read lasts as long as the helper does."""
    import signal

    cli = UiCli(executable=str(stub_executable(tmp_path, mode="stray")))
    started = time.monotonic()
    outcome = await asyncio.wait_for(cli.start(prompt_args(text="hi", agent="claude", cwd=str(tmp_path))).run(), 20)
    took = time.monotonic() - started
    helper = int((tmp_path / "stub" / "stray.pid").read_text())
    try:
        assert outcome.exit_code == 0
        assert outcome.document["text"].endswith(": hi")
        assert took < 10, f"the answer waited {took:.1f}s on a helper that holds the output"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(helper, signal.SIGKILL)


async def test_a_version_that_fails_or_hangs_is_a_uicli_error(tmp_path: Path) -> None:
    with pytest.raises(UiCliError, match="exited 1: version: cannot read the install"):
        await UiCli(executable=str(stub_executable(tmp_path, mode="mute"))).version()
    with pytest.raises(UiCliError, match="did not answer within"):
        await UiCli(executable=str(stub_executable(tmp_path, mode="slow"))).version(timeout_s=0.3)


@posix_only
async def test_end_tree_kills_what_ignores_the_polite_signal() -> None:
    script = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
    )
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE)
    assert proc.stdout is not None
    await proc.stdout.readline()
    await end_tree(proc, grace_s=0.3)
    assert proc.returncode == -9


def test_the_process_table_falls_back_to_ps_without_proc(tmp_path: Path) -> None:
    table = uicli._process_table(tmp_path / "no-proc-here")
    assert table[os.getpid()][0] == os.getppid()


def test_the_proc_reader_skips_vanished_and_zombie_entries(tmp_path: Path) -> None:
    for pid, stat in (("10", "10 (a b) S 1 10 10"), ("11", "11 (zombie)) Z 10 10 10"), ("12", None), ("self", None)):
        (tmp_path / pid).mkdir()
        if stat is not None:
            (tmp_path / pid / "stat").write_text(stat)
    assert uicli._proc_table(tmp_path) == {10: (1, 10)}


def test_liveness_reads_zombies_as_gone_and_a_refused_probe_as_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    me = str(os.getpid())
    (tmp_path / me).mkdir()
    (tmp_path / me / "stat").write_text(f"{me} (python) Z 1 1 1")
    assert uicli._alive(os.getpid(), tmp_path) is False
    (tmp_path / me / "stat").unlink()
    (tmp_path / me / "stat").mkdir()
    assert uicli._alive(os.getpid(), tmp_path) is False
    assert uicli._alive(os.getpid(), tmp_path / "no-proc-here") is True

    def refused(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(uicli.os, "kill", refused)
    assert uicli._alive(1) is True


async def test_a_signal_to_a_process_already_gone_is_not_an_error() -> None:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await proc.wait()
    uicli._quietly(os.kill, proc.pid, 15)


async def test_the_windows_path_runs_taskkill_then_reaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows ``taskkill /T`` walks the tree; here a stand-in records it was asked."""
    asked = tmp_path / "asked"
    taskkill = tmp_path / "taskkill"
    taskkill.write_text(f'#!/bin/sh\necho "$@" > "{asked}"\n')
    taskkill.chmod(0o755)
    monkeypatch.setattr(uicli.shutil, "which", lambda name: str(taskkill) if name == "taskkill" else None)
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
    await uicli._taskkill(proc)
    assert proc.returncode == -9
    assert asked.read_text().split() == ["/T", "/F", "/PID", str(proc.pid)]


async def test_the_guard_treats_an_unreadable_stdin_as_a_hang_up(tmp_path: Path) -> None:
    from raven.acp.mirasim.guard import guard

    out_r, out_w = os.pipe()
    unread = 9999
    with pytest.raises(OSError):
        os.fstat(unread)
    code = await asyncio.wait_for(
        guard([sys.executable, "-c", "import time; time.sleep(60)"], stdin_fd=unread, stdout_fd=out_w, stderr_fd=out_w),
        15,
    )
    assert code == 128 + 15
    for fd in (out_r, out_w):
        os.close(fd)


async def test_the_guard_keeps_draining_when_nobody_reads_its_output() -> None:
    from raven.acp.mirasim.guard import guard

    hang_r, hang_w = os.pipe()
    os.write(hang_w, b"x")
    out_r, out_w = os.pipe()
    os.close(out_r)
    code = await guard([sys.executable, "-c", "print('x' * 200000)"], stdin_fd=hang_r, stdout_fd=out_w, stderr_fd=out_w)
    assert code == 0
    for fd in (hang_r, hang_w, out_w):
        os.close(fd)


@posix_only
async def test_the_guard_does_not_wait_on_a_helper_holding_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    from raven.acp.mirasim import guard as guard_mod

    monkeypatch.setattr(guard_mod, "DRAIN_S", 0.3)
    exe = stub_executable(tmp_path, mode="stray")
    hang_r, hang_w = os.pipe()
    out_r, out_w = os.pipe()
    try:
        code = await asyncio.wait_for(
            guard_mod.guard(
                [str(exe), "ui-cli", "prompt", "--text=hi"], stdin_fd=hang_r, stdout_fd=out_w, stderr_fd=out_w
            ),
            15,
        )
        assert code == 0
    finally:
        with contextlib.suppress(FileNotFoundError, ProcessLookupError):
            os.kill(int((tmp_path / "stub" / "stray.pid").read_text()), signal.SIGKILL)
        for fd in (hang_r, hang_w, out_r, out_w):
            os.close(fd)


@posix_only
@pytest.mark.slow
async def test_a_read_that_runs_past_its_budget_is_ended_and_reported(tmp_path: Path) -> None:
    """The stand-in shrugs off SIGTERM, so it is still there unless the timeout waits for the tree to go."""
    cli = UiCli(executable=str(stub_executable(tmp_path, mode="stubborn")))
    # A budget a loaded machine still starts the stand-in inside, so the read
    # it ends is one that was really running.
    reading = asyncio.create_task(cli.query(["agents"], timeout_s=4.0))
    deadline = time.monotonic() + 8
    while not _stubs_of(tmp_path) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert _stubs_of(tmp_path), "the stand-in never started, so nothing below would be measured"
    with pytest.raises(UiCliError, match="did not answer within"):
        await reading
    # Ended with its tree before the timeout is reported, not left to finish
    # the read on its own: checked at once, with no wait, because a caller's
    # cancel is only settled when what it started is gone.
    assert not _stubs_of(tmp_path)


def _stubs_of(folder: Path) -> list[int]:
    """Live stand-ins writing their state under ``folder``, found by the environment they carry."""
    marker = f"MIRASIM_STUB_DIR={folder / 'stub'}".encode()
    found = []
    for pid in uicli._process_table():
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue
        if marker in environ.split(b"\0") and uicli._alive(pid):
            found.append(pid)
    return found
