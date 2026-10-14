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
from tests.test_subagent_acp_remote import HOST, PORT, Machine, box  # noqa: F401 - the fixture, by name

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
if "-N" not in args or "-L" not in args:
    sys.stderr.write("this stand-in only forwards\\n")
    sys.exit(2)
bind_host, bind_port, far_host, far_port = args[args.index("-L") + 1].rsplit(":", 3)
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


async def _chat_completions_stub() -> tuple[Any, int, list[tuple[list[str], dict[str, Any]]]]:
    """An endpoint on this computer's loopback: the far end of the stand-in tunnel."""
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

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
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
    pid = int((forwarding.bin / "tunnel.pid").read_text(encoding="utf-8"))
    await _until(lambda: _gone(pid))  # the ssh that never opened it is ended


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


def test_with_the_lane_off_a_cli_machine_row_stays_off_the_table(box, monkeypatch):
    registry = AgentRegistry(build_builtin=lambda row, narrowed: ("backend", row.name))
    local = ThirdPartyCliSubagentConfig(name="here", command="cat")

    monkeypatch.delenv("RAVEN_DEBUG_MACHINES", raising=False)
    registry.apply([cli_config("cat"), local])
    assert "where@box" not in registry.names() and "here" in registry.names()

    monkeypatch.setenv("RAVEN_DEBUG_MACHINES", "1")
    registry.apply([cli_config("cat"), local])
    assert "where@box" in registry.names()


async def test_with_the_lane_off_a_cli_dispatch_says_so_and_reaches_nothing(box, monkeypatch, tmp_path):
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES", raising=False)

    with pytest.raises(RuntimeError, match="the machine lane is debug-only and off"):
        await _run(cli_config("cat"), "hi", tmp_path)
    assert box.calls() == []


async def test_the_announce_names_a_cli_row_s_directory_on_the_machine(box):
    from raven.agent.subagent.manager import SubagentManager

    class _Provider:
        def get_default_model(self) -> str:
            return "m"

    mgr = SubagentManager(provider=_Provider(), workspace=Path("/tmp"), agents=[cli_config("cat")])

    assert mgr._machine_workdir("where@box", "reviewer") == ("box", "~/raven-work/reviewer")
