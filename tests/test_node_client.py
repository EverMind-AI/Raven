"""raven.node.client: one live link per registered machine, and the agents it carries.

The launch tests need no second computer. A stand-in ``ssh`` on PATH does what
sshd does with the one remote-command argument it is sent -- hands it to the
user's shell, in that user's home -- so the real ssh line and the real node
run: the machine's node directory holds a ``bin/python`` that is this test
run's own interpreter. Only the network is missing. The stand-in also fails
the way ssh fails, writing its own address-bearing words to the log the ssh
line names (``-E``), which is how the tests show those words never reach a
message.

Every failure is one sentence that names the machine and what to do, never the
address it is reached at.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.acp_client import remote
from raven.acp_client.client import AcpClient
from raven.acp_client.protocol import AcpConnectionError, AcpRemoteError, AcpTimeoutError
from raven.agent.subagent.backends import env as backend_env
from raven.agent.tools.filesystem import ReadFileTool
from raven.node import bundle, protocol
from raven.node import client as node_client
from raven.node.client import DEFAULT_NODE_DIR, ChannelEndpoint, NodeError, NodePool, _Node

HOST = "203.0.113.7"
PORT = 58717

_FAKE_SSH = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "calls"), "a") as f:
    f.write(json.dumps(args) + "\\n")
mode = open(os.path.join(here, "mode")).read().strip()
home = open(os.path.join(here, "home")).read().strip()
if mode == "unreachable":
    said = "ssh: connect to host {host} port {port}: Operation timed out"
    log = args[args.index("-E") + 1] if "-E" in args else None
    if log:
        open(log, "a").write(said + "\\n")
    else:
        sys.stderr.write(said + "\\n")
    sys.exit(255)
expect = os.path.join(here, "password")
if os.path.exists(expect):
    # The machine takes a password: the way OpenSSH asks for one under SSH_ASKPASS_REQUIRE=force.
    import subprocess as sp
    log = args[args.index("-E") + 1] if "-E" in args else None
    def refuse(said):
        if log:
            open(log, "a").write(said + "\\n")
        else:
            sys.stderr.write(said + "\\n")
        sys.exit(255)
    if "-i" in args or os.environ.get("SSH_ASKPASS_REQUIRE") != "force" or not os.environ.get("SSH_ASKPASS"):
        refuse("worker@{host}: Permission denied (password).")
    given = sp.run([os.environ["SSH_ASKPASS"], "password:"], capture_output=True, text=True).stdout.rstrip("\\n")
    if given != open(expect).read().strip():
        refuse("worker@{host}: Permission denied (password).")
command = args[args.index("--") + 1] if "--" in args else args[-1]
os.chdir(home)
# A daemon these tests start leaves soon after they do; the relay reads the idle time from its environment.
os.execve("/bin/sh", ["/bin/sh", "-c", command], {{"HOME": home, "SHELL": "/bin/sh", "PATH": os.environ["PATH"], "RAVEN_NODE_IDLE_S": "2"}})
"""

# A node that misbehaves in one named way, for the handshake's refusals.
_STUB_NODE = """
import json, sys
mode, digest, proto = sys.argv[1], sys.argv[2], int(sys.argv[3])
if mode == "crash":
    sys.stderr.write("Traceback (most recent call last):\\n  File x\\nModuleNotFoundError: No module named 'loguru'\\n")
    sys.exit(1)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    if mode == "silent" or (mode == "hello-only" and request["method"] != "node/hello"):
        continue
    answer = {"jsonrpc": "2.0", "id": request["id"]}
    if mode == "refuse":
        answer["error"] = {"code": -32600, "message": "this node speaks protocol 1, not 2"}
    elif mode == "other-protocol":
        answer["result"] = {"protocol": proto + 1, "digest": digest}
    elif mode == "other-code":
        answer["result"] = {"protocol": proto, "digest": "000000000000"}
    elif mode == "not-an-object":
        answer["result"] = ["node/hello"]
    else:
        answer["result"] = {"protocol": proto, "digest": digest, "python": "3.12.0", "home": "", "rg": False}
    print(json.dumps(answer), flush=True)
"""

# An agent the node starts for the tests: answers every request with its
# params, says one thing on stderr, leaves on `quit` with the code asked for,
# and on `spawn` starts a child that outlives it unless its group is ended.
_ECHO_AGENT = """
import json, subprocess, sys
sys.stderr.write("agent: started\\n")
sys.stderr.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    method = request.get("method")
    if method == "quit":
        sys.exit(int(request["params"]["rc"]))
    if method == "spawn":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"pid": child.pid}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"echo": request.get("params")}}), flush=True)
"""


class Machine:
    """The stand-in machine: its home, its ssh, and what was asked of it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]] | None = None) -> None:
        self.root = root
        self.home = root / "box-home"
        self.home.mkdir()
        self.bin = root / "fakebin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text(_FAKE_SSH.format(python=sys.executable, host=HOST, port=PORT), encoding="utf-8")
        ssh.chmod(0o755)
        (self.bin / "home").write_text(str(self.home), encoding="utf-8")
        self.mode("ok")
        self.registry = root / "connections.json"
        self.rows(rows or [row()])
        monkeypatch.setenv("RAVEN_CONNECTIONS", str(self.registry))
        path = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
        monkeypatch.setattr(backend_env, "_LOGIN_ENV", {**os.environ, "PATH": path})
        monkeypatch.setattr("raven.config.paths.get_logs_dir", lambda: root / "logs")

    def rows(self, rows: list[dict[str, Any]]) -> None:
        self.registry.write_text(json.dumps({"connections": rows}), encoding="utf-8")

    def mode(self, mode: str) -> None:
        (self.bin / "mode").write_text(mode, encoding="utf-8")

    def password(self, expected: str) -> None:
        """The machine takes a password from now on, and only this one."""
        (self.bin / "password").write_text(expected, encoding="utf-8")

    def node(self, where: Path | None = None, *, stub: str | None = None) -> Path:
        """Put a node of this Raven's code under ``where`` (the default node dir)."""
        base = where or self.home / DEFAULT_NODE_DIR.removeprefix("~/")
        python = base / bundle.install_name() / "bin" / "python"
        python.parent.mkdir(parents=True)
        if stub is None:
            body = f'exec {sys.executable} "$@"\n'
        else:
            script = self.root / "stub_node.py"
            script.write_text(_STUB_NODE, encoding="utf-8")
            body = f"exec {sys.executable} {script} {stub} {bundle.digest()} {protocol.PROTOCOL}\n"
        python.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        python.chmod(0o755)
        return python

    def agent(self) -> str:
        """The command of an agent on this machine: the echo agent above."""
        script = self.root / "echo_agent.py"
        script.write_text(_ECHO_AGENT, encoding="utf-8")
        return f"{sys.executable} {script}"

    def launches(self) -> list[list[str]]:
        path = self.bin / "calls"
        if not path.exists():
            return []
        calls = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [argv for argv in calls if "-T" in argv]


def row(**extra: Any) -> dict[str, Any]:
    return {
        "id": "box",
        "display_name": "Lab box",
        "host": HOST,
        "port": PORT,
        "user": "worker",
        "key": "/keys/id_test",
        **extra,
    }


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    if not Path("/bin/sh").exists():
        pytest.skip("the stand-in ssh hands the ssh line to a POSIX shell")
    return Machine(tmp_path, monkeypatch)


@pytest.fixture
async def pool():
    nodes = NodePool()
    yield nodes
    await nodes.close_all()


def _no_address(message: str) -> None:
    assert HOST not in message and str(PORT) not in message and "worker@" not in message


async def _until(check: Any, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "did not happen in time"
        await asyncio.sleep(0.02)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        # Reaped by nobody yet: a zombie answers the signal but is gone.
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == pid
    except ChildProcessError:
        return False


# --- a node over the real ssh line ------------------------------------------------


@pytest.mark.slow
async def test_one_node_answers_every_call_to_its_machine_as_the_tool_would_here(box, pool):
    box.node()
    work = box.home / "work"
    work.mkdir()
    (work / "notes.txt").write_text("".join(f"note {i}\n" for i in range(1, 41)), encoding="utf-8")
    params = {"path": str(work / "notes.txt"), "offset": 10, "limit": 3, "roots": [str(work)]}

    first = await pool.call("box", protocol.READ, params)
    second = await pool.call("box", protocol.READ, params)

    local = ReadFileTool(workspace=work, allowed_dirs=(work,), follow_binding=False)
    assert first == second
    assert first["text"] == await local.execute(str(work / "notes.txt"), offset=10, limit=3)
    assert first["sha256"] == hashlib.sha256((work / "notes.txt").read_bytes()).hexdigest()
    assert len(box.launches()) == 1, "one ssh serves every call"
    assert pool.live() == ["box"]
    hello = await pool.hello("box")
    assert hello["digest"] == bundle.digest() and hello["home"] == str(box.home)
    assert hello["protocol"] == protocol.PROTOCOL and hello["limits"]["agents"] == protocol.MAX_AGENTS


@pytest.mark.slow
async def test_the_node_is_started_from_where_the_registry_keeps_nodes_isolated_and_without_a_login_shell(
    box, pool, tmp_path
):
    kept = tmp_path / "nodes on a big disk"
    box.rows([row(node_dir=f"{kept}/")])
    box.node(kept)

    hello = await pool.hello("box")

    assert hello["digest"] == bundle.digest()
    (launch,) = box.launches()
    command = launch[launch.index("--") + 1]
    assert str(kept / bundle.install_name() / "bin" / "python") in command
    # Isolated: the login shell's PYTHONPATH or PYTHONHOME must not reach the
    # node, where a shadow package would answer with another digest.
    assert " -I -m raven.node --attach --dir " in command
    # One plain line any login shell reads, from the directory the login
    # lands in: no profile, no directory made, nothing read from stdin ahead
    # of the protocol. The node captures the login environment itself.
    assert command.startswith("exec /bin/sh -c 'echo; ")
    assert "mkdir" not in command and "-lic" not in command and "read -r" not in command
    assert "-T" in launch and "-E" in launch and "ServerAliveInterval=15" in launch


@pytest.mark.slow
async def test_a_node_that_ended_is_started_again_on_the_next_call(box, pool):
    box.node()
    await pool.hello("box")
    await pool._nodes["box"].client.close()
    assert pool.live() == []

    await pool.hello("box")

    assert len(box.launches()) == 2
    assert pool.live() == ["box"]


@pytest.mark.slow
async def test_a_machine_without_the_node_says_how_to_install_it(box, pool):
    with pytest.raises(NodeError) as caught:
        await pool.hello("box")

    assert str(caught.value) == (
        f"Raven node {bundle.install_name()} is not installed on machine 'Lab box' (box); "
        "install it with `raven ops connection install-node box`"
    )
    assert pool.live() == []
    assert pool.status("box").state == "error" and "is not installed" in pool.status("box").detail


@pytest.mark.slow
async def test_an_unreachable_machine_is_named_without_its_address(box, pool):
    box.node()
    box.mode("unreachable")

    with pytest.raises(NodeError) as caught:
        await pool.hello("box")

    assert str(caught.value) == "machine 'Lab box' (box) did not answer within the connection timeout"
    _no_address(str(caught.value))


@pytest.mark.slow
@pytest.mark.parametrize("stub", ["other-code", "other-protocol", "not-an-object"])
async def test_a_node_of_other_code_is_refused_with_the_command_that_updates_it(box, pool, stub):
    box.node(stub=stub)

    with pytest.raises(NodeError) as caught:
        await pool.hello("box")

    assert str(caught.value) == (
        "the Raven node on machine 'Lab box' (box) runs different code from this Raven; "
        "update it with `raven ops connection install-node box`"
    )
    assert pool.live() == []


@pytest.mark.slow
async def test_a_node_that_refuses_the_handshake_says_why(box, pool):
    box.node(stub="refuse")

    with pytest.raises(NodeError) as caught:
        await pool.hello("box")

    assert str(caught.value) == (
        "the Raven node on machine 'Lab box' (box) did not complete its handshake: this node speaks protocol 1, not 2"
    )


@pytest.mark.slow
async def test_a_node_that_cannot_start_gives_its_own_last_word(box, pool):
    box.node(stub="crash")

    with pytest.raises(NodeError) as caught:
        await pool.hello("box")

    assert str(caught.value) == (
        "the Raven node on machine 'Lab box' (box) stopped: ModuleNotFoundError: No module named 'loguru'"
    )


@pytest.mark.slow
async def test_a_node_that_never_answers_the_handshake_is_given_up_on(box, pool, monkeypatch):
    monkeypatch.setattr(node_client, "_HELLO_TIMEOUT_S", 0.5)
    box.node(stub="silent")

    with pytest.raises(NodeError, match=r"^the Raven node on machine 'Lab box' \(box\) did not complete its handshake"):
        await pool.hello("box")

    assert pool.live() == []


@pytest.mark.slow
async def test_a_call_that_runs_out_of_time_closes_the_node(box, pool):
    box.node(stub="hello-only")

    with pytest.raises(NodeError) as caught:
        await pool.call("box", protocol.READ, {"path": "x", "roots": ["/"]}, timeout=1.0)

    assert str(caught.value) == "the Raven node on machine 'Lab box' (box) did not answer fs/read within 1s"
    assert pool.live() == [] and "box" not in pool._nodes
    assert pool.status("box").state == "error"


@pytest.mark.slow
async def test_a_node_starts_without_a_debug_switch(box, pool, monkeypatch):
    box.node()
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES", raising=False)
    hello = await pool.hello("box")
    assert hello["protocol"] == protocol.PROTOCOL
    assert len(box.launches()) == 1


async def test_a_machine_that_is_not_registered_is_refused_before_any_ssh(box, pool):
    with pytest.raises(NodeError, match="machine 'elsewhere' is not registered"):
        await pool.hello("elsewhere")

    assert box.launches() == []


async def test_the_ssh_argv_reaches_the_connection_as_it_is_for_no_host_grammar_to_read_again(box, pool, monkeypatch):
    launched: list[Any] = []

    class _Stub:
        @classmethod
        async def launch(cls, **kw: Any) -> None:
            launched.append(kw["command"])
            raise AcpConnectionError("stood in")

    monkeypatch.setattr(node_client, "AcpClient", _Stub)

    with pytest.raises(NodeError, match="could not start ssh to machine 'Lab box'"):
        await pool.hello("box")

    (argv,) = launched
    assert isinstance(argv, list) and argv[0] == "ssh" and argv[-2] == "--", "an argv, not a line to split again"
    assert argv[-1].startswith("exec /bin/sh -c 'echo; exec ") and "-m raven.node --attach --dir" in argv[-1]


async def test_an_ssh_command_that_cannot_be_made_is_one_sentence(box, pool, monkeypatch):
    def refuse(*args: Any, **kwargs: Any) -> str:
        raise ValueError("a command for a registered machine must be one line")

    monkeypatch.setattr(remote, "ssh_command", refuse)

    with pytest.raises(NodeError, match=r"^machine 'Lab box' \(box\): a command .* must be one line$") as caught:
        await pool.hello("box")

    _no_address(str(caught.value))
    assert box.launches() == []


async def test_no_ssh_on_this_computer_is_one_sentence(box, pool, monkeypatch, tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setattr(backend_env, "_LOGIN_ENV", {**os.environ, "PATH": str(empty)})

    with pytest.raises(NodeError, match=r"^could not start ssh to machine 'Lab box' \(box\): ") as caught:
        await pool.hello("box")

    _no_address(str(caught.value))


# --- what the link carries beside file calls -----------------------------------------


@pytest.mark.slow
async def test_the_session_directory_is_made_on_the_machine_and_comes_back_absolute(box, pool):
    box.node()

    made, since = await pool.session_dir("box", "~/raven work/a b-1234")

    assert made == str((box.home / "raven work" / "a b-1234").resolve())
    assert Path(made).is_dir()
    assert abs(since - time.time()) < 60, "the machine's clock, as the directory was made"
    assert len(box.launches()) == 1, "on the link, not an ssh of its own"


@pytest.mark.slow
async def test_a_session_directory_the_machine_cannot_make_is_refused_with_its_reason(box, pool):
    box.node()
    (box.home / "blocker").write_text("a file, not a directory\n", encoding="utf-8")

    with pytest.raises(NodeError, match=r"refused session/dir: could not make .*blocker/t1") as caught:
        await pool.session_dir("box", "~/blocker/t1")

    _no_address(str(caught.value))
    assert pool.live() == ["box"], "a refusal keeps the link"


@pytest.mark.slow
async def test_the_machine_and_an_agent_s_command_are_probed_over_the_link(box, pool):
    box.node()

    machine = await pool.probe_machine("box")
    found = await pool.probe_agent("box", box.agent())
    missing = await pool.probe_agent("box", "no-such-agent-for-raven-tests --acp")

    assert machine["python"].count(".") == 2 and str(box.home) in machine["disk_free_kb"]
    assert found["found"] == sys.executable and missing["found"] is None
    assert len(box.launches()) == 1


# --- an agent on the machine, through the node -----------------------------------------


@pytest.mark.slow
async def test_an_agent_started_through_the_node_is_driven_as_a_local_one_is(box, pool):
    box.node()
    work = box.home / "raven-work"

    endpoint = await pool.open_channel("box", name="echo@box", command=box.agent(), cwd="~/raven-work", env={})
    client = AcpClient.over(endpoint, name="echo@box")
    try:
        assert await client.request("ping", {"x": 1}, timeout=10) == {"echo": {"x": 1}}
        assert await client.request("ping", {"x": 2}, timeout=10) == {"echo": {"x": 2}}
        await _until(lambda: "agent: started" in client.stderr_tail())
        assert work.is_dir(), "the agent's root is made for it"
        assert isinstance(endpoint.pid, int) and endpoint.describe().startswith("task echo@box-")
        assert "on machine 'Lab box' (box), pid " in endpoint.describe()
        assert pool.status("box").state == "connected" and pool.status("box").said() == "connected, 1 agent running"
        assert len(box.launches()) == 1, "the agent rides the node's ssh"
    finally:
        await client.close()
    await _until(lambda: pool.status("box").state == "idle")
    assert pool.status("box").said() == "connected, idle"


@pytest.mark.slow
async def test_the_entry_s_env_reaches_the_agent_over_the_link_and_never_an_ssh_argument(box, pool):
    box.node()
    probe = (
        "import json, os, sys; "
        "print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'key': os.environ.get('OPENAI_API_KEY'), "
        "'role': os.environ.get('RAVEN_SUBAGENT')}}), flush=True); sys.stdin.read()"
    )
    env = {"OPENAI_API_KEY": "sk-raven-test-not-real", "RAVEN_SUBAGENT": "1"}

    endpoint = await pool.open_channel(
        "box", name="probe@box", command=f"{sys.executable} -c {json.dumps(probe)}", cwd="~", env=env
    )
    client = AcpClient.over(endpoint, name="probe@box")
    try:
        assert await client.request("whatever", {}, timeout=10) == {"key": "sk-raven-test-not-real", "role": "1"}
    finally:
        await client.close()
    for argv in box.launches():
        assert "sk-raven-test-not-real" not in " ".join(argv)


@pytest.mark.slow
async def test_an_agent_that_exits_ends_its_connection_with_its_exit_code(box, pool):
    box.node()
    endpoint = await pool.open_channel("box", name="echo@box", command=box.agent(), cwd="~", env={})
    client = AcpClient.over(endpoint, name="echo@box")

    with pytest.raises(AcpConnectionError, match=r"connection ended \(exit 7\)"):
        await client.request("quit", {"rc": 7}, timeout=10)

    await _until(lambda: not client.alive)
    assert endpoint.returncode == 7 and endpoint.ended() is None
    assert "agent: started" in client.stderr_tail()
    assert pool.status("box").state == "idle", "the channel is gone, the link stays"
    await client.close()


@pytest.mark.slow
async def test_closing_the_connection_ends_the_agent_and_what_it_started(box, pool):
    box.node()
    endpoint = await pool.open_channel("box", name="echo@box", command=box.agent(), cwd="~", env={})
    client = AcpClient.over(endpoint, name="echo@box")
    child = (await client.request("spawn", {}, timeout=10))["pid"]
    agent = endpoint.pid
    assert agent is not None and not _gone(agent) and not _gone(child)

    await client.close()

    await _until(lambda: _gone(agent))
    await _until(lambda: _gone(child))
    assert endpoint.returncode is not None and not client.alive


@pytest.mark.slow
async def test_an_agent_whose_command_is_not_on_the_machine_says_so(box, pool):
    box.node()

    with pytest.raises(NodeError) as caught:
        await pool.open_channel("box", name="x@box", command="no-such-agent-for-raven-tests --acp", cwd="~", env={})

    assert str(caught.value) == (
        "the agent's command was not found on machine 'Lab box' (box); "
        "install it there, for the user the machine is registered with"
    )
    assert pool.live() == ["box"] and pool.status("box").state == "idle"


@pytest.mark.slow
async def test_an_agent_whose_root_cannot_be_made_says_so(box, pool):
    box.node()
    (box.home / "blocker").write_text("a file\n", encoding="utf-8")

    with pytest.raises(NodeError, match=r"could not start 'x@box': could not make .*blocker/agents") as caught:
        await pool.open_channel("box", name="x@box", command=box.agent(), cwd="~/blocker/agents", env={})

    _no_address(str(caught.value))


async def test_a_machine_full_of_agents_refuses_the_next_in_one_sentence(tmp_path):
    # The node's own refusal (tested with the node); here the sentence the
    # host makes of its code, with the limit the handshake reported.
    pool = NodePool()
    busy = AcpRemoteError(protocol.AGENT_LAUNCH, protocol.BUSY, "8 agents are already running on this machine")
    _live(pool, tmp_path, raises=busy)

    with pytest.raises(NodeError, match=r"^machine 'Lab box' \(box\) already runs 8 agents; wait for one to finish$"):
        await pool.open_channel("box", name="b@box", command="agent", cwd="~", env={})

    assert pool.live() == ["box"] and pool._nodes["box"].channels == {}


@pytest.mark.slow
async def test_a_link_that_goes_leaves_the_agent_running_and_the_turn_goes_on_once_it_is_back(box, pool):
    """The ssh is the way to the machine, not the agent's life: a request made while it is down is answered after."""
    box.node()
    pool._heartbeat_s = 0.1
    pool._lost_after_s = 1.0
    pool._backoff_s = (0.2,)
    endpoint = await pool.open_channel("box", name="echo@box", command=box.agent(), cwd="~", env={})
    client = AcpClient.over(endpoint, name="echo@box")
    assert await client.request("ping", {"x": 1}, timeout=10) == {"echo": {"x": 1}}
    relay = pool._nodes["box"].client._proc.pid
    daemon = pool._nodes["box"].hello["pid"]
    assert relay != daemon, "the ssh's far end is the relay; the daemon is another process"
    agent = endpoint.pid

    os.kill(relay, signal.SIGKILL)
    await _until(lambda: endpoint.detached is not None)

    # Asked while the link is down: held here, sent once the task is attached again.
    assert await client.request("ping", {"x": 2}, timeout=20) == {"echo": {"x": 2}}
    assert endpoint.detached is None and endpoint.pid == agent and not _gone(agent)
    assert len(box.launches()) == 2, "one ssh per link"
    assert pool.status("box").state == "connected" and pool._nodes["box"].hello["pid"] == daemon
    await client.close()
    await _until(lambda: _gone(agent))


@pytest.mark.slow
async def test_a_daemon_that_stops_takes_its_agents_and_the_turn_says_so(box, pool):
    box.node()
    pool._heartbeat_s = 0.1
    pool._lost_after_s = 1.0
    pool._backoff_s = (0.2,)
    endpoint = await pool.open_channel("box", name="echo@box", command=box.agent(), cwd="~", env={})
    client = AcpClient.over(endpoint, name="echo@box")
    assert await client.request("ping", {"x": 1}, timeout=10) == {"echo": {"x": 1}}
    daemon = pool._nodes["box"].hello["pid"]
    agent = endpoint.pid

    os.kill(daemon, signal.SIGTERM)

    await _until(lambda: _gone(agent)), "a daemon told to stop takes its agents with it"
    with pytest.raises(AcpConnectionError) as caught:
        await client.request("ping", {"x": 2}, timeout=20)
    said = str(caught.value)
    assert "echo@box" in said and "connection ended" in said
    _no_address(said)
    assert endpoint.returncode is not None and endpoint.returncode != 0
    # The machine is reachable again: the next relay started a daemon anew.
    assert pool.status("box").state in ("idle", "connected") and pool._nodes["box"].hello["pid"] != daemon
    await client.close()


@pytest.mark.slow
async def test_closing_the_link_on_purpose_leaves_the_agent_on_the_machine_for_the_next_link_to_find(box, pool):
    box.node()
    endpoint = await pool.open_channel("box", name="echo@box", command=box.agent(), cwd="~", env={})
    client = AcpClient.over(endpoint, name="echo@box")
    assert await client.request("ping", {}, timeout=10) == {"echo": {}}
    agent = endpoint.pid

    await pool.close("box")

    assert endpoint.ended() == "the link to machine 'Lab box' (box) was closed"
    assert not _gone(agent), "closing the link is not cancelling the agent"
    listed = await pool.tasks("box")
    assert [(row["task"], row["state"], row["attached"]) for row in listed] == [(endpoint.task, "running", False)]
    await pool.call("box", protocol.AGENT_ATTACH, {"channel": 9, "task": endpoint.task, "after": 1})
    await pool.call("box", protocol.AGENT_CANCEL, {"channel": 9})
    await _until(lambda: _gone(agent))
    await client.close()


@pytest.mark.slow
async def test_while_this_computer_is_leaving_the_endpoint_s_end_leaves_the_agent_with_the_machine(box, pool):
    """The host is stopping: closing the connection ends the endpoint only, and the daemon keeps the agent."""
    box.node()
    endpoint = await pool.open_channel("box", name="echo@box", command=box.agent(), cwd="~", env={})
    client = AcpClient.over(endpoint, name="echo@box")
    assert await client.request("ping", {}, timeout=10) == {"echo": {}}
    agent = endpoint.pid

    remote.leave_agents_running()
    try:
        await client.close()
    finally:
        remote.staying()

    assert endpoint.returncode == node_client.LOST_RC and "left running on task" in (endpoint.ended() or "")
    await asyncio.sleep(0.3)
    assert not _gone(agent), "the agent stays with the daemon"
    assert [(row["task"], row["state"]) for row in await pool.tasks("box")] == [(endpoint.task, "running")]
    picked, answer = await pool.attach("box", endpoint.task, name="echo@box", after=endpoint.received)
    assert answer["pid"] == agent and answer["rc"] is None
    await picked.end()
    await _until(lambda: _gone(agent))


@pytest.mark.slow
async def test_a_task_is_picked_up_from_the_frame_last_read_and_the_watchers_hear_each_frame_dealt_with(box, pool):
    box.node()
    endpoint = await pool.open_channel("box", name="echo@box", command=box.agent(), cwd="~", env={})
    heard: list[int] = []
    endpoint.watchers.append(heard.append)
    client = AcpClient.over(endpoint, name="echo@box")
    await client.request("ping", {"n": 1}, timeout=10)
    await client.request("ping", {"n": 2}, timeout=10)
    await asyncio.sleep(0.05)
    assert heard == [1, 2] and endpoint.received == 2
    agent = endpoint.pid
    await pool.close("box")

    picked, answer = await pool.attach("box", endpoint.task, name="echo@box", after=2)

    assert answer["pid"] == agent and answer["replayed"] == 0 and answer["rc"] is None
    again = AcpClient.over(picked, name="echo@box")
    again.continue_ids_from(100)
    assert await again.request("ping", {"n": 3}, timeout=10) == {"echo": {"n": 3}}
    assert picked.received == 3, "numbered on from the frames the first link read"
    await again.close()
    await _until(lambda: _gone(agent))
    await client.close()


@pytest.mark.slow
async def test_picking_up_a_task_the_machine_does_not_know_is_one_sentence(box, pool):
    box.node()

    with pytest.raises(NodeError, match=r"machine 'Lab box' \(box\) no longer runs 'echo@box' \(task nothing-here\)"):
        await pool.attach("box", "nothing-here", name="echo@box")


# --- a live node's calls, with the connection stood in for -------------------------


class _Client:
    """A connection that answers, or fails, the way it is told to."""

    def __init__(self, *, result: Any = None, raises: BaseException | None = None) -> None:
        self.result = {"text": "ok"} if result is None else result
        self.raises = raises
        self.alive = True
        self.asked: list[tuple[str, dict[str, Any], float]] = []
        self.said: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        self.asked.append((method, params, timeout))
        if self.raises is not None and method != protocol.SHUTDOWN:
            raise self.raises
        return self.result

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.said.append((method, params))

    async def close(self) -> None:
        self.alive = False


def _box(machine_id: str = "box") -> remote.Machine:
    return remote.Machine(id=machine_id, display_name="Lab box", _row=row(id=machine_id))


def _live(pool: NodePool, tmp_path: Path, machine_id: str = "box", client: _Client | None = None, **kw: Any) -> _Client:
    client = client or _Client(**kw)
    log = tmp_path / f"{machine_id}.log"
    log.write_text(f"ssh: connect to host {HOST} port {PORT}: Connection refused\n", encoding="utf-8")
    hello = {"protocol": protocol.PROTOCOL, "home": "/home/worker", "version": "0.9", "limits": {"agents": 8}}
    pool._nodes[machine_id] = _Node(client, _box(machine_id), log, hello, {})  # type: ignore[arg-type]
    return client


async def test_a_call_is_sent_with_its_budget_and_its_answer_returned(tmp_path):
    pool = NodePool()
    client = _live(pool, tmp_path, result={"text": "line 1"})
    before = pool._nodes["box"].used

    assert await pool.call("box", protocol.READ, {"path": "a"}, timeout=7.0) == {"text": "line 1"}

    assert client.asked == [(protocol.READ, {"path": "a"}, 7.0)]
    assert pool._nodes["box"].used >= before
    assert await pool.home("box") == "/home/worker"


async def test_an_answer_that_is_not_an_object_reads_as_empty(tmp_path):
    pool = NodePool()
    _live(pool, tmp_path, result=["not", "an", "object"])

    assert await pool.call("box", protocol.READ, {}) == {}


async def test_a_refused_call_keeps_the_node(tmp_path):
    pool = NodePool()
    client = _live(pool, tmp_path, raises=AcpRemoteError(protocol.READ, -32602, "roots must be a non-empty list"))

    with pytest.raises(NodeError) as caught:
        await pool.call("box", protocol.READ, {})

    assert str(caught.value) == (
        "the Raven node on machine 'Lab box' (box) refused fs/read: roots must be a non-empty list"
    )
    assert caught.value.reason is None and client.alive and pool.live() == ["box"]
    client.raises = AcpRemoteError(protocol.WRITE, protocol.CHANGED, "changed")
    with pytest.raises(NodeError) as changed:
        await pool.call("box", protocol.WRITE, {})
    assert changed.value.reason == "changed", "the node's own refusal, by name, for the file tools"


async def test_a_call_that_timed_out_drops_the_node(tmp_path):
    pool = NodePool(backoff_s=())
    client = _live(pool, tmp_path, raises=AcpTimeoutError("no answer", method=protocol.READ))

    with pytest.raises(NodeError, match=r"did not answer fs/read within 120s$"):
        await pool.call("box", protocol.READ, {})

    assert not client.alive and "box" not in pool._nodes
    await pool.close_all()


@pytest.mark.parametrize(
    ("returncode", "stderr", "said"),
    [
        (255, "", "machine 'Lab box' (box) could not be reached"),
        (
            127,
            "",
            "this Raven's node is not installed on machine 'Lab box' (box); "
            "install it with `raven ops connection install-node box`",
        ),
        (
            126,
            "sh: exec: /root/.raven-node/x/bin/python: cannot execute: No such file or directory",
            "this Raven's node is not installed on machine 'Lab box' (box); "
            "install it with `raven ops connection install-node box`",
        ),
        (
            1,
            "bash: no job control in this shell\nTraceback (most recent call last):\nMemoryError: out of memory\n",
            "the Raven node on machine 'Lab box' (box) stopped: MemoryError: out of memory",
        ),
        (None, None, "the Raven node on machine 'Lab box' (box) stopped"),
    ],
)
async def test_a_connection_that_ended_mid_call_is_one_sentence(tmp_path, returncode, stderr, said):
    pool = NodePool(backoff_s=())
    client = _live(pool, tmp_path, raises=AcpConnectionError("gone", stderr=stderr, returncode=returncode))

    with pytest.raises(NodeError) as caught:
        await pool.call("box", protocol.READ, {})

    assert str(caught.value) == said
    _no_address(str(caught.value))
    assert not client.alive and "box" not in pool._nodes
    await pool.close_all()


async def test_a_node_left_idle_is_closed_when_another_machine_is_asked_for_unless_an_agent_runs_on_it(tmp_path):
    pool = NodePool(idle_close_s=60.0)
    old = _live(pool, tmp_path, "old")
    busy = _live(pool, tmp_path, "busy")
    recent = _live(pool, tmp_path, "recent")
    asked = _live(pool, tmp_path, "asked")
    for machine_id in ("old", "busy", "asked"):
        pool._nodes[machine_id].used = time.monotonic() - 61.0
    pool._nodes["busy"].channels[1] = ChannelEndpoint(busy, 1, target=_box("busy"), agent="a")  # type: ignore[arg-type]

    await pool.call("asked", protocol.READ, {})

    assert not old.alive and busy.alive and recent.alive and asked.alive
    assert sorted(pool.live()) == ["asked", "busy", "recent"]
    assert old.asked == [], "a link closed by choice is just closed; its agents stay with the machine"


async def test_a_call_on_its_way_to_a_node_keeps_its_link_however_long_ago_it_was_last_used(tmp_path):
    pool = NodePool(idle_close_s=60.0)

    class _Slow(_Client):
        async def request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
            await asyncio.sleep(0.3)
            return await super().request(method, params, timeout=timeout)

    _live(pool, tmp_path, "slow")
    pool._nodes["slow"].client = slow = _Slow()  # type: ignore[assignment]
    pool._nodes["slow"].used = time.monotonic() - 61.0
    _live(pool, tmp_path, "other")
    on_its_way = asyncio.create_task(pool.call("slow", protocol.READ, {}))
    await asyncio.sleep(0.05)

    await pool.call("other", protocol.READ, {})

    assert slow.alive and "slow" in pool.live(), "a link with a call on its way is in use, not idle"
    assert await on_its_way == {"text": "ok"}
    assert pool._nodes["slow"].calls == 0


async def test_live_lists_only_the_nodes_still_running_and_close_ends_them(tmp_path):
    pool = NodePool()
    one = _live(pool, tmp_path, "one")
    two = _live(pool, tmp_path, "two")
    two.alive = False
    assert pool.live() == ["one"]

    await pool.close("one")
    assert not one.alive and pool.live() == []

    three = _live(pool, tmp_path, "three")
    await pool.close_all()
    assert not three.alive and pool._nodes == {}
    await pool.close("never-started")


async def test_one_pool_serves_the_process_until_it_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(node_client, "_POOL", None)
    pool = node_client.get_node_pool()
    assert node_client.get_node_pool() is pool
    client = _live(pool, tmp_path)

    await node_client.close_node_pool()

    assert not client.alive
    assert node_client.get_node_pool() is not pool
    await node_client.close_node_pool()
    await node_client.close_node_pool()


# --- the heartbeat, with the connection stood in for ------------------------------------


class _Deaf(_Client):
    """A connection that answers everything but a ping."""

    async def request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        self.asked.append((method, params, timeout))
        if method == protocol.PING:
            raise AcpTimeoutError("no answer", method=method)
        return self.result


async def test_a_heartbeat_unanswered_loses_the_link_tells_its_agents_and_reconnects(tmp_path, monkeypatch):
    pool = NodePool(heartbeat_s=0.01, lost_after_s=0.5, backoff_s=(0.01, 0.01))
    client = _Deaf()
    log = tmp_path / "box.log"
    log.write_text("", encoding="utf-8")
    node = _Node(client, _box(), log, {"home": "/h"}, {})  # type: ignore[arg-type]
    pool._nodes["box"] = node
    endpoint = ChannelEndpoint(client, 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    node.channels[1] = endpoint
    tried: list[str] = []

    async def launch(machine_id: str) -> _Node:
        tried.append(machine_id)
        if len(tried) == 1:
            raise NodeError("machine 'Lab box' (box) could not be reached")
        return _Node(_Client(), _box(), log, {"home": "/h"}, {})  # type: ignore[arg-type]

    monkeypatch.setattr(pool, "_launch", launch)
    node.heartbeat = asyncio.create_task(pool._heartbeat("box", node))

    await _until(lambda: not client.alive)
    assert endpoint.returncode is None and endpoint.ended() is None, "the agent is not ended: its link is"
    assert endpoint.detached == "machine 'Lab box' (box) did not answer a heartbeat within 0s"
    status = pool.status("box")
    assert status.state == "lost" and status.agents == 1
    assert status.said() == (
        "link lost: machine 'Lab box' (box) did not answer a heartbeat within 0s; "
        "1 task continues on the machine; reconnecting"
    )
    await _until(lambda: len(tried) == 2 and pool.live() == ["box"] and endpoint.detached is None)
    again = pool._nodes["box"].client
    # Among the new link's requests, not its last: a heartbeat may well follow the attach.
    attach = (protocol.AGENT_ATTACH, {"channel": 1, "task": endpoint.task, "after": 0, "stderr_after": 0})
    assert attach in [asked[:2] for asked in again.asked]
    assert pool.status("box").state == "connected" and pool._nodes["box"].channels[1] is endpoint
    await pool.close_all()


async def test_a_lost_link_that_does_not_come_back_keeps_its_last_sentence(tmp_path, monkeypatch):
    pool = NodePool(heartbeat_s=0.01, lost_after_s=0.5, backoff_s=(0.01,))
    client = _Deaf()
    node = _Node(client, _box(), tmp_path / "box.log", {}, {})  # type: ignore[arg-type]
    pool._nodes["box"] = node

    async def launch(machine_id: str) -> _Node:
        raise NodeError("machine 'Lab box' (box) refused the login; check the key it is registered with")

    monkeypatch.setattr(pool, "_launch", launch)
    node.heartbeat = asyncio.create_task(pool._heartbeat("box", node))

    await _until(lambda: not client.alive and "box" not in pool._reconnects)
    assert pool.status("box") == node_client.MachineStatus(
        "error", "machine 'Lab box' (box) refused the login; check the key it is registered with"
    )
    assert pool.status("elsewhere") == node_client.MachineStatus("off") and pool.status("elsewhere").said() == ""


async def test_a_channel_endpoint_carries_frames_both_ways_and_ends_with_the_agent(tmp_path):
    link = _Client()
    endpoint = ChannelEndpoint(link, 3, target=_box(), agent="echo@box")  # type: ignore[arg-type]

    await endpoint.write(b'{"jsonrpc":"2.0","id":1,"method":"x"}\n')
    endpoint.feed_frame('{"jsonrpc":"2.0","id":1,"result":{}}')
    endpoint.feed_stderr("agent: hello")
    endpoint.exited(3)
    endpoint.feed_frame("too late")

    assert link.said == [(protocol.AGENT_FRAME, {"channel": 3, "line": '{"jsonrpc":"2.0","id":1,"method":"x"}'})]
    assert await endpoint.readline() == b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
    assert await endpoint.readline() == b""
    assert await endpoint.readerr() == b"agent: hello\n" and await endpoint.readerr() == b""
    assert endpoint.returncode == 3 and await endpoint.wait() == 3 and endpoint.ended() is None
    with pytest.raises(BrokenPipeError):
        await endpoint.write(b"{}\n")
    await endpoint.end()
    assert all(method != protocol.AGENT_CANCEL for method, *_ in link.asked), "nothing to cancel once it has exited"


async def test_a_blank_line_is_a_frame_the_endpoint_hands_on_as_a_pipe_would():
    endpoint = ChannelEndpoint(_Client(), 1, target=_box(), agent="cli@box")  # type: ignore[arg-type]
    endpoint.feed_frame("a", 1)
    endpoint.feed_frame("", 2)
    endpoint.feed_frame("b", 3)
    endpoint.exited(0)

    assert [await endpoint.readline() for _ in range(4)] == [b"a\n", b"\n", b"b\n", b""]
    assert endpoint.received == 3, "the blank line counts, so an attach picks up where this one stopped"


async def test_ending_a_running_channel_is_a_cancel_on_the_link(tmp_path):
    link = _Client()
    endpoint = ChannelEndpoint(link, 2, target=_box(), agent="echo@box")  # type: ignore[arg-type]

    await endpoint.end()

    assert link.asked == [(protocol.AGENT_CANCEL, {"channel": 2}, node_client._CANCEL_TIMEOUT_S)]
    assert endpoint.returncode is None, "the exit is the node's to report"


# --- where a node lives -------------------------------------------------------------


@pytest.mark.parametrize(
    ("kept", "want"),
    [
        (None, "~/.raven-node"),
        ("", "~/.raven-node"),
        ("  /srv/raven-node/  ", "/srv/raven-node"),
        ("~/elsewhere", "~/elsewhere"),
    ],
)
def test_the_node_dir_is_the_row_s_or_the_default(kept, want):
    extra = {} if kept is None else {"node_dir": kept}
    target = remote.Machine(id="box", display_name="Lab box", _row=row(**extra))

    assert node_client.node_dir(target) == want
    assert node_client.node_python(target, "0.1-abc") == f"{want}/0.1-abc/bin/python"
    assert node_client.install_hint(target) == "raven ops connection install-node box"


# --- files both ways, through the node -------------------------------------------------


@pytest.mark.slow
async def test_a_file_is_fetched_from_the_machine_whole_and_checked(box, pool, tmp_path):
    box.node()
    session = box.home / "raven-work" / "t1"
    session.mkdir(parents=True)
    roots = [f"{box.home}/raven-work"]
    payload = os.urandom(protocol.CHUNK * 2 + 123)
    (session / "report.bin").write_bytes(payload)

    info = await pool.stat("box", f"{session}/report.bin", roots=roots, hash=True)
    back = tmp_path / "back" / "report.bin"
    got = await pool.pull_file("box", f"{session}/report.bin", back, roots=roots)

    assert info["kind"] == "file" and info["size"] == len(payload)
    assert info["sha256"] == hashlib.sha256(payload).hexdigest()
    assert got == len(payload) and back.read_bytes() == payload
    assert not back.with_name("report.bin.raven-part").exists()
    with pytest.raises(NodeError, match="outside allowed directories"):
        await pool.pull_file("box", f"{box.home}/elsewhere.bin", back, roots=roots)
    assert len(box.launches()) == 1, "all of it on the one link"


@pytest.mark.slow
async def test_a_cli_agent_s_input_travels_on_the_link_and_its_stdin_ends_when_told(box, pool):
    box.node()

    endpoint = await pool.open_channel("box", name="cat@box", command="cat", cwd="~", env={})
    await endpoint.send_input("first line\nsecond line\n", eof=True)

    lines = []
    while raw := await endpoint.readline():
        lines.append(raw.decode("utf-8").rstrip("\n"))
    assert lines == ["first line", "second line"], "what went in came back, line by line"
    assert await endpoint.wait() == 0, "its stdin closed, cat left on its own"
    assert len(box.launches()) == 1, "on the node's ssh, like a frame"


async def test_input_written_while_the_link_is_down_goes_out_in_order_once_it_is_back():
    endpoint = ChannelEndpoint(_Client(), 1, target=_box(), agent="cli@box")  # type: ignore[arg-type]
    endpoint.detach("the link went")
    await endpoint.write(b'{"a": 1}\n')
    await endpoint.send_input("the prompt", eof=True)
    back = _Client()

    await endpoint.reattach(back, 7)  # type: ignore[arg-type]

    assert back.said == [
        (protocol.AGENT_FRAME, {"channel": 7, "line": '{"a": 1}'}),
        (protocol.AGENT_STDIN, {"channel": 7, "data": "the prompt", "eof": True}),
    ], "on the new channel, in the order they were written"


async def test_input_for_an_agent_that_has_ended_or_whose_link_just_broke_is_refused_or_held():
    endpoint = ChannelEndpoint(_Client(), 1, target=_box(), agent="cli@box")  # type: ignore[arg-type]
    endpoint.exited(0)
    with pytest.raises(BrokenPipeError, match="has ended"):
        await endpoint.send_input("late", eof=True)

    class _Gone(_Client):
        async def notify(self, method: str, params: dict[str, Any]) -> None:
            raise AcpConnectionError("the ssh ended")

    held = ChannelEndpoint(_Gone(), 1, target=_box(), agent="cli@box")  # type: ignore[arg-type]
    await held.send_input("the prompt", eof=True)
    assert held._pending == [(protocol.AGENT_STDIN, {"data": "the prompt", "eof": True})], "kept for the next link"


async def test_frames_held_for_a_link_that_is_down_are_capped(tmp_path):
    endpoint = ChannelEndpoint(_Client(), 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    endpoint.detach("the link went")
    for n in range(node_client._PENDING_FRAMES):
        await endpoint.write(f"{n}\n".encode())
    with pytest.raises(BrokenPipeError, match="down too long: 1000 frames wait"):
        await endpoint.write(b"one more\n")
    assert endpoint.detached == "the link went" and endpoint.returncode is None


async def test_a_task_the_machine_no_longer_knows_ends_its_endpoint_when_the_link_is_back(tmp_path):
    pool = NodePool()
    gone = AcpRemoteError(protocol.AGENT_ATTACH, protocol.UNKNOWN_TASK, "no task 'echo@box-1' is running or recorded")
    client = _live(pool, tmp_path, raises=gone)
    endpoint = ChannelEndpoint(client, 1, target=_box(), agent="echo@box", task="echo@box-1")  # type: ignore[arg-type]
    endpoint.detach("the link went")
    pool._parked["box"] = [endpoint]

    await pool._reattach("box")

    assert endpoint.returncode == node_client.LOST_RC
    assert endpoint.ended() == (
        "machine 'Lab box' (box) no longer runs 'echo@box' (task echo@box-1): no task 'echo@box-1' is running or recorded"
    )
    assert pool._parked.get("box", []) == [] and pool._nodes["box"].channels == {}
    assert client.asked[-1][1] == {"channel": 1, "task": "echo@box-1", "after": 0, "stderr_after": 0}
    await pool.close_all()


async def test_an_attach_the_link_cannot_carry_parks_every_waiting_endpoint_again(tmp_path):
    pool = NodePool(backoff_s=())
    client = _live(pool, tmp_path, raises=AcpTimeoutError("no answer", method=protocol.AGENT_ATTACH))
    first = ChannelEndpoint(client, 1, target=_box(), agent="a@box")  # type: ignore[arg-type]
    second = ChannelEndpoint(client, 2, target=_box(), agent="b@box")  # type: ignore[arg-type]
    for endpoint in (first, second):
        endpoint.detach("the link went")
    pool._parked["box"] = [first, second]

    await pool._reattach("box")

    assert first.returncode is None and second.returncode is None
    assert pool._parked["box"] == [first, second], "the one tried and the one not yet reached both wait"
    assert not client.alive and "box" not in pool._nodes
    assert len(client.asked) == 1, "the second was not tried on a link that had just failed"
    assert pool.status("box").state == "lost" and pool.status("box").agents == 2
    await pool.close_all()


async def test_a_pick_up_the_machine_refuses_or_cannot_carry_is_one_sentence_each(tmp_path):
    pool = NodePool(backoff_s=())
    gone = AcpRemoteError(protocol.AGENT_ATTACH, protocol.UNKNOWN_TASK, "no task 't' is running or recorded")
    _live(pool, tmp_path, "box", raises=gone)
    with pytest.raises(NodeError, match=r"machine 'Lab box' \(box\) no longer runs 'echo@box' \(task t\): no task"):
        await pool.attach("box", "t", name="echo@box")
    assert pool._nodes["box"].channels == {}, "the channel opened for it is closed again"

    slow = _live(pool, tmp_path, "slow", raises=AcpTimeoutError("no answer", method=protocol.AGENT_ATTACH))
    with pytest.raises(NodeError, match=r"did not answer agent/attach"):
        await pool.attach("slow", "t", name="echo@box")
    assert not slow.alive, "a link that does not answer is lost"

    dead = _live(pool, tmp_path, "dead", raises=AcpConnectionError("connection ended (exit 255)"))
    with pytest.raises(NodeError, match=r"machine 'Lab box' \(dead\)"):
        await pool.attach("dead", "t", name="echo@box")
    assert not dead.alive
    await pool.close_all()


async def test_a_watcher_that_fails_is_named_and_the_frames_go_on(tmp_path):
    from loguru import logger

    endpoint = ChannelEndpoint(_Client(), 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    heard: list[int] = []

    def bad(n: int) -> None:
        raise RuntimeError("no disk")

    endpoint.watchers.extend([bad, heard.append])
    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="WARNING")
    try:
        endpoint.feed_frame('{"jsonrpc":"2.0","id":1,"result":{}}', 1)
        assert await endpoint.readline() == b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
        endpoint.consumed()
    finally:
        logger.remove(sink)

    assert heard == [1] and any("a watcher of task" in message and "no disk" in message for message in records)


# --- a link replaced by an ordinary call brings its agents along ------------------------


async def test_a_call_that_replaces_a_dead_link_attaches_the_parked_agents_again(tmp_path, monkeypatch):
    """The transport died before the heartbeat saw it: the next call relaunches the link, and the agents come with it."""
    pool = NodePool(heartbeat_s=60.0)
    client = _live(pool, tmp_path)
    endpoint = ChannelEndpoint(client, 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    pool._nodes["box"].channels[1] = endpoint
    again = _Client()

    async def launch(machine_id: str) -> _Node:
        return _Node(again, _box(), tmp_path / "box.log", {"home": "/h"}, {})  # type: ignore[arg-type]

    monkeypatch.setattr(pool, "_launch", launch)
    client.alive = False

    assert await pool.call("box", protocol.PING, {}) == {"text": "ok"}

    assert pool._parked.get("box", []) == [] and endpoint.detached is None
    assert (
        protocol.AGENT_ATTACH,
        {"channel": 1, "task": endpoint.task, "after": 0, "stderr_after": 0},
    ) in [a[:2] for a in again.asked]
    assert pool._nodes["box"].client is again and pool._nodes["box"].channels[1] is endpoint
    await pool.close_all()


async def test_a_relaunch_that_fails_under_a_call_still_reconnects_for_the_parked_agents(tmp_path, monkeypatch):
    """The link died unnoticed and the call's own relaunch failed: the agents wait on the backoff, as after a heartbeat loss."""
    pool = NodePool(heartbeat_s=60.0, backoff_s=(0.01, 0.01))
    client = _live(pool, tmp_path)
    endpoint = ChannelEndpoint(client, 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    pool._nodes["box"].channels[1] = endpoint
    tried: list[str] = []
    again = _Client()

    async def launch(machine_id: str) -> _Node:
        tried.append(machine_id)
        if len(tried) == 1:
            raise NodeError("machine 'Lab box' (box) could not be reached")
        return _Node(again, _box(), tmp_path / "box.log", {"home": "/h"}, {})  # type: ignore[arg-type]

    monkeypatch.setattr(pool, "_launch", launch)
    client.alive = False

    with pytest.raises(NodeError, match="could not be reached"):
        await pool.call("box", protocol.PING, {})
    assert pool.waiting("box") == 1 and pool.status("box").state == "lost"
    assert "box" in pool._reconnects, (
        "an agent waits: the link is tried again on the backoff, not only by the next call"
    )

    await _until(lambda: endpoint.detached is None and len(tried) == 2)
    assert (
        protocol.AGENT_ATTACH,
        {"channel": 1, "task": endpoint.task, "after": 0, "stderr_after": 0},
    ) in [a[:2] for a in again.asked]
    assert pool.status("box").state == "connected"
    await pool.close_all()


class _Gone(_Client):
    """A connection that answers requests but can carry no notification: it died right after the attach."""

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.said.append((method, params))
        raise AcpConnectionError("the ssh ended", returncode=255, stderr="")


async def test_a_replay_the_link_cannot_carry_keeps_the_frames_and_every_waiting_endpoint(tmp_path):
    pool = NodePool(backoff_s=())
    client = _live(pool, tmp_path, client=_Gone())
    first = ChannelEndpoint(client, 1, target=_box(), agent="a@box")  # type: ignore[arg-type]
    second = ChannelEndpoint(client, 2, target=_box(), agent="b@box")  # type: ignore[arg-type]
    for endpoint in (first, second):
        endpoint.detach("the link went")
    await first.write(b"one\n")
    await first.write(b"two\n")
    pool._parked["box"] = [first, second]

    await pool._reattach("box")

    assert [params["line"] for _, params in first._pending] == ["one", "two"], (
        "nothing the link did not carry is dropped"
    )
    assert pool._parked["box"] == [first, second], "the one being replayed and the one not yet reached both wait"
    assert first.detached and second.detached and first.returncode is None and second.returncode is None
    assert not client.alive and "box" not in pool._nodes
    assert len(client.said) == 1, "the second frame was not tried on a link that had just failed"
    assert "box" in pool._reconnects, "and the link is tried again"
    assert pool.status("box").state == "lost" and pool.status("box").agents == 2
    await pool.close_all()


async def test_a_cancel_the_link_cannot_carry_is_kept_for_the_next_link(tmp_path):
    dead = _Client(raises=AcpConnectionError("the ssh ended", returncode=255, stderr=""))
    endpoint = ChannelEndpoint(dead, 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]

    await endpoint.end()  # the link died; the heartbeat has not said so yet

    assert endpoint._cancel_wanted and endpoint.returncode is None
    again = _Client()
    await endpoint.reattach(again, 3)
    assert (protocol.AGENT_CANCEL, {"channel": 3}, node_client._CANCEL_TIMEOUT_S) in again.asked
    assert not endpoint._cancel_wanted, "delivered"


async def test_a_cancel_that_fails_during_the_reattach_is_still_wanted_after_it(tmp_path):
    endpoint = ChannelEndpoint(_Client(), 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    endpoint.detach("the link went")
    await endpoint.end()
    assert endpoint._cancel_wanted
    dying = _Client(raises=AcpConnectionError("the ssh ended", returncode=255, stderr=""))

    await endpoint.reattach(dying, 2)  # the cancel's request fails on the new link too

    assert endpoint._cancel_wanted, "not delivered: still wanted"
    again = _Client()
    await endpoint.reattach(again, 4)
    assert (protocol.AGENT_CANCEL, {"channel": 4}, node_client._CANCEL_TIMEOUT_S) in again.asked
    assert not endpoint._cancel_wanted


async def test_a_cancel_the_node_refuses_is_not_wanted_any_more(tmp_path):
    refusing = _Client(raises=AcpRemoteError(protocol.AGENT_CANCEL, protocol.UNKNOWN_TASK, "no agent on channel 1"))
    endpoint = ChannelEndpoint(refusing, 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]

    await endpoint.end()

    assert not endpoint._cancel_wanted, "the node answered: there is nothing there to cancel"


async def test_an_exit_the_node_explains_reads_as_its_reason_and_the_lost_code():
    """A daemon restarted under the agent says so on the exit: the endpoint hands the sentence on in place of a bare code."""
    reason = "the Raven node on this machine was restarted while the agent ran, and the agent is gone"
    endpoint = ChannelEndpoint(_Client(), 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    endpoint.exited(node_client.LOST_RC, reason)

    assert await endpoint.wait() == node_client.LOST_RC and endpoint.ended() == reason

    plain = ChannelEndpoint(_Client(), 2, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    plain.exited(0, "")
    assert await plain.wait() == 0 and plain.ended() is None, "no reason given: a plain exit, as before"


async def test_an_endpoint_is_offline_once_parked_or_once_its_link_is_dead():
    endpoint = ChannelEndpoint(_Client(), 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    assert endpoint.offline is False
    endpoint.detach("the link went")
    assert endpoint.offline is True

    dead = _Client()
    other = ChannelEndpoint(dead, 2, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    dead.alive = False
    assert other.offline is True, "gone and not yet said so by the heartbeat: nothing sent now arrives"


async def test_a_cancel_while_the_link_is_down_does_not_wait_the_settle_budget(monkeypatch):
    """The cancel rides with the parked endpoint to the next link; the caller is not held for a settlement that cannot come."""
    from raven.acp_client import client as client_mod

    monkeypatch.setattr(client_mod, "_CANCEL_SETTLE_S", 30.0)
    endpoint = ChannelEndpoint(_Client(), 1, target=_box(), agent="echo@box")  # type: ignore[arg-type]
    client = AcpClient.over(endpoint, name="echo@box")
    try:
        endpoint.detach("the link went")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        started = time.monotonic()
        await asyncio.wait_for(client._cancel_turn("s1", future), timeout=5)

        assert time.monotonic() - started < 1.0, "no settle wait on a link known to be down"
        assert client.take_unsettled_cancel("s1") is True, "the turn may still run on the machine: not reused"
        held = [params.get("line", "") for method, params in endpoint._pending if method == protocol.AGENT_FRAME]
        assert any("session/cancel" in line for line in held), "the cancel itself waits for the next link"
    finally:
        await client.close()


# ---- MCP streams lent across the link ------------------------------------------------------------


async def _endpoint(name: str = "pg") -> tuple[str, list[bytes], Any]:
    """A unix socket standing in for a host endpoint: every line it is sent, and the writers back to each stream.

    In a directory of its own under the system's temporary one, short enough
    for a socket's path, which pytest's is not on every system.
    """
    if not callable(getattr(asyncio, "start_unix_server", None)):
        pytest.skip("this endpoint fixture needs a Unix-socket host")
    base = Path(tempfile.mkdtemp(prefix="raven-ep-"))
    path = str(base / f"{name}.sock")
    got: list[bytes] = []
    writers: list[asyncio.StreamWriter] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        while line := await reader.readline():
            got.append(line)

    listening = await asyncio.start_unix_server(serve, path=path)

    def close() -> None:
        listening.close()
        shutil.rmtree(base, ignore_errors=True)

    return path, got, SimpleNamespace(base=base, writers=writers, close=close)


def _told(client: _Client, method: str) -> list[dict[str, Any]]:
    return [params for said, params in client.said if said == method]


async def test_a_lent_server_s_stream_is_pumped_into_its_endpoint_and_the_endpoint_s_frames_go_out_in_pieces(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(protocol, "MCP_CHUNK", 4)
    pool = NodePool()
    client = _live(pool, tmp_path)
    node = pool._nodes["box"]
    path, got, endpoint = await _endpoint()
    try:
        pool.lend("box", "d1", {"pg": path}, task="agent")
        await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 1, "task": "agent", "lend": "d1", "server": "pg"})
        assert list(node.streams) == [1]

        # The machine's pieces make one line into the endpoint.
        await pool._on_stream("box", protocol.MCP_FRAME, {"stream": 1, "text": "abc", "end": False})
        await pool._on_stream("box", protocol.MCP_FRAME, {"stream": 1, "text": "def", "end": True})
        await _until(lambda: got == [b"abcdef\n"])

        # The endpoint's line goes out in pieces, the last marked.
        endpoint.writers[0].write(b"123456\n")
        await endpoint.writers[0].drain()
        await _until(lambda: len(_told(client, protocol.MCP_FRAME)) == 2)
        assert _told(client, protocol.MCP_FRAME) == [
            {"stream": 1, "text": "1234", "end": False},
            {"stream": 1, "text": "56", "end": True},
        ]

        # What is no stream's business is dropped.
        await pool._on_stream("nobody", protocol.MCP_FRAME, {"stream": 1, "text": "x", "end": True})
        await pool._on_stream("box", protocol.MCP_FRAME, {"stream": True, "text": "x", "end": True})
        await pool._on_stream("box", protocol.MCP_FRAME, {"stream": 9, "text": "x", "end": True})
        await pool._on_stream("box", protocol.MCP_FRAME, {"stream": 1, "text": 5, "end": True})
        await asyncio.sleep(0.05)
        assert got == [b"abcdef\n"]

        # The machine closes it: nothing is told back.
        await pool._on_stream("box", protocol.MCP_CLOSE, {"stream": 1, "reason": "the agent closed the server"})
        assert node.streams == {} and _told(client, protocol.MCP_CLOSE) == []
    finally:
        endpoint.close()


async def test_a_stream_the_host_cannot_serve_is_closed_with_the_reason_and_the_link_s_end_takes_the_rest(
    tmp_path, monkeypatch
):
    pool = NodePool()
    client = _live(pool, tmp_path)
    node = pool._nodes["box"]
    path, got, endpoint = await _endpoint()
    try:
        await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 1, "task": "agent", "lend": "d1", "server": "pg"})
        assert _told(client, protocol.MCP_CLOSE) == [{"stream": 1, "reason": "server 'pg' is not lent to that run"}]
        pool.lend("box", "d1", {"pg": str(endpoint.base / "gone.sock")}, task="agent")
        await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 2, "task": "agent", "lend": "d1", "server": "pg"})
        gone = _told(client, protocol.MCP_CLOSE)[-1]
        assert gone["stream"] == 2 and gone["reason"].startswith("the host's endpoint for 'pg' is gone: ")
        assert node.streams == {}

        pool.lend("box", "d1", {"pg": path}, task="agent")
        monkeypatch.setattr(protocol, "MCP_MAX_FRAME", 8)
        # The machine's frame past the cap.
        await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 3, "task": "agent", "lend": "d1", "server": "pg"})
        await pool._on_stream("box", protocol.MCP_FRAME, {"stream": 3, "text": "123456789", "end": False})
        assert 3 not in node.streams
        assert _told(client, protocol.MCP_CLOSE)[-1] == {"stream": 3, "reason": "a frame exceeds 8 bytes"}
        # The endpoint's frame past the cap.
        await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 4, "task": "agent", "lend": "d1", "server": "pg"})
        await _until(lambda: len(endpoint.writers) == 2)
        endpoint.writers[-1].write(b"0123456789\n")
        await endpoint.writers[-1].drain()
        await _until(lambda: 4 not in node.streams)
        assert _told(client, protocol.MCP_CLOSE)[-1] == {"stream": 4, "reason": "frame exceeds 8 bytes"}
        monkeypatch.setattr(protocol, "MCP_MAX_FRAME", 32 * 1024 * 1024)

        # The endpoint closed under the host's write.
        await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 5, "task": "agent", "lend": "d1", "server": "pg"})
        stream = node.streams[5]
        real, stream.writer = (
            stream.writer,
            SimpleNamespace(
                write=lambda data: (_ for _ in ()).throw(ConnectionResetError("closed")), close=lambda: None
            ),
        )
        await pool._on_stream("box", protocol.MCP_FRAME, {"stream": 5, "text": "x", "end": True})
        real.close()
        assert 5 not in node.streams
        assert _told(client, protocol.MCP_CLOSE)[-1] == {"stream": 5, "reason": "the host's endpoint closed"}

        # The link is gone while the endpoint speaks: the stream goes, and nothing can be told.
        await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 6, "task": "agent", "lend": "d1", "server": "pg"})
        told_before = len(client.said)

        async def dead(method: str, params: dict[str, Any]) -> None:
            raise AcpConnectionError("the link is gone")

        client.notify = dead  # type: ignore[method-assign]
        await _until(lambda: len(endpoint.writers) == 4)
        endpoint.writers[-1].write(b"hello\n")
        await endpoint.writers[-1].drain()
        await _until(lambda: 6 not in node.streams)
        assert len(client.said) == told_before
        client.notify = _Client.notify.__get__(client)  # type: ignore[method-assign]

        # The dispatch ends: its streams are told; another dispatch's stay. A machine with no link is nothing to unlend.
        await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 7, "task": "agent", "lend": "d1", "server": "pg"})
        pool.lend("box", "d2", {"pg": path}, task="agent")
        await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 8, "task": "agent", "lend": "d2", "server": "pg"})
        await pool.unlend("box", "d1")
        assert list(node.streams) == [8]
        assert _told(client, protocol.MCP_CLOSE)[-1] == {"stream": 7, "reason": "the dispatch ended"}
        await pool.unlend("nobody", "d1")

        # The link's end takes the rest, with no word down a link that is gone.
        await pool._drop("box")
        assert node.streams == {} and _told(client, protocol.MCP_CLOSE)[-1]["stream"] == 7
    finally:
        endpoint.close()


async def test_the_bridge_argv_is_the_node_s_own_and_a_node_without_a_daemon_names_none(tmp_path):
    pool = NodePool()
    _live(pool, tmp_path)
    assert await pool.bridge_argv("box") is None

    pool._nodes["box"].hello["bridge"] = ["/opt/node/bin/python", "-I", "-m", "raven.node", "--mcp", "--dir", "/n"]

    assert await pool.bridge_argv("box") == ["/opt/node/bin/python", "-I", "-m", "raven.node", "--mcp", "--dir", "/n"]


async def test_an_mcp_stream_cannot_claim_another_tasks_lend(tmp_path):
    pool = NodePool()
    client = _live(pool, tmp_path)
    node = pool._nodes["box"]
    path, got, endpoint = await _endpoint()
    try:
        pool.lend("box", "victim-dispatch", {"pg": path}, task="victim")
        pool.lend("box", "own-dispatch", {"pg": path}, task="attacker")
        await pool._on_stream(
            "box", protocol.MCP_OPEN, {"stream": 1, "task": "attacker", "lend": "victim-dispatch", "server": "pg"}
        )
        assert 1 not in node.streams, "a valid task must not select a different task's host endpoint"
        assert not endpoint.writers and not got, "authorization precedes the host connection"
        await pool._on_stream(
            "box", protocol.MCP_OPEN, {"stream": 2, "task": "attacker", "lend": "own-dispatch", "server": "pg"}
        )
        assert 2 in node.streams, "the same authenticated task may open its own grant"
        await pool.unlend("box", "own-dispatch")
    finally:
        await pool._drop("box")
        endpoint.close()


async def test_an_mcp_stream_without_a_task_is_refused_before_connecting(tmp_path, monkeypatch):
    pool = NodePool()
    client = _live(pool, tmp_path)
    pool.lend("box", "d1", {"pg": "/unused"}, task="agent")

    async def connected(*args, **kwargs):
        pytest.fail("a missing task reached the connection")

    monkeypatch.setattr(asyncio, "open_unix_connection", connected, raising=False)
    await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 1, "lend": "d1", "server": "pg"})
    assert _told(client, protocol.MCP_CLOSE)


@pytest.mark.parametrize("change", ["unlend", "replace", "link"])
async def test_an_mcp_connection_opening_after_its_authorization_ended_is_discarded(tmp_path, monkeypatch, change):
    pool = NodePool()
    client = _live(pool, tmp_path)
    node = pool._nodes["box"]
    pool.lend("box", "d1", {"pg": "/endpoint"}, task="agent")
    entered, resume = asyncio.Event(), asyncio.Event()
    closed = []

    async def waited():
        pass

    writer = SimpleNamespace(close=lambda: closed.append(True), wait_closed=waited)

    async def opening(path):
        assert path == "/endpoint"
        entered.set()
        await resume.wait()
        return asyncio.StreamReader(), writer

    monkeypatch.setattr(asyncio, "open_unix_connection", opening, raising=False)
    monkeypatch.setattr(node_client, "unix_sockets_available", lambda: True)
    waiting = asyncio.create_task(
        pool._on_stream("box", protocol.MCP_OPEN, {"stream": 1, "task": "agent", "lend": "d1", "server": "pg"})
    )
    await asyncio.wait_for(entered.wait(), 2)
    if change == "unlend":
        await pool.unlend("box", "d1")
    elif change == "replace":
        pool.lend("box", "d1", {"pg": "/other"}, task="other-agent")
    else:
        _live(pool, tmp_path)
    resume.set()
    await waiting
    assert closed and node.streams == {}
    assert _told(client, protocol.MCP_CLOSE)[-1]["reason"] == "the dispatch or link ended"


async def test_a_host_without_unix_sockets_refuses_a_stream_in_one_sentence(tmp_path, monkeypatch):
    pool = NodePool()
    client = _live(pool, tmp_path)
    pool.lend("box", "d1", {"pg": "/unused"}, task="agent")
    monkeypatch.delattr(asyncio, "open_unix_connection", raising=False)
    await pool._on_stream("box", protocol.MCP_OPEN, {"stream": 1, "task": "agent", "lend": "d1", "server": "pg"})
    assert _told(client, protocol.MCP_CLOSE) == [
        {"stream": 1, "reason": "the host does not support Unix-socket MCP endpoints"}
    ]
    assert not pool._nodes["box"].streams


def test_lending_requires_a_nonempty_task():
    pool = NodePool()
    with pytest.raises(ValueError, match="agent task"):
        pool.lend("box", "d1", {"pg": "/unused"}, task="")
    assert pool._lent == {}


# ---- a machine registered with a password: the link logs in through askpass ------------


def _password_row() -> dict[str, Any]:
    return {k: v for k, v in row().items() if k != "key"} | {"auth": "password"}


async def test_the_link_to_a_password_machine_logs_in_through_askpass_and_offers_no_key(box, pool, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")  # the mode is refused on Windows first of all
    from raven.ops.connection_add import store_password

    monkeypatch.setenv("RAVEN_HOME", str(box.root / "rhome"))
    box.rows([_password_row()])
    box.password("hunter2")
    store_password("box", "hunter2")
    box.node()

    hello = await pool.hello("box")

    assert hello["digest"] == bundle.digest()
    (argv,) = box.launches()
    assert "-i" not in argv and "PubkeyAuthentication=no" in argv and "hunter2" not in " ".join(argv)


async def test_a_password_the_machine_refuses_ends_the_link_with_the_login_named_and_nothing_else(
    box, pool, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")  # the mode is refused on Windows first of all
    from raven.ops.connection_add import store_password

    monkeypatch.setenv("RAVEN_HOME", str(box.root / "rhome"))
    box.rows([_password_row()])
    box.password("hunter2")
    store_password("box", "wrong-one")
    box.node()

    with pytest.raises(NodeError) as caught:
        await pool.hello("box")

    said = str(caught.value)
    assert "refused the login" in said and "password" in said
    assert HOST not in said and "wrong-one" not in said


async def test_a_password_machine_whose_password_is_not_kept_here_is_refused_before_ssh_is_started(
    box, pool, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")  # the mode is refused on Windows first of all
    monkeypatch.setenv("RAVEN_HOME", str(box.root / "rhome"))
    box.rows([_password_row()])
    box.node()

    with pytest.raises(NodeError, match="no password is kept here for machine 'box'"):
        await pool.hello("box")

    assert box.launches() == [], "ssh never ran"
