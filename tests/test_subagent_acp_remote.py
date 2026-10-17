"""An acp sub-agent whose entry names a registered machine, end to end.

Nothing here needs a second computer. A stand-in ``ssh`` on PATH does what sshd
does with the one remote-command argument it is sent -- hands it to the user's
shell, in that user's home -- so the real ssh line, the real Raven node and the
real stub ACP server the node starts all run: only the network is missing. The
stand-in can also fail the way ssh fails, writing its own address-bearing words
where the ssh line asked (``-E``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.acp_client import remote, remote_runs
from raven.acp_client.capabilities import snapshot_fingerprint, verify_agent
from raven.acp_client.pool import close_pool, get_pool
from raven.acp_client.protocol import AcpConnectionError
from raven.agent.subagent import probe, resume_remote
from raven.agent.subagent.backends import (
    agent_meta,
    build_third_party_backend,
    format_agent_listing,
    session_mcp_effective,
)
from raven.agent.subagent.backends import env as backend_env
from raven.agent.subagent.history import SpawnRecord, nodes_root
from raven.agent.subagent.mcp_grant import GrantedServer, McpGrant
from raven.agent.subagent.probe_state import fingerprint
from raven.agent.subagent.registry import _row_for
from raven.config.schema import MCPServerConfig, ThirdPartyAcpSubagentConfig
from raven.config.update_subagents import reject_unsupported_acp_fields
from raven.mcp.bridge import unix_sockets_available
from raven.node import bundle
from raven.node.client import NodePool
from raven.ops import remote_path

_STUB = Path(__file__).with_name("acp_stub_server.py")
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
launch = "-T" in args  # a held session; the one-shot runner sends no -T
log = args[args.index("-E") + 1] if "-E" in args else None

def fail(said):
    if log:
        open(log, "a").write(said + "\\n")
    else:
        sys.stderr.write(said + "\\n")
    sys.exit(255)

if mode == "unreachable":
    fail("ssh: connect to host {host} port {port}: Operation timed out")
if mode == "launch_denied" and launch:
    fail("worker@{host}: Permission denied (publickey).")
command = args[args.index("--") + 1] if "--" in args else args[-1]
os.chdir(home)
# A daemon these tests start leaves soon after they do; the relay reads the idle time from its environment.
os.execve("/bin/sh", ["/bin/sh", "-c", command], {{"HOME": home, "SHELL": "/bin/sh", "PATH": os.environ["PATH"], "RAVEN_NODE_IDLE_S": "2"}})
"""


# Every dispatch, verify and ping here starts a real node over the stand-in
# ssh, and the agent through it: real processes, real seconds.
pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
async def _no_pooled_connections():
    yield
    await close_pool()


class Machine:
    """The stand-in machine: its home, its ssh, and what was asked of it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = root / "box-home"
        self.home.mkdir()
        self.bin = root / "fakebin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text(_FAKE_SSH.format(python=sys.executable, host=HOST, port=PORT), encoding="utf-8")
        ssh.chmod(0o755)
        (self.bin / "home").write_text(str(self.home), encoding="utf-8")
        self.mode("ok")
        registry = root / "connections.json"
        registry.write_text(
            json.dumps(
                {
                    "connections": [
                        {
                            "id": "box",
                            "display_name": "Lab box",
                            "host": HOST,
                            "port": PORT,
                            "user": "worker",
                            "key": "/keys/id_test",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("RAVEN_CONNECTIONS", str(registry))
        path = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
        # Both ways ssh is started: the one-shot runner inherits this process's
        # PATH, and an agent's launch takes the captured login-shell one.
        monkeypatch.setenv("PATH", path)
        monkeypatch.setattr(backend_env, "_LOGIN_ENV", {**os.environ, "PATH": path})
        monkeypatch.setattr("raven.config.paths.get_logs_dir", lambda: root / "logs")
        # This Raven's node, installed where the host looks for it: its
        # ``bin/python`` is this test run's own interpreter.
        python = self.home / ".raven-node" / bundle.install_name() / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        # The link to the machine, as assembly hands it to the file tools and
        # the dispatch: one pool for this test, closed with it.
        self.nodes = NodePool()
        monkeypatch.setattr(remote_path, "_NODES", lambda: self.nodes)

    def mode(self, mode: str) -> None:
        (self.bin / "mode").write_text(mode, encoding="utf-8")

    def calls(self) -> list[list[str]]:
        path = self.bin / "calls"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def daemons(self) -> list[int]:
        """The daemons serving this machine's node directory, as the process table shows them.

        ``-ww``: ps cuts a command line at the terminal's width otherwise, and
        the daemon's runs past 80 columns before the ``--dir`` that names
        this machine, so a narrow run listed none of them.
        """
        mark = f"--serve --dir {self.home / '.raven-node'}"
        listed = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
        return [int(line.split(None, 1)[0]) for line in listed.splitlines() if mark in line]

    def agents_left(self) -> list[int]:
        """The agents still under this machine's daemons: what a test must not leave behind."""
        out: list[int] = []
        for daemon in self.daemons():
            children = subprocess.run(["pgrep", "-P", str(daemon)], capture_output=True, text=True).stdout
            out += [int(pid) for pid in children.split()]
        return out


@pytest.fixture
async def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not Path("/bin/sh").exists():
        pytest.skip("the stand-in ssh hands the ssh line to a POSIX shell")
    machine = Machine(tmp_path, monkeypatch)
    yield machine
    # The pool before the links, as the host's own stop orders them: an agent
    # is ended through its link, and a link closed first leaves its agent with
    # the daemon -- right for a host, a leak for a test, whose daemon then
    # never goes idle. The links the test itself closed are covered by the
    # check after: an agent still under a daemon fails the test and is ended.
    await close_pool()
    await machine.nodes.close_all()
    deadline = time.monotonic() + 3.0
    while (left := machine.agents_left()) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    for daemon in machine.daemons():
        with contextlib.suppress(ProcessLookupError):
            os.kill(daemon, signal.SIGTERM)
    assert not left, f"agents left running under the stand-in machine's daemon: {left}"


async def test_the_daemon_lookup_reads_whole_command_lines_whatever_the_terminal_width(box, monkeypatch):
    """A stand-in daemon whose command line runs past 80 columns is still found, and so can still be ended."""
    monkeypatch.setenv("COLUMNS", "80")
    mark = f"--serve --dir {box.home / '.raven-node'}"
    padding = "x" * 120
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", padding, *mark.split(), stdout=asyncio.subprocess.DEVNULL
    )
    try:
        assert proc.pid in box.daemons(), "the mark sits past the 80th column of the command line"
    finally:
        proc.kill()
        await proc.wait()
    assert proc.pid not in box.daemons()


def _agent_pid(name: str = "stub@box") -> int | None:
    """The pid of the agent process on the machine, read off the pooled connection's endpoint."""
    (connection,) = get_pool().connections(name)
    return connection.client._endpoint.pid


def _alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _hold_launch_answers(monkeypatch: pytest.MonkeyPatch, answered: asyncio.Event) -> None:
    """``agent/launch`` answers wait half a second once they are in: a cancel in that wait
    lands before the answer reaches ``NodePool.open_channel``, with the agent already running."""
    from raven.acp_client.client import AcpClient
    from raven.node import protocol

    original = AcpClient.request

    async def held(self, method: str, *args: Any, **kwargs: Any) -> Any:
        answer = await original(self, method, *args, **kwargs)
        if method == protocol.AGENT_LAUNCH:
            answered.set()
            await asyncio.sleep(0.5)
        return answer

    monkeypatch.setattr(AcpClient, "request", held)


def remote_config(*, mode: str = "ok", command: str | None = None, **kw: Any) -> ThirdPartyAcpSubagentConfig:
    return ThirdPartyAcpSubagentConfig(
        name=kw.pop("name", "stub@box"),
        command=command or f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": mode, **kw.pop("env", {})},
        machine="box",
        remote_cwd=kw.pop("remote_cwd", "~/raven-work"),
        ready_timeout_ms=kw.pop("ready_timeout_ms", 15000),
        **kw,
    )


# ---- a dispatch ----------------------------------------------------------------


async def test_a_dispatch_runs_on_the_machine_in_a_directory_made_there_and_says_where(box, tmp_path):
    backend = build_third_party_backend(remote_config())
    local = tmp_path / "local-workspace"

    reply = await backend.run("hi", task_id="t1", workspace=local, executor=None, instance="reviewer")

    there = box.home / "raven-work" / "reviewer"
    assert there.is_dir(), "the session directory is made on the machine"
    assert "[raven] Ran on machine 'Lab box' (box), in " in reply
    # Shown the way scp writes it.
    assert f"in scp://worker@{HOST}:{PORT}//{str(there.resolve()).lstrip('/')};" in reply
    where = reply.split("in scp://", 1)[1].split(";", 1)[0]
    assert where.split("@", 1)[1].split(":", 1)[0] == HOST, "the address a model reads is the registry's, never the id"
    assert "not on this computer" in reply
    assert "the files it wrote are there, not on this computer" in reply
    assert "read_file, list_dir, find, grep, write_file and edit_file take that path as it is" in reply
    assert not local.exists(), "the caller's workspace is never touched"
    for argv in box.calls():
        assert str(local) not in " ".join(argv), "this computer's workspace path never reaches the machine"


async def test_the_entry_s_env_reaches_the_agent_over_the_link_and_never_an_ssh_argument(box, tmp_path):
    # env is where credentials live, and an argument is in the process list
    # at both ends. The stub answers in the mode its env names, so an echo
    # proves the variables arrived; no ssh argv may carry any of them.
    cfg = remote_config(mode="echo_blocks")
    cfg = cfg.model_copy(update={"env": {**cfg.env, "OPENAI_API_KEY": "sk-raven-test-not-real"}})
    backend = build_third_party_backend(cfg)

    reply = await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, model="parent-model")

    assert json.loads(reply.split("\n\n[raven]")[0]) == [{"type": "text", "text": "hi"}]
    (launch,) = [argv for argv in box.calls() if "-T" in argv]
    for argv in box.calls():
        joined = " ".join(argv)
        assert "sk-raven-test-not-real" not in joined and "ACP_STUB_MODE" not in joined
    remote = launch[launch.index("--") + 1]
    assert "RAVEN_PARENT_MODEL" not in remote, "the parent binding is this computer's process env"
    assert "raven.node --attach --dir" in remote, "the one ssh is the relay to the machine's daemon"
    assert "-E" in launch, "ssh's own messages go to their log"


async def test_an_edited_env_value_relaunches_the_agent_on_the_same_link(box, tmp_path):
    # The values travel on the link, and the pool keys on them: an edit must
    # not be answered by the agent process started before it.
    first = build_third_party_backend(remote_config())
    await first.run("one", task_id="t1", workspace=tmp_path, executor=None)
    before = _agent_pid()
    edited = remote_config().model_copy(update={"env": {"ACP_STUB_MODE": "ok", "EXTRA": "2"}})
    await build_third_party_backend(edited).run("two", task_id="t2", workspace=tmp_path, executor=None)

    assert _agent_pid() != before, "a new agent process"
    assert len([argv for argv in box.calls() if "-T" in argv]) == 1, "on the one link"


async def test_two_handles_on_one_machine_work_in_two_directories(box, tmp_path):
    backend = build_third_party_backend(remote_config())

    await backend.run("one", task_id="t1", workspace=tmp_path, executor=None, instance="a")
    await backend.run("two", task_id="t2", workspace=tmp_path, executor=None, instance="b")

    assert (box.home / "raven-work" / "a").is_dir()
    assert (box.home / "raven-work" / "b").is_dir()


async def test_an_unreachable_machine_fails_the_dispatch_in_one_sentence(box, tmp_path):
    box.mode("unreachable")
    backend = build_third_party_backend(remote_config())

    with pytest.raises(AcpConnectionError) as caught:
        await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)

    said = str(caught.value)
    assert "machine 'Lab box' (box) did not answer within the connection timeout" in said
    assert HOST not in said and str(PORT) not in said


async def test_a_launch_ssh_refuses_is_read_from_its_own_log_not_its_stderr(box, tmp_path):
    box.mode("launch_denied")
    backend = build_third_party_backend(remote_config())

    with pytest.raises(AcpConnectionError) as caught:
        await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)

    said = str(caught.value)
    assert "refused the login" in said
    assert HOST not in said


async def test_an_agent_not_installed_on_the_machine_says_so(box, tmp_path):
    backend = build_third_party_backend(remote_config(command="no-such-agent-for-raven-tests --acp"))

    with pytest.raises(AcpConnectionError, match="command was not found on machine 'Lab box'"):
        await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)

    snapshot = await verify_agent(remote_config(command="no-such-agent-for-raven-tests --acp"))
    assert snapshot.status == "attention" and "command was not found on machine 'Lab box'" in snapshot.detail


async def test_an_unregistered_machine_fails_before_anything_starts(box, tmp_path):
    backend = build_third_party_backend(remote_config().model_copy(update={"machine": "elsewhere"}))

    with pytest.raises(AcpConnectionError, match="machine 'elsewhere' is not registered"):
        await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)
    assert box.calls() == []


async def test_two_parent_models_share_one_agent_process_rather_than_starting_one_each(box, tmp_path):
    # The parent binding is launch environment for a process of raven's own;
    # carried, each parent model would start an agent process on the machine.
    backend = build_third_party_backend(remote_config())

    await backend.run("one", task_id="t1", workspace=tmp_path, executor=None, model="parent-a")
    first = _agent_pid()
    await backend.run("two", task_id="t2", workspace=tmp_path, executor=None, model="parent-b")

    assert _agent_pid() == first
    assert len([argv for argv in box.calls() if "-T" in argv]) == 1


async def test_this_computer_s_files_are_not_accounted_for_a_remote_turn(box, tmp_path, monkeypatch):
    from raven.acp_client import acp_agent

    built: list[dict[str, Any]] = []
    real = acp_agent._TurnCollector.__init__

    def spy(self, *args, **kwargs):
        built.append(kwargs)
        real(self, *args, **kwargs)

    monkeypatch.setattr(acp_agent._TurnCollector, "__init__", spy)
    backend = build_third_party_backend(remote_config())

    await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)

    assert built and built[0]["track_files"] is False and built[0]["workspace"] is None


async def test_the_node_s_ssh_starts_from_an_empty_log(box):
    from raven.acp_client import remote

    log = remote.ssh_log_path("raven-node@box")
    log.write_text("worker@host: Permission denied (publickey).\n", encoding="utf-8")

    hello = await box.nodes.hello("box")

    assert hello["digest"] == bundle.digest()
    assert log.read_text(encoding="utf-8") == "", "a failure is read from this launch's lines"


async def test_a_connection_that_ends_mid_turn_is_reported_as_it_ended(box, tmp_path):
    # Past ssh: the agent itself exits during the turn, so there is no ssh
    # failure to classify and the connection's own report stands.
    backend = build_third_party_backend(remote_config(mode="elicits_then_dies"))

    with pytest.raises(AcpConnectionError, match="connection ended"):
        await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)


@pytest.mark.skipif(
    not unix_sockets_available(), reason="MCP lending rides on unix sockets, which this computer has not"
)
async def test_granted_mcp_servers_are_lent_through_the_node_and_named_in_the_reply(box, tmp_path):
    backend = build_third_party_backend(remote_config(mode="echo_blocks"))
    grant = McpGrant(granted=(GrantedServer("pg", "stdio", MCPServerConfig(command="/bin/true")),))

    reply = await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, mcp_grant=grant)

    assert "MCP servers 'pg' were lent to acp agent 'stub@box' through the Raven node on machine 'box'" in reply
    assert "Ran on machine 'Lab box' (box)" in reply


_HANDOFF_AGENT = Path(__file__).with_name("mcp_handoff_acp_stub.py")
_HANDOFF_SERVER = Path(__file__).with_name("mcp_handoff_stub_server.py")


@pytest.mark.skipif(
    not unix_sockets_available(), reason="MCP lending rides on unix sockets, which this computer has not"
)
async def test_an_mcp_server_the_host_holds_is_reached_from_the_machine_through_its_node(box, tmp_path):
    """The agent there runs the node's bridge as a stdio server; the call reaches the server here, and its answer goes back."""
    ledger, receipt = tmp_path / "ledger.jsonl", tmp_path / "receipt.jsonl"
    upstream = MCPServerConfig(
        command=sys.executable, args=[str(_HANDOFF_SERVER)], env={"MCP_HANDOFF_LEDGER": str(ledger)}
    )
    grant = McpGrant(granted=(GrantedServer("probe", "stdio", upstream),))
    cfg = remote_config(command=f"{sys.executable} {_HANDOFF_AGENT}", env={"ACP_HANDOFF_RECEIPT": str(receipt)})

    reply = await build_third_party_backend(cfg).run(
        "marker-42", task_id="t1", workspace=tmp_path, executor=None, mcp_grant=grant
    )

    assert reply.split("\n\n[raven]")[0] == "MCP_HANDOFF_OK:marker-42", "the tool's answer, through the node"
    assert "MCP servers 'probe' were lent to acp agent 'stub@box' through the Raven node on machine 'box'" in reply
    calls = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert calls == [{"tool": "handoff_probe", "marker": "marker-42"}], "the server ran here, once"
    (handed,) = [json.loads(line) for line in receipt.read_text(encoding="utf-8").splitlines()]
    (stanza,) = handed["mcpServers"]
    assert stanza["name"] == "probe" and stanza["env"] == [] and stanza["command"] == sys.executable
    assert stanza["args"][:4] == ["-I", "-m", "raven.node", "--mcp"] and stanza["args"][-2:] == ["--server", "probe"]
    assert stanza["args"][stanza["args"].index("--lend") + 1] == "t1" and "--task" in stanza["args"]
    assert str(_HANDOFF_SERVER) not in json.dumps(stanza) and ".sock" not in json.dumps(stanza), (
        "nothing of the server's definition reaches the machine"
    )
    root = stanza["args"][stanza["args"].index("--dir") + 1]
    assert Path(root).is_relative_to(box.home), "the machine's own node directory"
    listed = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    assert f"--mcp --dir {root}" not in listed, "no bridge outlives the dispatch"


# ---- what it is given, and how ----------------------------------------------------


@pytest.mark.skipif(
    not unix_sockets_available(), reason="MCP lending rides on unix sockets, which this computer has not"
)
def test_mcp_is_lent_to_an_agent_on_a_machine_through_its_node_and_the_reply_says_so():
    cfg = remote_config()
    backend = build_third_party_backend(cfg)
    grant = McpGrant(granted=(GrantedServer("pg", "stdio", MCPServerConfig(command="/bin/true")),))

    assert not backend._session_mcp_refused
    assert "were lent to acp agent 'stub@box' through the Raven node on machine 'box'" in backend._mcp_note(
        grant, ("pg",)
    )
    assert "carries no MCP streams" in backend._mcp_note(grant, ())
    assert backend._mcp_note(grant) == "", "a run here says nothing of lending"
    assert session_mcp_effective(cfg) is True
    assert _row_for(cfg).injectable.mcps is True


def test_the_roster_names_the_machine_and_hands_it_no_paths():
    cfg = remote_config()
    meta = agent_meta(cfg)

    assert meta.machine == "box"
    assert meta.reads_local_files is False
    listing = format_agent_listing([meta])
    assert "on machine box" in listing and "no-local-files" in listing
    assert HOST not in listing
    assert _row_for(cfg).caps.machine == "box"
    assert _row_for(cfg).meta().machine == "box"


def test_a_local_row_is_listed_as_before():
    cfg = ThirdPartyAcpSubagentConfig(name="here", command="qwen --acp")

    meta = agent_meta(cfg)
    assert meta.machine == "" and meta.reads_local_files is True
    assert "on machine" not in format_agent_listing([meta])


def test_raven_s_keys_are_not_lent_to_a_machine_row():
    loaded = ThirdPartyAcpSubagentConfig(name="x", command="pi", machine="box", lend_keys=["openrouter"])
    assert loaded.lend_keys == [], "dropped on load, with a warning"

    with pytest.raises(ValueError, match="runs on machine 'box'"):
        reject_unsupported_acp_fields(
            [{"kind": "acp", "name": "x", "command": "pi", "machine": "box", "lendKeys": ["openrouter"]}]
        )


def test_moving_a_row_to_a_machine_is_a_new_launch_and_a_local_row_s_digest_is_untouched():
    here = ThirdPartyAcpSubagentConfig(name="a", command="qwen --acp")
    there = here.model_copy(update={"machine": "box"})
    elsewhere_root = there.model_copy(update={"remote_cwd": "/srv/agents"})

    assert len({snapshot_fingerprint(c) for c in (here, there, elsewhere_root)}) == 3
    assert len({fingerprint(c) for c in (here, there, elsewhere_root)}) == 3
    # A row that names no machine digests exactly as it did before the fields
    # existed, so no recorded measurement is discarded by the upgrade.
    assert snapshot_fingerprint(here) == snapshot_fingerprint(here.model_copy(update={"remote_cwd": None}))


# ---- checks -------------------------------------------------------------------------


async def test_the_free_probe_does_not_reach_the_machine(box):
    result = await probe.probe_one(remote_config(), source="config")

    assert result.status == "attention"
    assert "runs on machine 'Lab box' (box)" in result.detail
    assert "run a test" in result.detail
    assert box.calls() == [], "a listing never opens an ssh connection"


async def test_the_free_probe_shows_the_link_s_state_once_a_dispatch_has_opened_it(box, tmp_path):
    cfg = remote_config()
    await build_third_party_backend(cfg).run("hi", task_id="t1", workspace=tmp_path, executor=None)

    result = await probe.probe_one(cfg, source="config")

    assert "runs on machine 'Lab box' (box) (connected, 1 agent running)" in result.detail
    await close_pool()
    # The agent's exit reaches the link a beat after the close that asked for it; the listing reads the link.
    deadline = time.monotonic() + 10
    while box.nodes.status("box").agents and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert "(connected, idle)" in (await probe.probe_one(cfg, source="config")).detail


async def test_a_link_that_goes_mid_turn_is_waited_out_and_the_turn_finishes_when_it_is_back(box, tmp_path):
    """The ssh is the way to the machine, not the agent's life: the agent answers after the link is back."""
    box.nodes._heartbeat_s = 0.1
    box.nodes._lost_after_s = 1.0
    box.nodes._backoff_s = (0.2,)
    backend = build_third_party_backend(remote_config(env={"ACP_STUB_PROMPT_DELAY_S": "4"}))
    turn = asyncio.ensure_future(backend.run("hi", task_id="t1", workspace=tmp_path, executor=None))
    deadline = time.monotonic() + 20
    while box.nodes.status("box").agents != 1:
        assert time.monotonic() < deadline and not turn.done(), "the agent never started"
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)
    daemon = box.nodes._nodes["box"].hello["pid"]

    os.kill(box.nodes._nodes["box"].client._proc.pid, signal.SIGKILL)

    reply = await asyncio.wait_for(turn, timeout=30)
    assert reply.split("\n\n[raven]")[0] == "pong", "the agent's answer, through the link that came back"
    assert box.nodes._nodes["box"].hello["pid"] == daemon, "the same daemon, reached by a new ssh"
    assert len([argv for argv in box.calls() if "-T" in argv]) == 2


class _Manager:
    """What the pick-up asks of the sub-agent manager, recorded."""

    def __init__(self, backend: Any, session_dir: Path) -> None:
        self.registry = SimpleNamespace(backend=lambda name: backend if name == backend.name else None)
        self.session_dir = session_dir
        self.announced: list[dict[str, Any]] = []
        self.finalized: list[str] = []

    def session_dir_for(self, session_key: str) -> Path:
        return self.session_dir

    async def _finalize_node_claim(self, session_key: str, record: Any) -> None:
        self.finalized.append(record.node_id)

    def remember_session(self, session_key: str) -> None:
        pass

    async def _announce_result(self, task_id, summary, task, result, origin, status, record_path=None, activity=None):
        self.announced.append({"task_id": task_id, "result": result, "status": status, "origin": origin})


class _Registry:
    def __init__(self) -> None:
        self.rows: list[tuple[str, ...]] = []

    async def commit(self, session_key, agent, handle, agent_id, *, kind="cli"):
        self.rows.append(("commit", session_key, agent, handle, agent_id))

    async def bind_if_absent(self, session_key, agent, handle, agent_id, *, kind="cli"):
        self.rows.append(("bind", session_key, agent, handle, agent_id))
        return agent_id

    async def upsert_spawn(self, session_key, agent, handle, status, agent_id=None):
        self.rows.append(("status", session_key, agent, handle, status))


async def test_a_run_left_on_the_machine_when_this_computer_stops_is_picked_up_by_the_next_start(
    box, tmp_path, monkeypatch
):
    """The agent stays with the machine's daemon when this process stops; the next one reads its answer and reports it."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    backend = build_third_party_backend(remote_config(env={"ACP_STUB_PROMPT_DELAY_S": "3"}))
    turn = asyncio.ensure_future(
        backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, session_key="tui:s1")
    )
    deadline = time.monotonic() + 20
    while not any(run.prompt_id > 0 for run in remote_runs.pending()):
        assert time.monotonic() < deadline and not turn.done(), "the prompt never went out"
        await asyncio.sleep(0.05)
    agent = _agent_pid()
    (run,) = remote_runs.pending()
    assert (run.machine, run.agent, run.session_key, run.handle, run.task_id) == (
        "box",
        "stub@box",
        "tui:s1",
        "t1",
        "t1",
    )
    assert run.session_id and run.initialize and run.next_id == run.prompt_id + 1

    # This computer stops: the turn is cancelled the way the shutdown does it, then the pools are closed.
    remote.leave_agents_running()
    try:
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        assert remote_runs.was_left("t1"), "the manager would read this dispatch as left, not cancelled"
        await close_pool()
        await box.nodes.close_all()
    finally:
        remote.staying()
    assert _alive(agent), "the agent stays with the machine's daemon"
    assert [kept.task for kept in remote_runs.pending()] == [run.task]

    # The next start: the dispatch's record is still open under its session, as the manager leaves one.
    session_dir = tmp_path / "sessions" / "s1"
    meta = {"call_id": "n1", "session_key": "tui:s1", "agent": backend.name, "handle": "t1", "task_summary": "say hi"}
    SpawnRecord.open(session_dir, task_id="t1", task="hi", meta=meta, node_id="n1")
    manager = _Manager(backend, session_dir)
    registry = _Registry()
    monkeypatch.setattr(resume_remote, "get_registry", lambda: registry)

    assert await asyncio.wait_for(resume_remote.resume_remote_runs(manager), timeout=30) == 1

    (said,) = manager.announced
    assert said["status"] == "ok" and said["task_id"] == "t1" and said["origin"]["chat_id"] == "s1"
    assert said["result"].split("\n\n[raven]")[0] == "pong", "the agent's answer, read after the restart"
    assert "picked up after this computer restarted" in said["result"]
    meta_back = json.loads((nodes_root(session_dir) / "n1.meta.json").read_text(encoding="utf-8"))
    assert meta_back["status"] == "completed" and manager.finalized == ["n1"]
    assert (nodes_root(session_dir) / "n1.out.md").read_text(encoding="utf-8").startswith("pong")
    assert registry.rows == [("status", "tui:s1", "stub@box", "t1", "completed")]
    assert remote_runs.pending() == []
    assert _agent_pid() == agent, "the agent's process is the pool's connection again"


async def test_a_stop_that_lands_under_a_pick_up_leaves_the_run_for_the_start_after(box, tmp_path, monkeypatch):
    """This computer stops again while a pick-up is attached: the record and the agent stay, and the next start reads the answer."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    backend = build_third_party_backend(remote_config(env={"ACP_STUB_PROMPT_DELAY_S": "4"}))
    turn = asyncio.ensure_future(
        backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, session_key="tui:s1")
    )
    deadline = time.monotonic() + 20
    while not any(run.prompt_id > 0 for run in remote_runs.pending()):
        assert time.monotonic() < deadline and not turn.done(), "the prompt never went out"
        await asyncio.sleep(0.05)
    agent = _agent_pid()
    remote.leave_agents_running()
    try:
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        remote_runs.was_left("t1")
        await close_pool()
        await box.nodes.close_all()
    finally:
        remote.staying()
    (run,) = remote_runs.pending()
    session_dir = tmp_path / "sessions" / "s1"
    meta = {"call_id": "n1", "session_key": "tui:s1", "agent": backend.name, "handle": "t1", "task_summary": "say hi"}
    SpawnRecord.open(session_dir, task_id="t1", task="hi", meta=meta, node_id="n1")
    manager = _Manager(backend, session_dir)
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())

    # The next start begins the pick-up, and stops the moment its attach is on the machine.
    picking = asyncio.ensure_future(resume_remote.resume_remote_runs(manager))
    deadline = time.monotonic() + 10
    while not any(row.get("task") == run.task and row.get("attached") for row in await box.nodes.tasks("box")):
        assert time.monotonic() < deadline and not picking.done(), "the pick-up never attached"
        await asyncio.sleep(0.05)
    remote.leave_agents_running()
    try:
        await close_pool()
        await box.nodes.close_all()
        assert await asyncio.wait_for(picking, timeout=10) == 0
    finally:
        remote.staying()
    assert manager.announced == [], "nothing is reported for a run this computer did not finish reading"
    assert [kept.task for kept in remote_runs.pending()] == [run.task], "the record is the next start's"
    assert _alive(agent), "the agent stays with the machine's daemon"

    # The start after that reads the answer.
    assert await asyncio.wait_for(resume_remote.resume_remote_runs(manager), timeout=30) == 1
    (said,) = manager.announced
    assert said["status"] == "ok" and said["result"].split("\n\n[raven]")[0] == "pong"
    assert remote_runs.pending() == [] and _agent_pid() == agent


async def test_a_cancel_while_the_acp_launch_answer_waits_ends_the_agent_too(box, tmp_path, monkeypatch):
    """The turn is cancelled after the daemon started its agent but before ``agent/launch``'s answer
    reaches :meth:`NodePool.open_channel`: the agent is ended and the channel dropped all the same."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    answered = asyncio.Event()
    _hold_launch_answers(monkeypatch, answered)
    backend = build_third_party_backend(remote_config())
    turn = asyncio.ensure_future(
        backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, session_key="tui:s1")
    )
    deadline = time.monotonic() + 20
    while not box.agents_left():
        assert time.monotonic() < deadline and not turn.done(), "the agent never started"
        await asyncio.sleep(0.01)
    await asyncio.wait_for(answered.wait(), 10)

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    deadline = time.monotonic() + 10
    while box.agents_left():
        assert time.monotonic() < deadline, "the agent stayed on the machine"
        await asyncio.sleep(0.05)
    assert box.nodes._nodes["box"].channels == {}, "the channel of a launch left waiting is dropped"


async def test_a_reply_the_caller_reports_keeps_its_record_and_a_host_that_stops_before_the_announce_reads_it_again(
    box, tmp_path, monkeypatch
):
    """The manager announces the reply after the backend returns it: the record stays until then, at the prompt, so the next start replays the turn whole."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    backend = build_third_party_backend(remote_config())
    await backend.run("hi", task_id="t0", workspace=tmp_path, executor=None, session_key="tui:s1")
    assert remote_runs.pending() == [], "a caller that takes the reply as its own result leaves no record"

    reply = await backend.run(
        "hi", task_id="t1", workspace=tmp_path, executor=None, session_key="tui:s1", reported_by_caller=True
    )

    assert reply.split("\n\n[raven]")[0] == "pong"
    agent = _agent_pid()
    (run,) = remote_runs.pending()
    assert run.task_id == "t1" and run.prompt_id > 0, "the record outlives the reply, for the manager to forget"
    (connection,) = get_pool().connections(backend.name)
    assert run.received < connection.client.endpoint.received, (
        "and it stayed at the prompt: the turn is read again whole"
    )

    # This computer stops between the reply and its announce.
    remote.leave_agents_running()
    try:
        await close_pool()
        await box.nodes.close_all()
    finally:
        remote.staying()
    assert _alive(agent)
    session_dir = tmp_path / "sessions" / "s1"
    meta = {"call_id": "n1", "session_key": "tui:s1", "agent": backend.name, "handle": "t1", "task_summary": "say hi"}
    SpawnRecord.open(session_dir, task_id="t1", task="hi", meta=meta, node_id="n1")
    manager = _Manager(backend, session_dir)
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())

    assert await asyncio.wait_for(resume_remote.resume_remote_runs(manager), timeout=30) == 1

    (said,) = manager.announced
    assert said["status"] == "ok" and said["result"].split("\n\n[raven]")[0] == "pong", "the whole reply, read again"
    assert remote_runs.pending() == [] and _agent_pid() == agent


async def test_the_pool_closed_before_the_links_leaves_no_agent_and_the_daemon_goes_idle(box, tmp_path):
    """What the fixture's teardown relies on: ended through its link, the agent goes, and the daemon leaves on its own."""
    backend = build_third_party_backend(remote_config())
    await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)
    agent = _agent_pid()
    (daemon,) = box.daemons()
    assert box.agents_left() == [agent]

    await close_pool()
    await box.nodes.close_all()

    deadline = time.monotonic() + 3.0
    while _alive(agent) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _alive(agent) and box.agents_left() == []
    # Idle two seconds after the last link closed (the stand-in ssh's RAVEN_NODE_IDLE_S), checked every second.
    deadline = time.monotonic() + 8.0
    while daemon in box.daemons() and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert daemon not in box.daemons(), "the daemon stays only while an agent runs or a link is open"


async def test_a_daemon_that_stops_mid_turn_fails_the_turn_in_one_sentence(box, tmp_path):
    box.nodes._heartbeat_s = 0.1
    box.nodes._lost_after_s = 1.0
    box.nodes._backoff_s = (0.2,)
    backend = build_third_party_backend(remote_config(mode="prompt_hangs"))
    turn = asyncio.ensure_future(backend.run("hi", task_id="t1", workspace=tmp_path, executor=None))
    deadline = time.monotonic() + 20
    while box.nodes.status("box").agents != 1:
        assert time.monotonic() < deadline and not turn.done(), "the agent never started"
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)

    os.kill(box.nodes._nodes["box"].hello["pid"], signal.SIGTERM)

    with pytest.raises(AcpConnectionError) as caught:
        await asyncio.wait_for(turn, timeout=30)
    said = str(caught.value)
    assert said.startswith("acp agent 'stub@box'") and "connection ended" in said
    assert HOST not in said and str(PORT) not in said


async def test_the_free_probe_names_a_machine_that_is_gone(box):
    result = await probe.probe_one(remote_config().model_copy(update={"machine": "elsewhere"}), source="config")

    assert result.status == "attention"
    assert "machine 'elsewhere' is not registered" in result.detail


async def test_verify_handshakes_on_the_machine_in_the_one_check_directory(box):
    snapshot = await verify_agent(remote_config())

    assert snapshot.status == "ready", snapshot.detail
    assert (box.home / "raven-work" / "raven-check").is_dir()


async def test_verify_starts_the_agent_with_the_entry_s_env(box):
    # The stub refuses session/new when its env names that mode; started without
    # the variables it would default to answering, and the check would read ready.
    snapshot = await verify_agent(remote_config(mode="no_session"))

    assert snapshot.status == "attention"
    assert "no session could be opened" in snapshot.detail


async def test_verify_of_an_unreachable_machine_is_attention_never_missing(box):
    box.mode("unreachable")

    snapshot = await verify_agent(remote_config())

    assert snapshot.status == "attention", "missing would offer an installer that runs on this computer"
    assert "did not answer within the connection timeout" in snapshot.detail
    assert HOST not in snapshot.detail


async def test_verify_of_a_launch_ssh_refuses_says_so(box):
    box.mode("launch_denied")

    snapshot = await verify_agent(remote_config())

    assert snapshot.status == "attention"
    assert "refused the login" in snapshot.detail
    assert HOST not in snapshot.detail


async def test_verify_of_a_root_the_machine_cannot_make_is_attention_in_its_own_words(box):
    # Measured 2026-10-10: a registered machine with a full disk, where the
    # root's mkdir fails before the agent starts. The node says why.
    snapshot = await verify_agent(remote_config(remote_cwd="/proc/raven-cannot-make-this"))

    assert snapshot.status == "attention", "missing would offer an installer that runs on this computer"
    assert "could not make /proc/raven-cannot-make-this" in snapshot.detail
    assert HOST not in snapshot.detail


async def test_verify_of_an_unregistered_machine_names_it(box):
    snapshot = await verify_agent(remote_config().model_copy(update={"machine": "elsewhere"}))

    assert snapshot.status == "attention"
    assert "machine 'elsewhere' is not registered" in snapshot.detail
    assert box.calls() == []


async def test_verify_that_cannot_make_its_directory_says_so(box):
    (box.home / "raven-work").write_text("a file where the root should be\n", encoding="utf-8")

    snapshot = await verify_agent(remote_config())

    assert snapshot.status == "attention"
    assert "could not make" in snapshot.detail and HOST not in snapshot.detail


async def test_verify_with_no_ssh_here_is_attention_with_the_reason(box, monkeypatch):
    # A PATH holding no ssh at all, so nothing falls through to the real one.
    (box.bin / "ssh").unlink()
    monkeypatch.setattr(backend_env, "_LOGIN_ENV", {**os.environ, "PATH": str(box.bin)})

    snapshot = await verify_agent(remote_config())

    assert snapshot.status == "attention"
    assert "ssh" in snapshot.detail


async def test_the_free_probe_reads_the_recorded_verdict_until_the_entry_changes(box):
    cfg = remote_config()
    await probe.record_capabilities(cfg)

    recorded = await probe.probe_one(cfg, source="config")
    moved = await probe.probe_one(cfg.model_copy(update={"remote_cwd": "/srv/elsewhere"}), source="config")

    assert recorded.status == "ready", recorded.detail
    assert moved.status == "attention" and "launch config changed" in moved.detail
    assert [argv for argv in box.calls() if "-T" in argv], "only the recording reached the machine"


async def test_a_node_whose_ssh_log_cannot_be_emptied_still_starts(box):
    from raven.acp_client import remote

    remote.ssh_log_path("raven-node@box").mkdir()

    assert (await box.nodes.hello("box"))["digest"] == bundle.digest()


async def test_a_ping_answers_from_the_machine_and_leaves_one_directory(box):
    result = await probe.ping_agent(remote_config())

    assert result.ok, result.detail
    made = sorted(p.name for p in (box.home / "raven-work").iterdir())
    assert made == ["raven-check"], "every check reuses one directory instead of one per press"


async def test_a_failed_ping_on_a_machine_offers_no_fix_to_run_here(box):
    box.mode("launch_denied")

    result = await probe.ping_agent(remote_config())

    assert result.ok is False
    assert result.remedy is None
    assert "on machine 'box'" in result.detail
    assert HOST not in result.detail


# ---- the announce ------------------------------------------------------------------


async def test_the_announce_does_not_point_at_this_computer_s_workspace(box, tmp_path):
    from raven.agent.subagent.manager import SubagentManager

    class _Provider:
        def get_default_model(self) -> str:
            return "m"

    mgr = SubagentManager(provider=_Provider(), workspace=Path("/tmp"), agents=[remote_config()])
    submitted: list[Any] = []
    mgr.set_submit(submitted.append)

    origin = {"channel": "web", "chat_id": "d", "session_key": "web:s1", "agent": "stub@box", "workspace": "/tmp"}
    await mgr._announce_result("t1", "Look", "look", "done", origin, "ok")

    text = submitted[0].text
    # Named on every path, as written: a failed or cancelled run's result does
    # not close with the resolved directory the way a finished one's does.
    assert "Working directory: on machine 'box', ~/raven-work/t1 (made there when the run started)" in text
    assert "Working directory: /tmp" not in text
    assert "Collected outputs:" not in text, "nothing came back from this run"

    # A run refused by the registry, the ssh or the node never made it: the
    # line names the directory without claiming it exists.
    await mgr._announce_result("t1", "Look", "look", "Error: it could not be reached", origin, "error")
    failed = submitted[-1].text
    assert "Working directory: on machine 'box', ~/raven-work/t1 (made there only if the run got that far)" in failed
    assert "made there when the run started" not in failed

    record = tmp_path / "nodes" / "t1.out.md"
    await mgr._announce_result("t1", "Look", "look", "done", origin, "ok", record_path=str(record))
    assert "Collected outputs:" not in submitted[1].text, "nothing is brought back from a machine unasked"


async def test_the_announce_and_the_record_name_the_machine_s_directory_for_any_handle(box):
    from raven.agent.subagent.manager import SubagentManager

    class _Provider:
        def get_default_model(self) -> str:
            return "m"

    mgr = SubagentManager(
        provider=_Provider(),
        workspace=Path("/tmp"),
        agents=[remote_config(), remote_config(name="deep@box", remote_cwd="/srv/agents/")],
    )

    assert mgr._machine_workdir("stub@box", "reviewer") == ("box", "~/raven-work/reviewer")
    assert mgr._machine_workdir("deep@box", "a b") == (
        "box",
        "/srv/agents/a_b-" + mgr._machine_workdir("deep@box", "a b")[1][-8:],
    )
    assert mgr._machine_workdir("nobody", "t1") == ("", "")


# ---- registered machines ----------------------------------------------------------------


def test_machine_and_local_acp_rows_are_on_the_table_without_a_debug_switch(box, monkeypatch):
    from raven.agent.subagent.registry import AgentRegistry

    monkeypatch.delenv("RAVEN_DEBUG_MACHINES", raising=False)
    registry = AgentRegistry(build_builtin=lambda row, narrowed: ("backend", row.name))
    local = ThirdPartyAcpSubagentConfig(name="here", command="stub")
    registry.apply([remote_config(), local])
    assert "stub@box" in registry.names() and "here" in registry.names()
    assert box.calls() == [], "constructing the table does not connect to a machine"


async def test_acp_dispatch_and_checks_need_no_debug_switch(box, monkeypatch, tmp_path):
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES", raising=False)
    cfg = remote_config()
    probed = await probe.probe_one(cfg, source="config")
    assert box.calls() == [], "the free probe must not open a connection"
    assert "debug-only" not in probed.detail
    reply = await build_third_party_backend(cfg).run("hi", task_id="default-on", workspace=tmp_path, executor=None)
    assert "pong" in reply
    assert (await verify_agent(cfg)).status == "ready"
    assert (await probe.run_test(cfg, source="config")).ok


async def test_the_boot_backfill_never_logs_in_to_a_machine(box):
    from raven.agent.subagent.backends import acp_snapshot_for

    cfg = remote_config()
    await probe._verify_missing_snapshots(object(), [_row_for(cfg)])

    assert box.calls() == [], "an unattended boot opens no ssh session"
    assert acp_snapshot_for(cfg) is None, "the first listing says 'run a test', and that press is the owner's"


# ---- what a test on a machine row reports --------------------------------------------------


async def test_a_test_of_a_machine_row_that_needs_a_sign_in_offers_no_fix_for_this_computer(box, monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("asked the agent again in print mode, on this computer")

    monkeypatch.setattr(probe.kimi_code, "explain_refusal", never)
    monkeypatch.setattr(probe, "_remedy_for", lambda cfg: pytest.fail("a fix resolved on this computer's PATH"))

    result = await probe.run_test(remote_config(mode="no_session"), source="config")

    assert result.ok is False
    assert result.remedy is None, "a sign-in command the page offered to copy would fix this computer"
    assert result.detail.endswith("(on machine 'box': any fix is made there)")
    assert HOST not in result.detail


async def test_the_machine_note_survives_a_refusal_longer_than_the_detail_cap(box):
    long_reason = "Authentication required: " + "x" * 2500
    cfg = remote_config(mode="no_session").model_copy(
        update={"env": {"ACP_STUB_MODE": "no_session", "ACP_STUB_MESSAGE": long_reason}}
    )

    pinged = await probe.ping_agent(cfg)
    tested = await probe.run_test(cfg, source="config")

    for detail in (pinged.detail, tested.detail):
        assert len(detail) <= probe._DETAIL_CAP
        assert detail.endswith("(on machine 'box': any fix is made there)")


def test_the_machine_note_is_kept_whole_inside_the_cap():
    assert probe._on_machine("x" * 5000, "box").endswith(" (on machine 'box': any fix is made there)")
    assert len(probe._on_machine("x" * 5000, "box")) == probe._DETAIL_CAP
    assert probe._on_machine("short", "box") == "short (on machine 'box': any fix is made there)"


# ---- one owner for "session MCP on a machine" ---------------------------------------------


@pytest.mark.skipif(
    not unix_sockets_available(), reason="MCP lending rides on unix sockets, which this computer has not"
)
def test_the_one_mcp_predicate_answers_for_a_machine_row_too(monkeypatch):
    from raven.agent.subagent.backends import session_mcp_delivered

    assert session_mcp_delivered(session_mcp=True, snapshot=None, machine="box") is True, "lent through the node"
    assert session_mcp_delivered(session_mcp=False, snapshot=None, machine="box") is False, (
        "the peer's own refusal holds"
    )
    assert session_mcp_delivered(session_mcp=True, snapshot=None, machine="") is True
    assert session_mcp_delivered(session_mcp=True, snapshot=None) is True

    # Without the transport (a Windows host), neither side is promised what the dispatch would withhold.
    monkeypatch.delattr(asyncio, "start_unix_server", raising=False)
    assert session_mcp_delivered(session_mcp=True, snapshot=None, machine="box") is False
    assert session_mcp_delivered(session_mcp=True, snapshot=None) is False


# ---- the write doors ------------------------------------------------------------------------


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.json"
    row = {"name": "pi-box", "kind": "acp", "preset": "pi", "command": "pi --acp", "machine": "box", "enabled": False}
    config = {"providers": {"openrouter": {"apiKey": "sk-raven-test-not-real"}}, "subagents": {"agents": [row]}}
    path.write_text(json.dumps(config), encoding="utf-8")
    for module in ("raven.config.loader", "raven.config.self_surface", "raven.rpc.methods.subagents"):
        monkeypatch.setattr(f"{module}.get_config_path", lambda: path)
    return path


def _stored(path: Path) -> dict:
    (row,) = json.loads(path.read_text(encoding="utf-8"))["subagents"]["agents"]
    return row


async def test_the_update_door_refuses_lending_keys_to_a_machine_row(box, config_file):
    from raven.rpc.methods.subagents import ConfigValidationError, subagents_update

    # The schema would drop the value on load and the handler would answer
    # "updated", so the one path that can say no is the write door.
    with pytest.raises(ConfigValidationError, match="cannot be started with Raven's keys"):
        await subagents_update({"name": "pi-box", "lend_keys": ["openrouter"]})

    assert "lendKeys" not in _stored(config_file) and "lend_keys" not in _stored(config_file)


# ---- two machines on one disk ---------------------------------------------------------------


async def test_a_prompt_that_fails_after_it_went_out_keeps_its_record_for_the_caller_that_reports_the_failure(
    box, tmp_path, monkeypatch
):
    """The failure is the caller's to announce, as a reply is: the record stays until it has, and the next start
    reads how the task ended off the machine and reports that."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    backend = build_third_party_backend(remote_config(mode="dies_mid_turn"))

    with pytest.raises(Exception, match="exit 5"):
        await backend.run("hi", task_id="t0", workspace=tmp_path, executor=None, session_key="tui:s1")
    assert remote_runs.pending() == [], "a caller that takes the failure as its own result leaves no record"

    with pytest.raises(Exception, match="exit 5"):
        await backend.run(
            "hi", task_id="t1", workspace=tmp_path, executor=None, session_key="tui:s1", reported_by_caller=True
        )
    (run,) = remote_runs.pending()
    assert run.task_id == "t1" and run.prompt_id > 0, "the prompt was out: the record outlives the failure"

    # The next start reads the end off the machine and reports it as the error it is.
    session_dir = tmp_path / "sessions" / "s1"
    meta = {"call_id": "n1", "session_key": "tui:s1", "agent": backend.name, "handle": "t1", "task_summary": "say hi"}
    SpawnRecord.open(session_dir, task_id="t1", task="hi", meta=meta, node_id="n1")
    manager = _Manager(backend, session_dir)
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())

    assert await asyncio.wait_for(resume_remote.resume_remote_runs(manager), timeout=30) == 1

    (said,) = manager.announced
    assert said["status"] == "error" and "exit 5" in said["result"]
    assert remote_runs.pending() == []


async def test_a_daemon_killed_outright_mid_turn_ends_the_turn_with_the_reason_and_leaves_no_agent(box, tmp_path):
    """The next daemon brings the task down and says why; the host reads that sentence off the attach instead of a
    bare exit code, and the agent the killed daemon left behind is gone."""
    box.nodes._heartbeat_s = 0.1
    box.nodes._lost_after_s = 1.0
    box.nodes._backoff_s = (0.2,)
    backend = build_third_party_backend(remote_config(env={"ACP_STUB_PROMPT_DELAY_S": "20"}))
    turn = asyncio.ensure_future(backend.run("hi", task_id="t1", workspace=tmp_path, executor=None))
    deadline = time.monotonic() + 20
    while box.nodes.status("box").agents != 1:
        assert time.monotonic() < deadline and not turn.done(), "the agent never started"
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)
    agent = _agent_pid()
    daemon = box.nodes._nodes["box"].hello["pid"]
    assert _alive(agent) and _alive(daemon)

    os.kill(daemon, signal.SIGKILL)

    with pytest.raises(Exception, match="restarted while the agent ran") as caught:
        await asyncio.wait_for(turn, timeout=30)
    assert "exit -1" not in str(caught.value), "the sentence, not the bare lost code"
    assert box.nodes._nodes["box"].hello["pid"] != daemon, "a new daemon, started by the new ssh"
    deadline = time.monotonic() + 5
    while _alive(agent) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _alive(agent), "the agent the killed daemon left behind was ended by the new one"


async def test_a_remote_turn_runs_without_lent_mcp_when_the_host_has_no_unix_socket_api(box, tmp_path, monkeypatch):
    monkeypatch.delattr(asyncio, "start_unix_server", raising=False)
    monkeypatch.delattr(asyncio, "open_unix_connection", raising=False)
    backend = build_third_party_backend(remote_config())
    grant = McpGrant(granted=(GrantedServer("pg", "stdio", MCPServerConfig(command="must-not-start")),))
    reply = await backend.run("hi", task_id="no-host-socket", workspace=tmp_path, executor=None, mcp_grant=grant)
    assert reply.split("\n\n[raven]")[0] == "pong"
    assert "this computer does not support Unix-socket MCP endpoints" in reply
    assert not box.nodes._lent
