"""What an agent on a machine writes on stderr reaches the host whole, whichever connection held its task then.

The machine's daemon keeps an agent's stderr in the task's record and numbers its lines as it numbers frames;
an attach replays the lines past those its connection has seen, after the frames. So a pick-up -- a process that
never held the task -- reads all of it, as a cli entry's reply, failure and session id need, and an endpoint whose
link went reads what was written while it was down. The tests cover the daemon, the host's endpoint and pool, and
the ways a cli run meets it end to end: picked up after this computer stopped, its reason or the session id a
CLI such as hermes names only on stderr read off the record, and watched across a lost link.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

if sys.platform == "win32":
    pytest.skip("the daemon is POSIX: it locks with fcntl and listens on a unix socket", allow_module_level=True)

from raven.agent.subagent.backends import build_third_party_backend
from raven.agent.subagent.instances import InstanceRegistry
from raven.node import protocol, server
from raven.node.client import ChannelEndpoint, NodePool
from tests.test_node_client import _box, _live
from tests.test_node_server import _Conn
from tests.test_subagent_acp_remote import Machine, box  # noqa: F401 - the fixture, by name
from tests.test_subagent_cli_remote import _gone, _run, _until, cli_config

# --- the daemon --------------------------------------------------------------


def _python(tmp_path: Path, body: str) -> str:
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return f"{sys.executable} {script}"


def _told(conn: _Conn) -> list[tuple[str, dict[str, Any]]]:
    """Every notification the connection was sent, in order."""
    return [(frame["method"], frame["params"]) for frame in conn.frames() if "method" in frame]


@pytest.mark.slow
async def test_stderr_lines_go_out_numbered_and_are_kept_as_written(tmp_path):
    """Numbered like frames, a blank line included, so a count read off the wire is a place in the record."""
    daemon = server.Daemon(tmp_path / "node")
    host = _Conn(daemon)
    command = _python(
        tmp_path,
        """
        import sys
        for line in ("e1", "", "e3"):
            print(line, file=sys.stderr, flush=True)
        """,
    )

    await host.ask(protocol.AGENT_LAUNCH, {"channel": 1, "task": "t-err", "command": command, "cwd": str(tmp_path)})
    await host.until(lambda: host.notified(protocol.AGENT_EXIT))

    assert [(e["text"], e["n"]) for e in host.notified(protocol.AGENT_STDERR)] == [("e1", 1), ("", 2), ("e3", 3)]
    assert (daemon.record_dir("t-err") / "stderr.log").read_bytes() == b"e1\n\ne3\n"


@pytest.mark.slow
async def test_an_attach_replays_the_stderr_its_connection_has_not_seen_then_hands_on_what_comes(tmp_path):
    daemon = server.Daemon(tmp_path / "node")
    first = _Conn(daemon)
    command = _python(
        tmp_path,
        """
        import sys
        def say(text):
            print(text, file=sys.stderr, flush=True)
        say("e1")
        sys.stdin.readline()
        say("e2")
        say("e3")
        sys.stdin.readline()
        say("e4")
        """,
    )
    await first.ask(protocol.AGENT_LAUNCH, {"channel": 1, "task": "t-gap", "command": command, "cwd": str(tmp_path)})
    await first.until(lambda: first.notified(protocol.AGENT_STDERR))

    # The connection goes; the agent writes two lines nobody holds it to hear.
    await first.node.detach_all()
    await daemon.tasks["t-gap"].write("go")
    log = daemon.record_dir("t-gap") / "stderr.log"
    await first.until(lambda: log.read_bytes().count(b"\n") == 3)

    second = _Conn(daemon)
    await second.ask(
        protocol.AGENT_ATTACH, {"channel": 7, "task": "t-gap", "after": 0, "stderr_after": 1}, request_id=2
    )
    assert [(e["text"], e["n"]) for e in second.notified(protocol.AGENT_STDERR)] == [("e2", 2), ("e3", 3)]

    await second.tell(protocol.AGENT_FRAME, {"channel": 7, "line": "go"})
    await second.until(lambda: second.notified(protocol.AGENT_EXIT))
    assert [(e["text"], e["n"]) for e in second.notified(protocol.AGENT_STDERR)] == [
        ("e2", 2),
        ("e3", 3),
        ("e4", 4),
    ], "the replay, then the live line numbered on from it"
    assert [e["text"] for e in first.notified(protocol.AGENT_STDERR)] == ["e1"], (
        "the connection that went hears no more"
    )


@pytest.mark.slow
@pytest.mark.parametrize("restarted", [False, True], ids=["the daemon that ran it", "a daemon that never ran it"])
async def test_a_finished_task_replays_its_stderr_after_its_frames_and_before_its_exit(tmp_path, restarted):
    root = tmp_path / "node"
    first = _Conn(server.Daemon(root))
    command = _python(
        tmp_path,
        """
        import sys
        print("partial answer", flush=True)
        print("token expired", file=sys.stderr, flush=True)
        sys.exit(3)
        """,
    )
    await first.ask(protocol.AGENT_LAUNCH, {"channel": 1, "task": "t-fail", "command": command, "cwd": str(tmp_path)})
    await first.until(lambda: first.notified(protocol.AGENT_EXIT))

    later = _Conn(server.Daemon(root) if restarted else first.node.daemon)
    attached = await later.ask(protocol.AGENT_ATTACH, {"channel": 2, "task": "t-fail", "after": 0, "stderr_after": 0})

    assert attached["result"]["rc"] == 3
    assert _told(later) == [
        (protocol.AGENT_FRAME, {"channel": 2, "line": "partial answer", "n": 1}),
        (protocol.AGENT_STDERR, {"channel": 2, "text": "token expired", "n": 1}),
        (protocol.AGENT_EXIT, {"channel": 2, "rc": 3}),
    ]


def _recorded(daemon: server.Daemon, task: str, *, stderr: bytes | None) -> None:
    """A finished task's record, as a daemon before this one left it."""
    record = daemon.record_dir(task)
    record.mkdir(parents=True)
    info = {"task": task, "state": "exited", "rc": 1, "pid": 1, "started": 1.0, "ended": 2.0, "frames": 0}
    (record / "task.json").write_text(json.dumps(info), encoding="utf-8")
    if stderr is not None:
        (record / "stderr.log").write_bytes(stderr)


@pytest.mark.parametrize(
    ("asked", "replayed"),
    [
        ({}, []),
        ({"stderr_after": 0}, [("one", 1), ("two", 2)]),
        ({"stderr_after": 1}, [("two", 2)]),
        ({"stderr_after": 2}, []),
        ({"stderr_after": 9}, []),
    ],
    ids=["not asked", "from the first", "past the first", "past the last", "past the end"],
)
async def test_an_attach_replays_exactly_the_stderr_lines_it_asks_past(tmp_path, asked, replayed):
    """An attach that does not ask hears no stderr, as one from before the lines were numbered."""
    daemon = server.Daemon(tmp_path / "node")
    _recorded(daemon, "t-rec", stderr=b"one\ntwo\n")
    conn = _Conn(daemon)

    await conn.ask(protocol.AGENT_ATTACH, {"channel": 1, "task": "t-rec", **asked})

    assert [(e["text"], e["n"]) for e in conn.notified(protocol.AGENT_STDERR)] == replayed
    assert conn.notified(protocol.AGENT_EXIT) == [{"channel": 1, "rc": 1}]


async def test_a_record_with_no_stderr_kept_replays_none(tmp_path):
    daemon = server.Daemon(tmp_path / "node")
    _recorded(daemon, "t-quiet", stderr=None)
    conn = _Conn(daemon)

    answer = await conn.ask(protocol.AGENT_ATTACH, {"channel": 1, "task": "t-quiet", "stderr_after": 0})

    assert answer["result"]["rc"] == 1 and conn.notified(protocol.AGENT_STDERR) == []


@pytest.mark.parametrize("value", [-1, "1", True, 1.5])
async def test_a_stderr_place_that_is_not_a_whole_number_is_refused(tmp_path, value):
    daemon = server.Daemon(tmp_path / "node")
    _recorded(daemon, "t-x", stderr=b"one\n")
    conn = _Conn(daemon)

    answer = await conn.ask(protocol.AGENT_ATTACH, {"channel": 1, "task": "t-x", "stderr_after": value})

    assert answer["error"]["code"] == -32602 and "stderr_after must be a whole number" in answer["error"]["message"]
    assert conn.notified(protocol.AGENT_STDERR) == [] and conn.notified(protocol.AGENT_EXIT) == []


# --- the host's endpoint and pool -------------------------------------------


async def test_the_endpoint_counts_its_stderr_lines_by_the_numbers_the_node_gives_them():
    endpoint = ChannelEndpoint(SimpleNamespace(alive=True), 1, target=_box(), agent="echo@box", task="echo@box-1")  # type: ignore[arg-type]

    endpoint.feed_stderr("one", 1)
    endpoint.feed_stderr("three", 3)
    assert endpoint.stderr_received == 3, "a replay goes on from where the node numbered"
    endpoint.feed_stderr("four")
    endpoint.feed_stderr("bool", True)

    assert endpoint.stderr_received == 5, "a line with no usable number counts on from the last"
    assert [await endpoint.readerr() for _ in range(4)] == [b"one\n", b"three\n", b"four\n", b"bool\n"]


async def test_a_parked_endpoint_is_attached_again_from_the_frame_and_the_stderr_line_it_last_read(tmp_path):
    pool = NodePool()
    client = _live(pool, tmp_path, result={"pid": 7, "rc": None})
    endpoint = ChannelEndpoint(client, 1, target=_box(), agent="echo@box", task="echo@box-1")  # type: ignore[arg-type]
    endpoint.feed_frame("a", 1)
    endpoint.feed_frame("b", 2)
    for n in (1, 2, 3):
        endpoint.feed_stderr(f"e{n}", n)
    endpoint.detach("the link went")
    pool._parked["box"] = [endpoint]

    await pool._reattach("box")

    assert client.asked[-1][:2] == (
        protocol.AGENT_ATTACH,
        {"channel": 1, "task": "echo@box-1", "after": 2, "stderr_after": 3},
    )
    assert endpoint.detached is None
    await pool.close_all()


async def test_a_pick_up_attaches_from_the_stderr_line_it_is_told_and_from_the_first_by_default(tmp_path):
    """A cli pick-up asks from the first line (the command's own stderr); an acp pick-up from the line its
    record kept as the prompt went out, whatever frame it reads from."""
    pool = NodePool()
    client = _live(pool, tmp_path, result={"pid": 7, "rc": None})

    endpoint, _answer = await pool.attach("box", "echo@box-1", name="echo@box", after=4)
    assert client.asked[-1][:2] == (
        protocol.AGENT_ATTACH,
        {"channel": 1, "task": "echo@box-1", "after": 4, "stderr_after": 0},
    )
    assert endpoint.received == 4 and endpoint.stderr_received == 0

    endpoint, _answer = await pool.attach("box", "stub@box-1", name="stub@box", after=4, stderr_after=7)
    assert client.asked[-1][:2] == (
        protocol.AGENT_ATTACH,
        {"channel": 2, "task": "stub@box-1", "after": 4, "stderr_after": 7},
    )
    assert endpoint.received == 4 and endpoint.stderr_received == 7, "a lost link goes on from there"
    await pool.close_all()


# --- a cli run end to end, on the stand-in machine ---------------------------


@pytest.mark.slow
@pytest.mark.parametrize("when", ["before", "after"], ids=["said while held", "said while this computer was away"])
async def test_a_left_cli_run_s_reason_on_stderr_reaches_its_pick_up(box: Machine, tmp_path, monkeypatch, when):
    """The pick-up is a process that held none of the run: the daemon gives it the whole stderr, the reason in it."""
    from raven.acp_client import remote, remote_runs

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    say = "echo token expired >&2"
    wait = "while [ ! -f go ]; do sleep 0.05; done"
    steps = [say, "echo $$ > pid.txt", wait] if when == "before" else ["echo $$ > pid.txt", wait, say]
    backend = build_third_party_backend(cli_config(f"sh -c '{'; '.join(steps)}; exit 3'"))
    turn = asyncio.ensure_future(
        backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, session_key="tui:s1", instance="left")
    )
    workdir = box.home / "raven-work" / "left"
    await _until((workdir / "pid.txt").exists)
    await _until(lambda: remote_runs.pending() != [])
    pid = int((workdir / "pid.txt").read_text(encoding="utf-8"))
    (run,) = remote_runs.pending()

    # This computer stops, the way the shutdown does it; the command goes on.
    remote.leave_agents_running()
    try:
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await box.nodes.close_all()
    finally:
        remote.staying()
    (workdir / "go").touch()
    await _until(lambda: _gone(pid))

    picked = await backend.pick_up(run)

    assert (picked.status, picked.reply) == ("error", "CLI agent 'where@box' exited 3: token expired")


@pytest.mark.slow
@pytest.mark.parametrize("when", ["before", "after"], ids=["said while held", "said while this computer was away"])
async def test_a_left_create_that_names_its_session_only_on_stderr_is_bound_to_it_by_the_pick_up(
    box: Machine, tmp_path, monkeypatch, when
):
    """The create's deferred commit never ran, so the pick-up binds the handle (#957) -- off stderr, where hermes says it."""
    from raven.acp_client import remote, remote_runs

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    say = "echo session slow-A1 >&2"
    wait = "while [ ! -f go ]; do sleep 0.05; done"
    steps = [say, "echo $$ > pid.txt", wait] if when == "before" else ["echo $$ > pid.txt", wait, say]
    cfg = cli_config(
        f"sh -c '{'; '.join(steps)}; echo done'",
        id_source="derived",
        session_id_pattern=r"session (\S+)",
        resume_command="sh -c 'cat' {agent_id}",
    )
    registry = InstanceRegistry(path=tmp_path / "instances.json")
    backend = build_third_party_backend(cfg, registry=registry)
    turn = asyncio.ensure_future(
        backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, session_key="tui:s1", instance="left")
    )
    workdir = box.home / "raven-work" / "left"
    await _until((workdir / "pid.txt").exists)
    await _until(lambda: remote_runs.pending() != [])
    pid = int((workdir / "pid.txt").read_text(encoding="utf-8"))
    (run,) = remote_runs.pending()

    remote.leave_agents_running()
    try:
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await box.nodes.close_all()
    finally:
        remote.staying()
    assert await registry.lookup("tui:s1", "where@box", "left") is None, "the create's own commit never ran"
    (workdir / "go").touch()
    await _until(lambda: _gone(pid))

    picked = await backend.pick_up(run)

    assert picked.status == "ok" and picked.reply.startswith("done")
    assert "not resumable" not in picked.reply
    assert await registry.lookup("tui:s1", "where@box", "left") == "slow-A1", "the next turn resumes this session"


@pytest.mark.slow
async def test_a_reason_written_on_stderr_while_the_link_was_down_reaches_the_run_watching_it(
    box: Machine, tmp_path, monkeypatch
):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    box.nodes._heartbeat_s = 0.1
    box.nodes._lost_after_s = 1.0
    # Long enough that the line is written well before the link is back.
    box.nodes._backoff_s = (1.0,)
    command = "sh -c 'echo $$ > pid.txt; while [ ! -f go ]; do sleep 0.05; done; echo token expired >&2; exit 3'"
    turn = asyncio.ensure_future(_run(cli_config(command), "hi", tmp_path, instance="down"))
    workdir = box.home / "raven-work" / "down"
    await _until((workdir / "pid.txt").exists)
    node = box.nodes._nodes["box"]
    (endpoint,) = node.channels.values()

    os.kill(node.client._proc.pid, signal.SIGKILL)
    await _until(lambda: endpoint.detached is not None)
    (workdir / "go").touch()

    with pytest.raises(RuntimeError, match=r"CLI agent 'where@box' exited 3: token expired$"):
        await asyncio.wait_for(turn, timeout=30)
    assert endpoint.stderr_received == 1
