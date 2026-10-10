"""raven.node.client: one live Raven node per registered machine.

The launch tests need no second computer. A stand-in ``ssh`` on PATH does what
sshd does with the one remote-command argument it is sent -- hands it to the
user's shell, in that user's home -- so the real launch line and the real login
shell run, and so does the real node: the machine's node directory holds a
``bin/python`` that is this test run's own interpreter. Only the network is
missing. The stand-in also fails the way ssh fails, writing its own
address-bearing words to the log the launch line names (``-E``), which is how
the tests show those words never reach a message.

Every failure is one sentence that names the machine and what to do, never the
address it is reached at.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from raven.acp_client import remote
from raven.acp_client.protocol import AcpConnectionError, AcpRemoteError, AcpTimeoutError
from raven.agent.subagent.backends import env as backend_env
from raven.agent.tools.filesystem import ReadFileTool
from raven.node import bundle, protocol
from raven.node import client as node_client
from raven.node.client import DEFAULT_NODE_DIR, NodeError, NodePool, _Node

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
command = args[args.index("--") + 1] if "--" in args else args[-1]
os.chdir(home)
os.execve("/bin/sh", ["/bin/sh", "-c", command], {{"HOME": home, "SHELL": "/bin/sh", "PATH": os.environ["PATH"]}})
"""

# A node that misbehaves in one named way, for the handshake's refusals.
_STUB_NODE = """
import json, sys
mode, digest = sys.argv[1], sys.argv[2]
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
        answer["error"] = {"code": -32600, "message": "this node speaks protocol 2, not 1"}
    elif mode == "other-protocol":
        answer["result"] = {"protocol": 2, "digest": digest}
    elif mode == "other-code":
        answer["result"] = {"protocol": 1, "digest": "000000000000"}
    elif mode == "not-an-object":
        answer["result"] = ["node/hello"]
    else:
        answer["result"] = {"protocol": 1, "digest": digest, "python": "3.12.0", "home": "", "rg": False}
    print(json.dumps(answer), flush=True)
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
            body = f"exec {sys.executable} {script} {stub} {bundle.digest()}\n"
        python.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        python.chmod(0o755)
        return python

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
    return Machine(tmp_path, monkeypatch)


@pytest.fixture
async def pool():
    nodes = NodePool()
    yield nodes
    await nodes.close_all()


def _no_address(message: str) -> None:
    assert HOST not in message and str(PORT) not in message and "worker@" not in message


# --- a node over the real launch line --------------------------------------------


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
    assert first == second == {"text": await local.execute(str(work / "notes.txt"), offset=10, limit=3)}
    assert len(box.launches()) == 1, "one ssh serves every call"
    assert pool.live() == ["box"]
    hello = await pool.hello("box")
    assert hello["digest"] == bundle.digest() and hello["home"] == str(box.home)


@pytest.mark.slow
async def test_the_node_is_started_from_where_the_registry_keeps_nodes(box, pool, tmp_path):
    kept = tmp_path / "nodes on a big disk"
    box.rows([row(node_dir=f"{kept}/")])
    box.node(kept)

    hello = await pool.hello("box")

    assert hello["digest"] == bundle.digest()
    (launch,) = box.launches()
    command = launch[launch.index("--") + 1]
    assert str(kept / bundle.install_name() / "bin" / "python") in command
    # From the home directory: a node makes no working directory, which on a
    # full disk would fail before the node started.
    assert command.startswith("exec /bin/sh -c 'echo; mkdir -p ~ && cd ~ || exit; ")


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
        "the Raven node on machine 'Lab box' (box) did not complete its handshake: this node speaks protocol 2, not 1"
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


async def test_a_machine_that_is_not_registered_is_refused_before_any_ssh(box, pool):
    with pytest.raises(NodeError, match="machine 'elsewhere' is not registered"):
        await pool.hello("elsewhere")

    assert box.launches() == []


async def test_a_launch_line_that_cannot_be_made_is_one_sentence(box, pool, monkeypatch):
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise remote.RemoteMachineError("machine 'Lab box' (box): no key to log in with")

    monkeypatch.setattr(remote, "prepare_launch", refuse)

    with pytest.raises(NodeError, match=r"^machine 'Lab box' \(box\): no key to log in with$"):
        await pool.hello("box")


async def test_no_ssh_on_this_computer_is_one_sentence(box, pool, monkeypatch, tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setattr(backend_env, "_LOGIN_ENV", {**os.environ, "PATH": str(empty)})

    with pytest.raises(NodeError, match=r"^could not start ssh to machine 'Lab box' \(box\): ") as caught:
        await pool.hello("box")

    _no_address(str(caught.value))


# --- a live node's calls, with the connection stood in for -------------------------


class _Client:
    """A connection that answers, or fails, the way it is told to."""

    def __init__(self, *, result: Any = None, raises: BaseException | None = None) -> None:
        self.result = {"text": "ok"} if result is None else result
        self.raises = raises
        self.alive = True
        self.asked: list[tuple[str, dict[str, Any], float]] = []

    async def request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        self.asked.append((method, params, timeout))
        if self.raises is not None:
            raise self.raises
        return self.result

    async def close(self) -> None:
        self.alive = False


def _live(pool: NodePool, tmp_path: Path, machine_id: str = "box", **kw: Any) -> _Client:
    client = _Client(**kw)
    target = remote.Machine(id=machine_id, display_name="Lab box", _row=row(id=machine_id))
    log = tmp_path / f"{machine_id}.log"
    log.write_text(f"ssh: connect to host {HOST} port {PORT}: Connection refused\n", encoding="utf-8")
    launched = remote.RemoteLaunch(target=target, root="~", log=log, command="ssh")
    pool._nodes[machine_id] = _Node(client, launched)  # type: ignore[arg-type]
    return client


async def test_a_call_is_sent_with_its_budget_and_its_answer_returned(tmp_path):
    pool = NodePool()
    client = _live(pool, tmp_path, result={"text": "line 1"})
    before = pool._nodes["box"].used

    assert await pool.call("box", protocol.READ, {"path": "a"}, timeout=7.0) == {"text": "line 1"}

    assert client.asked == [(protocol.READ, {"path": "a"}, 7.0)]
    assert pool._nodes["box"].used >= before


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
    assert client.alive and pool.live() == ["box"]


async def test_a_call_that_timed_out_drops_the_node(tmp_path):
    pool = NodePool()
    client = _live(pool, tmp_path, raises=AcpTimeoutError("no answer", method=protocol.READ))

    with pytest.raises(NodeError, match=r"did not answer fs/read within 120s$"):
        await pool.call("box", protocol.READ, {})

    assert not client.alive and "box" not in pool._nodes


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
    pool = NodePool()
    client = _live(pool, tmp_path, raises=AcpConnectionError("gone", stderr=stderr, returncode=returncode))

    with pytest.raises(NodeError) as caught:
        await pool.call("box", protocol.READ, {})

    assert str(caught.value) == said
    _no_address(str(caught.value))
    assert not client.alive and "box" not in pool._nodes


async def test_a_node_left_idle_is_closed_when_another_machine_is_asked_for(tmp_path):
    pool = NodePool(idle_close_s=60.0)
    old = _live(pool, tmp_path, "old")
    recent = _live(pool, tmp_path, "recent")
    asked = _live(pool, tmp_path, "asked")
    pool._nodes["old"].used = time.monotonic() - 61.0
    pool._nodes["asked"].used = time.monotonic() - 61.0

    await pool.call("asked", protocol.READ, {})

    assert not old.alive and recent.alive and asked.alive
    assert sorted(pool.live()) == ["asked", "recent"]


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
