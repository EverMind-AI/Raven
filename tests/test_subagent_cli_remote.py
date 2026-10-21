"""Sub-agents of every kind on a registered machine: a cli entry runs there through the machine's node, an
openai entry's endpoint is reached from there through a tunnel, and a built-in entry is told it runs here.

The stand-in machine is the one ``tests/test_subagent_acp_remote.py`` builds: a stand-in ``ssh`` on PATH hands
the one remote-command argument to a shell in the machine user's home, so the real ssh line, the real Raven node
and the real command all run, with only the network missing. A cli agent here is a shell command, and what it
prints is its reply. For the tunnel, a second stand-in forwards a port the way ``ssh -N -L`` does.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from raven.agent.subagent import probe
from raven.agent.subagent.backends import agent_meta, build_third_party_backend, format_agent_listing
from raven.agent.subagent.backends.cli_agent import CliAgentTimeoutError
from raven.agent.subagent.instances import InstanceRegistry
from raven.agent.subagent.mcp_grant import GrantedServer, McpGrant
from raven.agent.subagent.registry import AgentRegistry, _row_for
from raven.config.schema import (
    BuiltinAgentConfig,
    MCPServerConfig,
    ThirdPartyCliSubagentConfig,
    ThirdPartyOpenAISubagentConfig,
)
from raven.config.update_subagents import reject_unsupported_openai_fields
from raven.spine.message import Media
from tests.test_subagent_acp_remote import (  # noqa: F401 - the fixture, by name
    HOST,
    PORT,
    Machine,
    _hold_launch_answers,
    box,
)

# Every dispatch, test and ping here starts a real node over the stand-in ssh,
# and the command through it: real processes, real seconds.
pytestmark = pytest.mark.slow

# The stand-in for ``ssh -N -L``: it logs the call, fails the way ssh fails in
# the named modes, and otherwise forwards the local port to the far host and
# port, as sshd would from the machine.
_FORWARDING_SSH = """#!{python}
import json, os, socket, sys, threading
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "calls"), "a") as f:
    f.write(json.dumps(args) + "\\n")
mode = open(os.path.join(here, "mode")).read().strip()
log = args[args.index("-E") + 1] if "-E" in args else None
open(os.path.join(here, "tunnel.pid"), "w").write(str(os.getpid()))
if mode == "unreachable":
    open(log, "a").write("ssh: connect to host {host} port {port}: Operation timed out\\n")
    sys.exit(255)
if mode == "denied":
    open(log, "a").write("worker@{host}: Permission denied (publickey).\\n")
    sys.exit(255)
if mode == "silent":
    import time
    time.sleep(30)
if mode in ("port_taken", "port_taken_once"):
    # ssh's words when something took the port here first; with ExitOnForwardFailure it then ends.
    if mode == "port_taken_once":
        open(os.path.join(here, "mode"), "w").write("ok")
    open(log, "a").write("bind [127.0.0.1]:1: Address already in use\\n")
    open(log, "a").write("channel_setup_fwd_listener_tcpip: cannot listen to port: 1\\n")
    open(log, "a").write("Could not request local forwarding.\\n")
    sys.exit(255)
if "-N" not in args or "-L" not in args:
    sys.stderr.write("this stand-in only forwards\\n")
    sys.exit(2)
# The forward as ssh reads it: a bracketed IPv6 far host keeps its own colons.
bind_host, bind_port, far = args[args.index("-L") + 1].split(":", 2)
far_host, far_port = far.rsplit(":", 1)
far_host = far_host.strip("[]")
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind((bind_host, int(bind_port)))
listener.listen()

def pump(src, dst):
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

while True:
    near, _ = listener.accept()
    try:
        far = socket.create_connection((far_host, int(far_port)))
    except OSError:
        near.close()
        continue
    threading.Thread(target=pump, args=(near, far), daemon=True).start()
    threading.Thread(target=pump, args=(far, near), daemon=True).start()
"""


@pytest.fixture
def logs():
    lines: list[str] = []
    token = logger.add(lambda message: lines.append(str(message)), level="INFO")
    yield lines
    logger.remove(token)


@pytest.fixture
def forwarding(box: Machine) -> Machine:
    """The machine with the stand-in that forwards a port, in place of the one that runs a command."""
    ssh = box.bin / "ssh"
    ssh.write_text(_FORWARDING_SSH.format(python=sys.executable, host=HOST, port=PORT), encoding="utf-8")
    ssh.chmod(0o755)
    return box


def cli_config(command: str, **kw: Any) -> ThirdPartyCliSubagentConfig:
    return ThirdPartyCliSubagentConfig(
        name=kw.pop("name", "where@box"),
        command=command,
        machine=kw.pop("machine", "box"),
        remote_cwd=kw.pop("remote_cwd", "~/raven-work"),
        **kw,
    )


def http_config(base_url: str, **kw: Any) -> ThirdPartyOpenAISubagentConfig:
    return ThirdPartyOpenAISubagentConfig(
        name=kw.pop("name", "llm@box"), base_url=base_url, model="m", machine="box", **kw
    )


async def _run(cfg: Any, task: str, workspace: Path, **kw: Any) -> str:
    return await build_third_party_backend(cfg, **{k: kw.pop(k) for k in ("registry",) if k in kw}).run(
        task, task_id=kw.pop("task_id", "t1"), workspace=workspace, executor=None, **kw
    )


def _split(reply: str) -> tuple[str, str]:
    """The agent's own words, and raven's notes after them."""
    head, _, notes = reply.partition("\n\n[raven] ")
    return head, notes


async def _until(check: Any, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "did not happen in time"
        await asyncio.sleep(0.05)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == pid
    except ChildProcessError:
        return False


# ---- a cli agent on the machine --------------------------------------------------


async def test_a_cli_dispatch_runs_on_the_machine_in_a_directory_made_there_and_says_where(box, tmp_path):
    local = tmp_path / "local-workspace"

    reply = await _run(cli_config("sh -c pwd"), "hi", local, instance="reviewer")

    there = box.home / "raven-work" / "reviewer"
    assert there.is_dir(), "the run's directory is made on the machine"
    head, note = _split(reply)
    assert Path(head.strip()).resolve() == there.resolve(), "the command ran in it"
    assert note.startswith("Ran on machine 'Lab box' (box), in ")
    assert f"in scp://worker@{HOST}:{PORT}//{str(there.resolve()).lstrip('/')};" in note
    where = note.split("in scp://", 1)[1].split(";", 1)[0]
    assert where.split("@", 1)[1].split(":", 1)[0] == HOST, "the address a model reads is the registry's, never the id"
    assert "the files it wrote are there, not on this computer" in note
    assert not local.exists(), "the caller's workspace is never touched"
    for argv in box.calls():
        assert str(local) not in " ".join(argv), "this computer's workspace path never reaches the machine"


async def test_the_prompt_reaches_a_cli_agent_there_the_three_ways_it_does_here(box, tmp_path):
    prompt = "first line\n\nthird line, after a blank one"

    on_stdin, _ = _split(await _run(cli_config("cat"), prompt, tmp_path, task_id="t-stdin"))
    as_argument, _ = _split(await _run(cli_config("printf %s {prompt}"), prompt, tmp_path, task_id="t-arg"))
    from_file, _ = _split(await _run(cli_config("cat {prompt_file}"), prompt, tmp_path, task_id="t-file"))

    assert on_stdin == as_argument == from_file == prompt, "every line as printed, the blank one included"
    written = box.home / "raven-work" / "t-file" / ".raven" / "prompt-t-file.txt"
    assert written.read_text(encoding="utf-8") == prompt, "the prompt file is written into the run's directory there"
    assert not list(tmp_path.glob("raven_subagent_*")), "no prompt file is made on this computer"


async def test_the_entry_s_env_reaches_the_agent_on_the_link_and_this_computer_s_shell_stays_here(
    box, tmp_path, monkeypatch
):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "raven-home"))
    command = "sh -c 'printf %s \"$FROM_ENTRY:$RAVEN_SUBAGENT:${RAVEN_HOME:-unset}\"'"

    head, _ = _split(await _run(cli_config(command, env={"FROM_ENTRY": "sk-raven-test-not-real"}), "hi", tmp_path))

    assert head == "sk-raven-test-not-real:1:unset", "the entry's env and the role mark, and not this host's home"
    for argv in box.calls():
        assert "sk-raven-test-not-real" not in " ".join(argv), "a value in env is never an ssh argument"


async def test_a_command_that_leaves_before_its_stdin_reaches_it_still_answers_with_every_line(box, tmp_path):
    """A quick command is gone before the prompt is sent: its own output and exit code are the report."""
    head, _ = _split(await _run(cli_config("sh -c 'printf \"a\\n\\nb\\n\"'"), "hi", tmp_path))

    assert head == "a\n\nb", "every line as printed, the blank one included"


class _Link:
    """A node link that records what is said on it and answers every request with nothing."""

    def __init__(self) -> None:
        self.alive = True
        self.said: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.said.append((method, params))

    async def request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        return {}


class _Placed:
    """A node pool whose one agent is ``endpoint``, already started: the dispatch's place on the machine."""

    def __init__(self, endpoint: Any) -> None:
        self.endpoint = endpoint

    async def session_dir(self, machine_id: str, path: str) -> tuple[str, float]:
        return f"/home/worker/raven-work/{path.rsplit('/', 1)[-1]}", 0.0

    async def write_file(self, machine_id: str, path: str, content: str, *, roots: Any) -> None:
        return None

    async def open_channel(self, machine_id: str, **kw: Any) -> Any:
        return self.endpoint


async def test_an_agent_gone_before_its_stdin_is_sent_is_read_as_it_left(box, tmp_path, monkeypatch):
    """The prompt reaches an agent that already left: nothing is sent, and what it printed and how it exited are the report."""
    from raven.acp_client import remote
    from raven.node.client import ChannelEndpoint
    from raven.ops import remote_path

    link = _Link()
    endpoint = ChannelEndpoint(link, 1, target=remote.machine("box"), agent="where@box")  # type: ignore[arg-type]
    endpoint.feed_frame("PONG", 1)
    endpoint.feed_frame("", 2)
    endpoint.feed_stderr("done already")
    endpoint.exited(0)
    monkeypatch.setattr(remote_path, "_NODES", lambda: _Placed(endpoint))

    head, _ = _split(await _run(cli_config("sh -c 'echo PONG'"), "hi", tmp_path))

    assert head == "PONG" and link.said == [], "nothing is sent to an agent that is gone"


async def test_a_link_down_too_long_for_the_prompt_fails_the_run(box, tmp_path, monkeypatch):
    from raven.acp_client import remote
    from raven.node import client as node_client
    from raven.node.client import ChannelEndpoint
    from raven.ops import remote_path

    link = _Link()
    endpoint = ChannelEndpoint(link, 1, target=remote.machine("box"), agent="where@box")  # type: ignore[arg-type]
    endpoint.detach("the link went")
    endpoint._pending = [(node_client.protocol.AGENT_FRAME, {"line": "x"})] * node_client._PENDING_FRAMES
    monkeypatch.setattr(remote_path, "_NODES", lambda: _Placed(endpoint))

    with pytest.raises(BrokenPipeError, match="down too long"):
        await _run(cli_config("cat"), "hi", tmp_path)


async def test_a_cli_agent_that_exits_non_zero_there_fails_with_its_one_line(box, tmp_path):
    with pytest.raises(RuntimeError, match=r"CLI agent 'where@box' exited 3: token expired$"):
        await _run(cli_config("sh -c 'echo token expired; exit 3'"), "hi", tmp_path)


async def test_a_timeout_ends_the_agent_s_whole_process_tree_on_the_machine(box, tmp_path):
    cfg = cli_config("sh -c 'echo $$ > pid.txt; exec sleep 30'", timeout=1)

    with pytest.raises(CliAgentTimeoutError, match="timed out after 1s"):
        await _run(cfg, "hi", tmp_path, instance="slow")

    pid = int((box.home / "raven-work" / "slow" / "pid.txt").read_text(encoding="utf-8"))
    await _until(lambda: _gone(pid))


async def test_a_timed_out_command_keeps_its_record_for_the_caller_that_reports_the_failure(box, tmp_path, monkeypatch):
    """As for the output: the failure is the caller's to announce, and the record waits until it has."""
    from raven.acp_client import remote_runs

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    cfg = cli_config("sh -c 'exec sleep 30'", timeout=1)

    with pytest.raises(CliAgentTimeoutError):
        await _run(cfg, "hi", tmp_path, session_key="tui:s1", reported_by_caller=True)
    (run,) = remote_runs.pending()
    assert (run.kind, run.task_id) == ("cli", "t1"), "the record outlives the failure, for the manager to forget"

    with pytest.raises(CliAgentTimeoutError):
        await _run(cfg, "hi", tmp_path, task_id="t2", session_key="tui:s1")
    assert [r.task_id for r in remote_runs.pending()] == ["t1"], (
        "a caller that takes the failure as its own leaves none"
    )


async def test_a_cancel_ends_the_agent_s_whole_process_tree_on_the_machine(box, tmp_path):
    cfg = cli_config("sh -c 'echo $$ > pid.txt; exec sleep 30'")
    pid_file = box.home / "raven-work" / "stopped" / "pid.txt"
    turn = asyncio.ensure_future(_run(cfg, "hi", tmp_path, instance="stopped"))
    await _until(pid_file.exists)
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not _gone(pid)

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    await _until(lambda: _gone(pid))


async def test_a_cancel_while_the_launch_answer_waits_ends_the_agent_too(box, tmp_path, monkeypatch):
    """The turn is cancelled after the daemon started its agent but before ``agent/launch``'s answer
    reaches :meth:`NodePool.open_channel`: the agent is ended and the channel dropped all the same."""
    answered = asyncio.Event()
    _hold_launch_answers(monkeypatch, answered)
    cfg = cli_config("sh -c 'echo $$ > pid.txt; exec sleep 30'")
    pid_file = box.home / "raven-work" / "held" / "pid.txt"
    turn = asyncio.ensure_future(_run(cfg, "hi", tmp_path, instance="held"))
    await _until(pid_file.exists)
    await asyncio.wait_for(answered.wait(), 10)
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not _gone(pid)

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    await _until(lambda: _gone(pid))
    await _until(lambda: box.nodes._nodes["box"].channels == {})


async def test_a_cancel_while_the_daemon_still_prepares_the_launch_ends_the_agent_too(box, tmp_path, monkeypatch):
    """The daemon registers the channel only once it has the login environment and the agent: a cancel
    sent before that is answered ``no agent on channel``, and the agent starts all the same."""
    from raven.acp_client.client import AcpClient
    from raven.node import protocol
    from tests.test_subagent_acp_remote import _FAKE_SSH

    if not Path("/bin/bash").exists():
        pytest.skip("the daemon captures a login environment from bash or zsh only")
    # A login shell that takes a second, which the daemon runs before its first agent starts.
    stand_in = _FAKE_SSH.format(python=sys.executable, host=HOST, port=PORT)
    assert stand_in.count('"SHELL": "/bin/sh"') == 1
    (box.bin / "ssh").write_text(stand_in.replace('"SHELL": "/bin/sh"', '"SHELL": "/bin/bash"'), encoding="utf-8")
    (box.home / ".bash_profile").write_text("sleep 1\n", encoding="utf-8")
    request = AcpClient.request
    sent = asyncio.Event()

    async def watched(self: AcpClient, method: str, *args: Any, **kwargs: Any) -> Any:
        if method == protocol.AGENT_LAUNCH:
            sent.set()
        return await request(self, method, *args, **kwargs)

    monkeypatch.setattr(AcpClient, "request", watched)
    turn = asyncio.ensure_future(_run(cli_config("sh -c 'exec sleep 30'"), "hi", tmp_path, instance="early"))
    await asyncio.wait_for(sent.wait(), 15)
    await asyncio.sleep(0.3)  # the launch is on the daemon, inside that login shell

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    tasks = box.home / ".raven-node" / "tasks"
    await _until(lambda: any(tasks.glob("*/task.json")))
    await _until(lambda: not box.agents_left(), timeout=5)


async def test_a_stateful_cli_agent_resumes_its_session_on_the_machine(box, tmp_path):
    cfg = cli_config("sh -c 'echo created {agent_id}'", resume_command="sh -c 'echo resumed {agent_id}'")
    registry = InstanceRegistry(path=tmp_path / "instances.json")

    first, _ = _split(await _run(cfg, "hi", tmp_path, registry=registry, instance="r1", task_id="t1"))
    second, _ = _split(await _run(cfg, "again", tmp_path, registry=registry, instance="r1", task_id="t2"))

    assert first.startswith("created ") and second == f"resumed {first.removeprefix('created ')}"
    assert (box.home / "raven-work" / "r1").is_dir(), "one directory per handle, both turns in it"


async def test_the_files_that_came_with_the_task_are_named_by_address_and_not_copied(box, tmp_path):
    data = tmp_path / "sales.csv"
    data.write_text("day,amount\n1,3\n", encoding="utf-8")
    media = (Media(path=str(data), mime="text/csv", kind="file"),)

    head, _ = _split(await _run(cli_config("cat"), "count the rows", tmp_path, media=media, instance="counter"))

    assert head.startswith("count the rows\n\nFiles that came with this task, by their address on the computer")
    assert "Nothing was copied here" in head and f"- {data}" in head
    assert "[attachments, by absolute path]" not in head, "not the local note, whose paths mean nothing there"
    assert not (box.home / "raven-work" / "counter" / "sales.csv").exists()


async def test_the_command_is_split_the_way_the_machine_s_shell_reads_it_whatever_this_computer_s_is(
    box, tmp_path, monkeypatch
):
    """From a Windows host, ``sh -c 'echo one; cat'`` reaches the machine as the three words its shell reads."""
    from raven.utils import commands

    cfg = cli_config("sh -c 'echo one; cat'")
    # This computer as Windows, for the splitter alone: ``command_argv`` reads ``os.name`` from its own module.
    monkeypatch.setattr(commands, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))

    head, _ = _split(await _run(cfg, "two", tmp_path))

    assert head.strip() == "one\ntwo"


async def test_mcp_servers_are_not_lent_to_a_cli_agent_on_a_machine_and_the_reply_says_why(box, tmp_path):
    cfg = cli_config("cat {mcp_file}", mcps=["pg"])
    grant = McpGrant(granted=(GrantedServer("pg", "stdio", MCPServerConfig(command="/bin/true")),))

    reply = await _run(cfg, "hi", tmp_path, mcp_grant=grant)

    head, notes = _split(reply)
    assert json.loads(head) == {"tools": {"mcpServers": {}, "disabledTools": []}}, "a file there, naming no server"
    assert "MCP servers ['pg'] were not lent to cli agent 'where@box': it runs on machine 'box'" in notes
    assert not list(tmp_path.glob("raven_subagent_*")), "no MCP file is made on this computer"


async def test_this_computer_s_files_are_not_accounted_for_a_remote_run(box, tmp_path, monkeypatch):
    from raven.agent.subagent.backends import cli_agent

    looked: list[str] = []
    monkeypatch.setattr(cli_agent, "take_snapshot", lambda cwd: looked.append(cwd) or {})

    await _run(cli_config("cat"), "hi", tmp_path)

    assert looked == [], "no snapshot of a directory here: the run's files are on the machine"


async def test_an_unreachable_machine_fails_a_cli_dispatch_in_one_sentence(box, tmp_path):
    box.mode("unreachable")

    with pytest.raises(RuntimeError) as caught:
        await _run(cli_config("cat"), "hi", tmp_path)

    said = str(caught.value)
    assert said.startswith("CLI agent 'where@box': ")
    assert "machine 'Lab box' (box) did not answer within the connection timeout" in said
    assert HOST not in said and str(PORT) not in said


async def test_an_unregistered_machine_fails_a_cli_dispatch_before_anything_starts(box, tmp_path):
    with pytest.raises(RuntimeError, match="machine 'elsewhere' is not registered"):
        await _run(cli_config("cat", machine="elsewhere"), "hi", tmp_path)
    assert box.calls() == []


async def test_a_command_not_on_the_machine_and_a_prompt_file_it_cannot_write_each_say_so(box, tmp_path):
    with pytest.raises(RuntimeError, match="command was not found on machine 'Lab box'"):
        await _run(cli_config("no-such-agent-for-raven-tests {prompt}"), "hi", tmp_path)

    # The run's `.raven/` is a file there: the prompt file cannot be made under it.
    blocked = box.home / "raven-work" / "blocked"
    blocked.mkdir(parents=True)
    (blocked / ".raven").write_text("not a directory", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"prompt-t1\.txt could not be written on machine 'Lab box'"):
        await _run(cli_config("cat {prompt_file}"), "hi", tmp_path, instance="blocked")


async def test_a_process_that_reaches_no_machine_says_so_before_anything_starts(box, tmp_path, monkeypatch):
    from raven.ops import remote_path

    monkeypatch.setattr(remote_path, "_NODES", None)

    with pytest.raises(RuntimeError, match="this process does not reach registered machines"):
        await _run(cli_config("cat"), "hi", tmp_path)
    assert box.calls() == []


# ---- checks --------------------------------------------------------------------------


async def test_the_free_probe_of_a_cli_row_does_not_reach_the_machine_and_a_test_runs_there(box):
    cfg = cli_config("sh -c 'echo PONG'")

    probed = await probe.probe_one(cfg, source="config")

    assert probed.status == "attention" and probed.target == "box"
    assert "runs on machine 'Lab box' (box)" in probed.detail and "run a test" in probed.detail
    assert box.calls() == [], "no ssh for a listing"

    tested = await probe.run_test(cfg, source="config")

    assert tested.ok is True and tested.detail == "the agent ran and replied" and tested.remedy is None
    assert len(box.calls()) == 1, "the test went to the machine, over the node's one ssh"


async def test_a_ping_of_a_cli_row_answers_from_the_machine_and_leaves_one_directory(box):
    pinged = await probe.ping_agent(cli_config("cat"))

    assert pinged.ok is True and pinged.detail == "it ran and replied"
    assert sorted(p.name for p in (box.home / "raven-work").iterdir()) == ["raven-check"]


async def test_a_failed_ping_of_a_cli_row_says_the_fix_is_made_on_the_machine(box):
    pinged = await probe.ping_agent(cli_config("sh -c 'echo not signed in >&2; exit 1'"))

    assert pinged.ok is False
    assert "exited 1: not signed in" in pinged.detail and pinged.detail.endswith(
        "(on machine 'box': any fix is made there)"
    )
    assert pinged.remedy is None, "nothing offered to run here"


# ---- an openai agent reached from the machine -------------------------------------------


async def _chat_completions_stub(host: str = "127.0.0.1") -> tuple[Any, int, list[tuple[list[str], dict[str, Any]]]]:
    """An endpoint on this computer's loopback, at ``host``: the far end of the stand-in tunnel."""
    seen: list[tuple[list[str], dict[str, Any]]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("utf-8", "replace").split("\r\n")
            length = next((int(h.split(":", 1)[1]) for h in lines if h.lower().startswith("content-length:")), 0)
            body = await reader.readexactly(length) if length else b""
        except (asyncio.IncompleteReadError, ConnectionError):
            # The tunnel's own readiness check connects and leaves.
            writer.close()
            return
        seen.append((lines, json.loads(body) if body else {}))
        answer = {"choices": [{"message": {"content": "pong through the tunnel"}}], "usage": {"total_tokens": 3}}
        payload = json.dumps(answer).encode("utf-8")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
            % len(payload)
            + payload
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, host, 0)
    return server, server.sockets[0].getsockname()[1], seen


async def test_an_openai_row_on_a_machine_is_reached_through_a_tunnel_from_it(forwarding, tmp_path, monkeypatch):
    server, port, seen = await _chat_completions_stub()
    # This computer's proxy is not where a port on its own loopback is.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    try:
        reply = await _run(http_config(f"http://localhost:{port}/v1"), "hi", tmp_path)
    finally:
        server.close()
        await server.wait_closed()

    assert reply == "pong through the tunnel"
    (headers, body) = seen[-1]
    assert headers[0] == "POST /v1/chat/completions HTTP/1.1"
    assert any(h.lower() == f"host: localhost:{port}" for h in headers), "the name the server knows itself by"
    assert body["model"] == "m" and body["messages"][-1] == {"role": "user", "content": "hi"}
    (argv,) = forwarding.calls()
    assert "-N" in argv and "ExitOnForwardFailure=yes" in argv and "BatchMode=yes" in argv
    forward = argv[argv.index("-L") + 1]
    assert forward.startswith("127.0.0.1:") and forward.endswith(f":localhost:{port}")
    pid = int((forwarding.bin / "tunnel.pid").read_text(encoding="utf-8"))
    await _until(lambda: _gone(pid))  # the tunnel ends with the turn


async def test_the_files_that_came_with_an_openai_row_s_task_are_named_by_address_and_not_copied(forwarding, tmp_path):
    """The openai lane says of the files what the acp and cli lanes say: their addresses here, nothing carried."""
    server, port, seen = await _chat_completions_stub()
    data = tmp_path / "sales.csv"
    data.write_text("day,amount\n1,3\n", encoding="utf-8")
    media = (Media(path=str(data), mime="text/csv", kind="file"),)
    try:
        reply = await _run(http_config(f"http://localhost:{port}/v1"), "count the rows", tmp_path, media=media)
    finally:
        server.close()
        await server.wait_closed()

    assert reply == "pong through the tunnel"
    sent = seen[-1][1]["messages"][-1]["content"]
    assert sent.startswith("count the rows\n\nFiles that came with this task, by their address on the computer")
    assert "Nothing was copied here" in sent and f"- {data}" in sent
    assert "[attachments, by absolute path]" not in sent, "not the local note, whose paths mean nothing there"


async def test_an_ipv6_endpoint_is_forwarded_to_with_its_address_bracketed_as_ssh_reads_it(forwarding, tmp_path):
    """``http://[::1]:8000/v1`` asks for ``-L 127.0.0.1:<port>:[::1]:8000``; bare, ssh refuses the forward."""
    forwarding.mode("unreachable")

    with pytest.raises(RuntimeError, match="did not answer within the connection timeout"):
        await _run(http_config("http://[::1]:8000/v1"), "hi", tmp_path)

    (argv,) = forwarding.calls()
    forward = argv[argv.index("-L") + 1]
    assert forward.startswith("127.0.0.1:") and forward.endswith(":[::1]:8000"), forward


async def test_an_endpoint_on_the_machine_s_ipv6_loopback_answers_through_the_tunnel(forwarding, tmp_path):
    try:
        server, port, seen = await _chat_completions_stub(host="::1")
    except OSError:
        pytest.skip("this computer has no IPv6 loopback")
    try:
        reply = await _run(http_config(f"http://[::1]:{port}/v1"), "hi", tmp_path)
    finally:
        server.close()
        await server.wait_closed()

    assert reply == "pong through the tunnel"
    assert any(h.lower() == f"host: [::1]:{port}" for h in seen[-1][0]), "the name the server knows itself by"


async def test_the_free_probe_of_an_openai_row_asks_no_network_and_a_test_asks_through_the_tunnel(forwarding, tmp_path):
    server, port, seen = await _chat_completions_stub()
    cfg = http_config(f"http://localhost:{port}/v1", api_key="sk-raven-test-not-real")
    try:
        probed = await probe.probe_one(cfg, source="config")
        assert probed.status == "attention" and "runs on machine 'Lab box' (box)" in probed.detail
        assert "run a test" in probed.detail
        assert forwarding.calls() == [] and seen == [], "nothing is asked of the network for a listing"

        tested = await probe.run_test(cfg, source="config")
    finally:
        server.close()
        await server.wait_closed()

    assert tested.ok is True and tested.detail == "it ran and replied"
    assert [argv for argv in forwarding.calls() if "-L" in argv], "the test went through the tunnel"
    assert any("Bearer sk-raven-test-not-real" in h for h in seen[-1][0]), "the row's own key, through it"


async def test_a_tunnel_that_does_not_open_fails_the_turn_in_one_sentence_without_the_address(forwarding, tmp_path):
    forwarding.mode("unreachable")

    with pytest.raises(RuntimeError) as caught:
        await _run(http_config("http://localhost:1/v1"), "hi", tmp_path)

    said = str(caught.value)
    assert said.startswith("OpenAI-API agent 'llm@box': ")
    assert "machine 'Lab box' (box) did not answer within the connection timeout" in said
    assert HOST not in said and str(PORT) not in said


async def test_an_endpoint_the_machine_cannot_reach_fails_the_turn_in_one_sentence(forwarding, tmp_path):
    # The stand-in forwards, and the far port answers nobody: what the tunnel carries back is a closed connection.
    with pytest.raises(RuntimeError, match="did not answer through the tunnel from machine 'Lab box'"):
        await _run(http_config("http://localhost:1/v1"), "hi", tmp_path)


async def test_a_tunnel_that_never_opens_is_given_up_on_in_one_sentence(forwarding, tmp_path, monkeypatch):
    from raven.acp_client import remote

    forwarding.mode("silent")
    monkeypatch.setattr(remote, "TUNNEL_READY_S", 2.0)
    target = remote.machine("box")

    with pytest.raises(remote.RemoteMachineError, match="machine 'Lab box' \\(box\\) did not open the tunnel within"):
        async with remote.tunnel(target, "http://localhost:1/v1", ssh_log=tmp_path / "t.log", connect_timeout=0):
            pass
    await _until(lambda: (forwarding.bin / "tunnel.pid").exists())  # a loaded machine starts the stand-in late
    pid = int((forwarding.bin / "tunnel.pid").read_text(encoding="utf-8"))
    await _until(lambda: _gone(pid))  # the ssh that never opened it is ended


async def test_a_port_taken_after_it_was_chosen_is_given_up_for_another_before_any_ssh(
    forwarding, tmp_path, monkeypatch
):
    """A listener that took the port would answer the readiness probe in ssh's place and get the turn's request:
    the port is checked again just before ssh starts, and a taken one costs no ssh."""
    import socket

    from raven.acp_client import remote

    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen()
    squatter.setblocking(False)
    taken = squatter.getsockname()[1]
    chosen = iter([taken, remote._free_port()])
    monkeypatch.setattr(remote, "_free_port", lambda: next(chosen))
    target = remote.machine("box")
    try:
        async with remote.tunnel(target, "http://localhost:1/v1", ssh_log=tmp_path / "t.log", connect_timeout=0) as fwd:
            assert fwd.url.startswith("http://127.0.0.1:") and not fwd.url.startswith(f"http://127.0.0.1:{taken}/")
            with pytest.raises(BlockingIOError):
                squatter.accept()
    finally:
        squatter.close()
    forwards = [argv[argv.index("-L") + 1] for argv in forwarding.calls() if "-L" in argv]
    assert len(forwards) == 1 and not forwards[0].startswith(f"127.0.0.1:{taken}:"), "one ssh, on the other port"


async def test_a_port_ssh_lost_the_race_for_is_given_up_for_another(forwarding, tmp_path):
    from raven.acp_client import remote

    forwarding.mode("port_taken_once")
    target = remote.machine("box")

    async with remote.tunnel(target, "http://localhost:1/v1", ssh_log=tmp_path / "t.log", connect_timeout=0) as fwd:
        assert fwd.url.startswith("http://127.0.0.1:")

    forwards = [argv[argv.index("-L") + 1] for argv in forwarding.calls() if "-L" in argv]
    assert len(forwards) == 2 and forwards[0] != forwards[1], "the second ssh listened on another port"


async def test_ports_taken_every_time_end_the_turn_in_one_sentence_about_this_computer(forwarding, tmp_path):
    from raven.acp_client import remote

    forwarding.mode("port_taken")
    target = remote.machine("box")

    with pytest.raises(
        remote.RemoteMachineError, match="a port on this computer for the tunnel was taken each of the 3"
    ) as caught:
        async with remote.tunnel(target, "http://localhost:1/v1", ssh_log=tmp_path / "t.log", connect_timeout=0):
            pass

    assert HOST not in str(caught.value) and str(PORT) not in str(caught.value)
    assert len([argv for argv in forwarding.calls() if "-L" in argv]) == 3


async def test_a_tunnel_names_what_it_cannot_do_before_any_ssh(box, tmp_path, monkeypatch):
    from raven.acp_client import remote

    target = remote.machine("box")
    with pytest.raises(remote.RemoteMachineError, match="names no host"):
        async with remote.tunnel(target, "http:///v1", ssh_log=tmp_path / "t.log"):
            pass

    async def no_ssh(*argv, **kw):
        raise OSError("no ssh here")

    monkeypatch.setattr(remote.asyncio, "create_subprocess_exec", no_ssh)
    with pytest.raises(remote.RemoteMachineError, match="ssh could not be started: no ssh here"):
        async with remote.tunnel(target, "http://localhost:1/v1", ssh_log=tmp_path / "t.log"):
            pass
    with pytest.raises(RuntimeError, match="machine 'elsewhere' is not registered"):
        await _run(http_config("http://localhost:1/v1").model_copy(update={"machine": "elsewhere"}), "hi", tmp_path)
    assert box.calls() == []


async def test_an_https_endpoint_on_a_machine_is_warned_on_load_and_refused_before_any_ssh(box, tmp_path, logs):
    cfg = http_config("https://llm.lab.internal/v1")

    assert any("carries http only" in line and "'llm@box'" in line for line in logs)
    with pytest.raises(RuntimeError, match="has to be http"):
        await _run(cfg, "hi", tmp_path)
    assert box.calls() == []


# ---- the roster, the table and the schema, for every kind ----------------------------------


def test_the_roster_names_the_machine_for_every_kind_and_hands_it_no_paths():
    cli = cli_config("cat")
    http = http_config("http://llm.lab.internal:8000/v1")

    for meta in (agent_meta(cli), agent_meta(http)):
        assert meta.machine == "box" and meta.reads_local_files is False
    listing = format_agent_listing([agent_meta(cli), agent_meta(http)])
    assert listing.count("on machine box") == 2 and HOST not in listing
    assert _row_for(cli).caps.machine == "box" and _row_for(http).caps.machine == "box"


def test_a_built_in_row_cannot_name_a_machine_and_is_told_so(logs):
    cfg = BuiltinAgentConfig(name="research-raven", machine="box")

    assert cfg.machine is None and agent_meta(cfg).machine == ""
    said = next(line for line in logs if "'research-raven'" in line)
    assert "cannot run on" in said and "Raven's own loop runs in this process" in said and "remove `machine`" in said
    assert BuiltinAgentConfig(name="research-raven").machine is None


def test_a_cli_row_s_machine_is_an_id_or_nothing():
    assert ThirdPartyCliSubagentConfig(name="x", command="cat", machine="  box ").machine == "box"
    assert ThirdPartyCliSubagentConfig(name="x", command="cat", machine="  ").machine is None
    assert ThirdPartyCliSubagentConfig(name="x", command="cat").remote_cwd is None


def test_an_openai_row_drops_a_remote_cwd_with_a_warning_and_the_write_door_refuses_it(logs):
    row = {
        "name": "llm",
        "kind": "openai",
        "baseUrl": "http://x/v1",
        "model": "m",
        "machine": "box",
        "remoteCwd": "~/w",
    }

    loaded = ThirdPartyOpenAISubagentConfig.model_validate(row)

    assert loaded.machine == "box" and not hasattr(loaded, "remote_cwd")
    assert any("remoteCwd is not supported for kind 'openai'" in line for line in logs)
    with pytest.raises(ValueError, match="remoteCwd is not supported for kind 'openai'"):
        reject_unsupported_openai_fields([row])


def test_machine_and_local_cli_rows_are_on_the_table_without_a_debug_switch(box, monkeypatch):
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES", raising=False)
    registry = AgentRegistry(build_builtin=lambda row, narrowed: ("backend", row.name))
    local = ThirdPartyCliSubagentConfig(name="here", command="cat")
    registry.apply([cli_config("cat"), local])
    assert "where@box" in registry.names() and "here" in registry.names()
    assert box.calls() == []


async def test_a_cli_dispatch_needs_no_debug_switch(box, monkeypatch, tmp_path):
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES", raising=False)
    reply = await _run(cli_config("cat"), "hi", tmp_path)
    assert reply.startswith("hi") and "Ran on machine" in reply


async def test_the_announce_names_a_cli_row_s_directory_on_the_machine(box):
    from raven.agent.subagent.manager import SubagentManager

    class _Provider:
        def get_default_model(self) -> str:
            return "m"

    mgr = SubagentManager(provider=_Provider(), workspace=Path("/tmp"), agents=[cli_config("cat")])

    assert mgr._machine_workdir("where@box", "reviewer") == ("box", "~/raven-work/reviewer")


async def test_a_cli_run_left_on_the_machine_when_this_computer_stops_is_picked_up_by_the_next_start(
    box, tmp_path, monkeypatch
):
    """The command goes on under the machine's daemon when this process stops; the next one reads its output and reports it."""
    from raven.acp_client import remote, remote_runs
    from raven.agent.subagent import resume_remote
    from raven.agent.subagent.history import SpawnRecord, nodes_root
    from tests.test_subagent_acp_remote import _Manager, _Registry

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    backend = build_third_party_backend(cli_config("sh -c 'echo $$ > pid.txt; sleep 2; echo done; cat'"))
    turn = asyncio.ensure_future(
        backend.run(
            "hi",
            task_id="t1",
            workspace=tmp_path,
            executor=None,
            session_key="tui:s1",
            instance="left",
            authored_task="say done {{ ref:notes.md }}",
        )
    )
    pid_file = box.home / "raven-work" / "left" / "pid.txt"
    await _until(pid_file.exists)
    await _until(lambda: remote_runs.pending() != [])
    pid = int(pid_file.read_text(encoding="utf-8"))
    (run,) = remote_runs.pending()
    assert (run.kind, run.machine, run.agent, run.session_key, run.handle, run.task_id) == (
        "cli",
        "box",
        "where@box",
        "tui:s1",
        "left",
        "t1",
    )
    assert run.authored_task == "say done {{ ref:notes.md }}" and run.stateful is False

    # This computer stops: the turn is cancelled the way the shutdown does it, then the links are closed.
    remote.leave_agents_running()
    try:
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        assert remote_runs.was_left("t1"), "the manager would read this dispatch as left, not cancelled"
        await box.nodes.close_all()
    finally:
        remote.staying()
    assert not _gone(pid), "the command stays with the machine's daemon"
    assert [kept.task for kept in remote_runs.pending()] == [run.task]

    # The next start: the dispatch's record is still open under its session, as the manager leaves one.
    session_dir = tmp_path / "sessions" / "s1"
    meta = {
        "call_id": "n1",
        "session_key": "tui:s1",
        "agent": backend.name,
        "handle": "left",
        "task_summary": "say done",
    }
    SpawnRecord.open(session_dir, task_id="t1", task="hi", meta=meta, node_id="n1")
    manager = _Manager(backend, session_dir)
    registry = _Registry()
    monkeypatch.setattr(resume_remote, "get_registry", lambda: registry)

    assert await asyncio.wait_for(resume_remote.resume_remote_runs(manager), timeout=30) == 1

    (said,) = manager.announced
    assert said["status"] == "ok" and said["task_id"] == "t1" and said["origin"]["chat_id"] == "s1"
    assert said["result"].split("\n\n[raven]")[0] == "done\nhi", "the command's whole output, read after the restart"
    assert (
        "picked up after this computer restarted" in said["result"]
        and "Ran on machine 'Lab box' (box)" in said["result"]
    )
    meta_back = json.loads((nodes_root(session_dir) / "n1.meta.json").read_text(encoding="utf-8"))
    assert meta_back["status"] == "completed" and manager.finalized == ["n1"]
    assert registry.rows == [("status", "tui:s1", "where@box", "left", "completed")]
    assert remote_runs.pending() == []
    await _until(lambda: _gone(pid))


async def test_a_cancel_while_this_computer_goes_on_ends_the_command_and_keeps_no_record(box, tmp_path, monkeypatch):
    from raven.acp_client import remote_runs

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    turn = asyncio.ensure_future(
        _run(cli_config("sh -c 'echo $$ > pid.txt; exec sleep 30'"), "hi", tmp_path, instance="gone")
    )
    pid_file = box.home / "raven-work" / "gone" / "pid.txt"
    await _until(pid_file.exists)
    await _until(lambda: remote_runs.pending() != [])
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert [run.kind for run in remote_runs.pending()] == ["cli"], "recorded while it runs"

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    await _until(lambda: _gone(pid))
    assert remote_runs.pending() == [] and not remote_runs.was_left("t1")


async def test_a_command_that_ends_here_leaves_no_record(box, tmp_path, monkeypatch):
    from raven.acp_client import remote_runs

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))

    head, _ = _split(await _run(cli_config("cat"), "hi", tmp_path))

    assert head == "hi" and remote_runs.pending() == []


async def test_a_reply_the_caller_reports_keeps_the_command_s_record_and_a_host_that_stops_before_the_announce_reads_it_again(
    box, tmp_path, monkeypatch
):
    """The manager announces the output after the backend returns it: the record stays until then, and the next start reads the whole output again."""
    from raven.acp_client import remote_runs
    from raven.agent.subagent import resume_remote
    from raven.agent.subagent.history import SpawnRecord
    from tests.test_subagent_acp_remote import _Manager, _Registry

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    backend = build_third_party_backend(cli_config("sh -c 'echo done; cat'"))

    head, _ = _split(
        await backend.run(
            "hi",
            task_id="t1",
            workspace=tmp_path,
            executor=None,
            session_key="tui:s1",
            instance="kept",
            reported_by_caller=True,
        )
    )

    assert head == "done\nhi"
    (run,) = remote_runs.pending()
    assert (run.kind, run.task_id, run.received) == ("cli", "t1", 0), (
        "the record outlives the output, for the manager to forget"
    )

    # This computer stops between the output and its announce.
    await box.nodes.close_all()
    session_dir = tmp_path / "sessions" / "s1"
    meta = {
        "call_id": "n1",
        "session_key": "tui:s1",
        "agent": backend.name,
        "handle": "kept",
        "task_summary": "say done",
    }
    SpawnRecord.open(session_dir, task_id="t1", task="hi", meta=meta, node_id="n1")
    manager = _Manager(backend, session_dir)
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())

    assert await asyncio.wait_for(resume_remote.resume_remote_runs(manager), timeout=30) == 1

    (said,) = manager.announced
    assert said["status"] == "ok" and said["result"].split("\n\n[raven]")[0] == "done\nhi", (
        "the whole output, read again"
    )
    assert remote_runs.pending() == []


class _PickUpLink:
    """A node pool with one task, whose attach hands over ``endpoint``: what a pick-up reads a left run off."""

    def __init__(self, endpoint: Any, *, rows: Any = None, refuse: Exception | None = None) -> None:
        self.endpoint = endpoint
        self.rows = rows if rows is not None else [{"task": "where@box-1", "state": "running"}]
        self.refuse = refuse
        self.attached: list[tuple[str, str, int]] = []

    async def tasks(self, machine_id: str) -> list[dict[str, Any]]:
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows

    async def attach(self, machine_id: str, task: str, *, name: str, after: int = 0) -> tuple[Any, dict[str, Any]]:
        if self.refuse is not None:
            raise self.refuse
        self.attached.append((machine_id, task, after))
        return self.endpoint, {"pid": 7, "frames": 2, "replayed": 2, "rc": None}


def _left_run(**kw: Any) -> Any:
    from raven.acp_client import remote_runs

    fields: dict[str, Any] = dict(
        kind="cli",
        machine="box",
        task="where@box-1",
        agent="where@box",
        session_key="tui:s1",
        handle="t1",
        task_id="t1",
        session_id="",
        prompt_id=0,
        next_id=0,
        initialize=None,
        launch_key="",
        config_key="",
        session_cwd="/home/worker/raven-work/t1",
        stateful=False,
    )
    fields.update(kw)
    return remote_runs.RemoteRun(**fields)


def _left_endpoint(*lines: str, rc: int | None = 0, lost: str | None = None) -> Any:
    """The channel a pick-up is handed, already fed what the daemon replays: ``lines``, then the exit or the loss."""
    from raven.acp_client import remote
    from raven.node.client import ChannelEndpoint

    endpoint = ChannelEndpoint(_Link(), 1, target=remote.machine("box"), agent="where@box", task="where@box-1")  # type: ignore[arg-type]
    for n, line in enumerate(lines, 1):
        endpoint.feed_frame(line, n)
    if lost is not None:
        endpoint.lost(lost)
    elif rc is not None:
        endpoint.exited(rc)
    return endpoint


async def test_a_left_cli_run_is_read_whole_off_the_daemon_and_its_exit_ends_it(box):
    """The pick-up replays every line from the first, the exit code ends it, and the reply closes with the machine note."""
    from raven.acp_client import remote_resume

    link = _PickUpLink(_left_endpoint("done", "hi", rc=0))

    picked = await build_third_party_backend(cli_config("cat")).pick_up(_left_run(), pool=link)

    assert picked.status == "ok" and picked.reply.split("\n\n[raven]")[0] == "done\nhi"
    assert link.attached == [("box", "where@box-1", 0)], (
        "from the first line: a cli run's reply is the whole of its output"
    )
    assert remote_resume.PICKED_UP_NOTE in picked.reply and f"scp://worker@{HOST}:{PORT}//home/worker" in picked.reply


def _derived_config(**kw: Any) -> ThirdPartyCliSubagentConfig:
    """A cli entry whose session id is read off the create's output; stateful unless ``resume_command`` is cleared."""
    kw.setdefault("resume_command", "sh -c 'cat' {agent_id}")
    if kw["resume_command"] is None:
        del kw["resume_command"]
    return cli_config("sh -c 'cat'", id_source="derived", session_id_pattern=r"session (\S+)", **kw)


class _Recorder:
    """A registry stand-in: what a pick-up bound, looked up, and what it found."""

    def __init__(self, bound: str | None = None) -> None:
        self.bound = bound
        self.rows: list[tuple[str, ...]] = []
        self.lookups = 0

    async def lookup(self, session_key: str, agent: str, handle: str, *, kind: str = "cli") -> str | None:
        self.lookups += 1
        return self.bound

    async def bind_if_absent(
        self, session_key: str, agent: str, handle: str, agent_id: str, *, kind: str = "cli"
    ) -> str:
        if self.bound is not None:
            return self.bound
        self.rows.append((session_key, agent, handle, agent_id))
        self.bound = agent_id
        return agent_id

    async def commit(self, session_key: str, agent: str, handle: str, agent_id: str, *, kind: str = "cli") -> None:
        raise AssertionError("a pick-up binds where nothing is bound; it never commits over a binding")


async def test_a_picked_up_create_binds_the_instance_to_the_session_id_its_output_names(box):
    """The dispatch's deferred commit never ran (this computer stopped first): the pick-up reads the id the way the
    dispatch would have and binds the handle, so the next turn to it resumes this session."""
    backend = build_third_party_backend(_derived_config())
    registry = _Recorder()
    backend._registry = registry  # type: ignore[attr-defined]

    picked = await backend.pick_up(
        _left_run(handle="t1", session_id="", stateful=True),
        pool=_PickUpLink(_left_endpoint("session abc123", "done", rc=0)),
    )
    assert picked.status == "ok" and registry.rows == [("tui:s1", "where@box", "t1", "abc123")]
    assert "not resumable" not in picked.reply

    registry = _Recorder()
    backend._registry = registry  # type: ignore[attr-defined]
    picked = await backend.pick_up(
        _left_run(handle="t1", session_id="", stateful=True), pool=_PickUpLink(_left_endpoint("done", rc=0))
    )
    assert picked.status == "ok" and registry.rows == [] and "not resumable" in picked.reply, (
        "no id in the output: said so"
    )

    picked = await backend.pick_up(
        _left_run(handle="t1", session_id="bound-before", stateful=True),
        pool=_PickUpLink(_left_endpoint("session zzz", "done", rc=0)),
    )
    assert picked.status == "ok" and registry.rows == [], "a run that resumed a bound session binds nothing"


async def test_a_picked_up_stateless_run_binds_nothing_and_warns_of_nothing_as_the_dispatch_path_does(box):
    """A stateless entry may declare ``idSource: derived``; the dispatch reads no id from it and binds nothing, and
    the record's ``stateful`` is what tells the pick-up so -- the handle cannot, every run carries one."""
    backend = build_third_party_backend(_derived_config(resume_command=None))
    assert backend.is_stateful is False
    registry = _Recorder()
    backend._registry = registry  # type: ignore[attr-defined]

    with_id = await backend.pick_up(
        _left_run(handle="t1", session_id="", stateful=False),
        pool=_PickUpLink(_left_endpoint("session abc123", "done", rc=0)),
    )
    without = await backend.pick_up(
        _left_run(handle="t1", session_id="", stateful=False), pool=_PickUpLink(_left_endpoint("done", rc=0))
    )

    assert with_id.status == "ok" and without.status == "ok"
    assert registry.rows == [] and registry.lookups == 0, "nothing bound, nothing looked up"
    assert "not resumable" not in with_id.reply and "not resumable" not in without.reply


async def test_a_binding_a_dispatch_made_while_the_pick_up_waited_is_kept(box):
    """A session the newer turn bound while this pick-up waited on the machine is the instance's thread now, and the
    picked-up create's id does not replace it; with no id in the output there is no warning either."""
    backend = build_third_party_backend(_derived_config())
    registry = _Recorder(bound="newer-turn")
    backend._registry = registry  # type: ignore[attr-defined]

    picked = await backend.pick_up(
        _left_run(handle="t1", session_id="", stateful=True),
        pool=_PickUpLink(_left_endpoint("session first", "done", rc=0)),
    )
    assert picked.status == "ok" and registry.rows == [] and registry.bound == "newer-turn"
    assert "not resumable" not in picked.reply

    picked = await backend.pick_up(
        _left_run(handle="t1", session_id="", stateful=True), pool=_PickUpLink(_left_endpoint("done", rc=0))
    )
    assert picked.status == "ok" and "not resumable" not in picked.reply, "resumable, through the newer binding"


async def test_the_pick_up_does_not_wait_for_a_dispatch_holding_the_handle_and_its_binding_still_wins(box, tmp_path):
    """The interleaving itself, with the real registry: the dispatch holds the handle through its create and
    commit while the picked-up command ends; the pick-up binds at once -- it waits for no turn, and no run picked
    up after it does -- and the dispatch's commit, which comes last, is what the handle ends on."""
    from raven.agent.subagent.instances import hold_handle

    backend = build_third_party_backend(_derived_config())
    registry = InstanceRegistry(path=tmp_path / "instances.json")
    backend._registry = registry  # type: ignore[attr-defined]
    endpoint = _left_endpoint("session first", rc=None)  # the command on the machine has not ended
    picking = asyncio.create_task(
        backend.pick_up(_left_run(handle="t1", session_id="", stateful=True), pool=_PickUpLink(endpoint))
    )
    await asyncio.sleep(0.05)
    assert not picking.done(), "waiting on the machine's command"

    async with hold_handle("tui:s1", "where@box", "t1"):
        # A dispatch to the same instance: looks up, finds nothing, creates -- and holds the handle throughout.
        assert await registry.lookup("tui:s1", "where@box", "t1") is None
        endpoint.feed_frame("done", 2)
        endpoint.exited(0)  # the picked-up command ends while the dispatch still holds the handle
        picked = await asyncio.wait_for(picking, timeout=5)
        assert picked.status == "ok", "the pick-up did not wait for the handle"
        assert await registry.lookup("tui:s1", "where@box", "t1") == "first", "bound where nothing was bound"
        await registry.commit("tui:s1", "where@box", "t1", "second")  # the dispatch's deferred commit

    assert await registry.lookup("tui:s1", "where@box", "t1") == "second", "the dispatch commits last and wins"


async def test_bind_if_absent_binds_once_and_reads_a_status_row_or_another_kind_as_absent(tmp_path):
    registry = InstanceRegistry(path=tmp_path / "instances.json")

    assert await registry.bind_if_absent("tui:s1", "a", "h", "first") == "first"
    assert await registry.bind_if_absent("tui:s1", "a", "h", "second") == "first", "bound already: the first stays"
    assert await registry.lookup("tui:s1", "a", "h") == "first"

    await registry.upsert_spawn("tui:s1", "a", "idle-one", "idle")  # a status row with no id
    assert await registry.bind_if_absent("tui:s1", "a", "idle-one", "x") == "x"
    assert await registry.lookup("tui:s1", "a", "idle-one") == "x"

    await registry.commit("tui:s1", "a", "other", "acp-sess", kind="acp")
    assert await registry.bind_if_absent("tui:s1", "a", "other", "cli-sess") == "cli-sess", "another kind: absent"
    assert await registry.lookup("tui:s1", "a", "other") == "cli-sess"


async def test_bind_if_absent_closes_a_row_bound_to_this_very_id_and_leaves_one_bound_to_another_alone(tmp_path):
    """A left turn that resumed the session its handle is bound to: the manager wrote ``running`` on the row when the
    turn began, and the pick-up's bind is what closes it -- as ``commit``'s rebuild did before -- or the instance
    reads as interrupted after a reply that was announced. A row bound to another turn's session is that turn's to
    close."""
    registry = InstanceRegistry(path=tmp_path / "instances.json")
    await registry.commit("tui:s1", "a", "own", "X")
    await registry.upsert_spawn("tui:s1", "a", "own", "running")
    await registry.commit("tui:s1", "a", "other", "X")
    await registry.upsert_spawn("tui:s1", "a", "other", "running")
    was = {key: dict(row) for key, row in registry._load().items()}

    assert await registry.bind_if_absent("tui:s1", "a", "own", "X") == "X"
    assert await registry.bind_if_absent("tui:s1", "a", "other", "Y") == "X"

    rows = registry._load()
    own, other = rows[("tui:s1", "a", "own")], rows[("tui:s1", "a", "other")]
    assert own["agentId"] == "X" and "status" not in own, "bound to this id: the row is this run's, and closed"
    assert own["createdAtMs"] == was[("tui:s1", "a", "own")]["createdAtMs"]
    assert other == was[("tui:s1", "a", "other")], "bound to another id: untouched, that turn closes it"


async def test_a_left_cli_run_that_exited_non_zero_is_an_error_with_its_one_line(box):
    picked = await build_third_party_backend(cli_config("cat")).pick_up(
        _left_run(), pool=_PickUpLink(_left_endpoint("token expired", rc=3))
    )

    assert picked.status == "error" and picked.reply == "CLI agent 'where@box' exited 3: token expired"


async def test_a_left_cli_run_the_machine_cannot_be_asked_about_waits_for_a_later_start(box, monkeypatch):
    from raven.acp_client import remote_resume
    from raven.ops import remote_path

    backend = build_third_party_backend(cli_config("cat"))
    monkeypatch.setattr(remote_path, "_NODES", None)
    with pytest.raises(remote_resume.UnreachableError, match="does not reach registered machines"):
        await backend.pick_up(_left_run())
    down = _PickUpLink(_left_endpoint(), rows=remote_path.NodeCallError("the link to machine 'Lab box' (box) is down"))
    with pytest.raises(remote_resume.UnreachableError, match="is down"):
        await backend.pick_up(_left_run(), pool=down)
    silent = _PickUpLink(
        _left_endpoint(), refuse=remote_path.NodeCallError("the Raven node did not answer agent/attach")
    )
    with pytest.raises(remote_resume.UnreachableError, match="did not answer"):
        await backend.pick_up(_left_run(), pool=silent)


async def test_a_left_cli_run_the_machine_no_longer_has_or_an_unregistered_machine_is_an_error_in_one_sentence(box):
    from raven.ops import remote_path

    backend = build_third_party_backend(cli_config("cat"))
    gone = _PickUpLink(_left_endpoint(), rows=[])
    picked = await backend.pick_up(_left_run(), pool=gone)
    assert picked.status == "error" and "no longer has the run of 'where@box'" in picked.reply and gone.attached == []

    refused = _PickUpLink(
        _left_endpoint(),
        refuse=remote_path.NodeCallError(
            "machine 'Lab box' (box) no longer runs 'where@box' (task where@box-1)", reason="unknown_task"
        ),
    )
    picked = await backend.pick_up(_left_run(), pool=refused)
    assert picked.status == "error" and "no longer runs" in picked.reply

    picked = await backend.pick_up(_left_run(machine="elsewhere"), pool=gone)
    assert picked.status == "error" and "is not registered" in picked.reply


async def test_a_left_cli_run_whose_link_goes_is_said_so_or_kept_while_this_computer_is_leaving(box):
    from raven.acp_client import remote, remote_resume

    backend = build_third_party_backend(cli_config("cat"))
    picked = await backend.pick_up(_left_run(), pool=_PickUpLink(_left_endpoint("partial", lost="the link went")))
    assert picked.status == "error" and "ended while this computer was away: the link went" in picked.reply

    remote.leave_agents_running()
    try:
        with pytest.raises(remote_resume.UnreachableError, match="stopped while the run of 'where@box'"):
            await backend.pick_up(
                _left_run(), pool=_PickUpLink(_left_endpoint("partial", lost="left running: this computer stopped"))
            )
    finally:
        remote.staying()


async def test_the_tunnel_s_helpers_read_nothing_from_a_missing_log_and_kill_an_ssh_that_ignores_terminate(tmp_path):
    from raven.acp_client import remote

    assert remote._log_since(tmp_path / "absent.log", 0) == ""
    assert remote._log_size(tmp_path / "absent.log") == 0

    class _Stubborn:
        """An ssh whose terminate changes nothing; only kill ends it."""

        def __init__(self) -> None:
            self.returncode: int | None = None
            self.signals: list[str] = []

        def terminate(self) -> None:
            self.signals.append("terminate")

        def kill(self) -> None:
            self.signals.append("kill")
            self.returncode = -9

        async def wait(self) -> int:
            while self.returncode is None:
                await asyncio.sleep(0.01)
            return self.returncode

    proc = _Stubborn()
    waits = iter([asyncio.TimeoutError(), None])

    async def wait_for(awaitable, timeout):  # noqa: ANN001 - the shape asyncio.wait_for has
        outcome = next(waits)
        if outcome is not None:
            awaitable.close()
            raise outcome
        return await awaitable

    original = remote.asyncio.wait_for
    remote.asyncio.wait_for = wait_for  # type: ignore[assignment]
    try:
        await remote._end_ssh(proc)
    finally:
        remote.asyncio.wait_for = original  # type: ignore[assignment]
    assert proc.signals == ["terminate", "kill"] and proc.returncode == -9
