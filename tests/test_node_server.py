"""raven.node.server: Raven's own file tools, answered over stdio.

The property everything else rests on is parity: a file call a node answers
comes back exactly as the same tool's answer on the host's own disk, footer
and limits included. Then the fence: a call reaches only the roots it names,
and one that names none is refused rather than read as "no fence".
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

if sys.platform == "win32":
    pytest.skip("the daemon is POSIX: it locks with fcntl and listens on a unix socket", allow_module_level=True)

from raven.agent.tools.file_search import FindTool, GrepTool
from raven.agent.tools.filesystem import ListDirTool, ReadFileTool
from raven.node import __main__ as node_main
from raven.node import bundle, protocol, server


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "sub").mkdir(parents=True)
    (root / "big.txt").write_text("".join(f"line {i} of the file\n" for i in range(1, 501)), encoding="utf-8")
    (root / "sub" / "a.py").write_text("needle = 1\nhay = 2\n", encoding="utf-8")
    (root / "b.md").write_text("hay\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top secret\n", encoding="utf-8")
    return root


def _local(cls: Any, root: Path) -> Any:
    """The tool as the host would run it on its own disk, fenced to ``root``."""
    return cls(workspace=Path.home(), allowed_dirs=(root,), follow_binding=False)


async def _ask(method: str, params: Any = None, *, request_id: Any = 1) -> dict[str, Any] | None:
    frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    return await server.handle(frame)


async def _result(method: str, params: dict[str, Any]) -> dict[str, Any]:
    answer = await _ask(method, params)
    assert answer is not None and "result" in answer, answer
    return answer["result"]


async def _refusal(method: str, params: Any) -> tuple[int, str]:
    answer = await _ask(method, params)
    assert answer is not None and "error" in answer, answer
    return answer["error"]["code"], answer["error"]["message"]


# --- parity -------------------------------------------------------------------


async def test_a_read_through_the_node_is_the_read_the_host_would_have_made(tree):
    path = str(tree / "big.txt")

    got = await _ask(protocol.READ, {"path": path, "offset": 200, "limit": 5, "roots": [str(tree)]})

    want = await _local(ReadFileTool, tree).execute(path, offset=200, limit=5)
    digest = hashlib.sha256((tree / "big.txt").read_bytes()).hexdigest()
    assert got == {"jsonrpc": "2.0", "id": 1, "result": {"text": want, "sha256": digest}}
    assert "line 200 of the file" in want and "line 199 of the file" not in want


async def test_a_read_with_no_offset_starts_at_the_first_line(tree):
    path = str(tree / "b.md")

    got = await _result(protocol.READ, {"path": path, "roots": [str(tree)]})

    assert got == {
        "text": await _local(ReadFileTool, tree).execute(path),
        "sha256": hashlib.sha256(b"hay\n").hexdigest(),
    }


async def test_list_find_and_grep_answer_as_their_tools_do(tree):
    roots = [str(tree)]

    listed = await _result(protocol.LIST, {"path": str(tree), "recursive": True, "max_entries": 10, "roots": roots})
    found = await _result(protocol.FIND, {"pattern": "*.py", "path": str(tree), "limit": 5, "roots": roots})
    grepped = await _result(
        protocol.GREP,
        {
            "pattern": "HAY",
            "path": str(tree),
            "glob": "*.py",
            "output_mode": "content",
            "case_insensitive": True,
            "context": 1,
            "limit": 5,
            "roots": roots,
        },
    )

    assert listed == {"text": await _local(ListDirTool, tree).execute(str(tree), recursive=True, max_entries=10)}
    assert found == {"text": await _local(FindTool, tree).execute("*.py", path=str(tree), limit=5)}
    assert grepped == {
        "text": await _local(GrepTool, tree).execute(
            "HAY", path=str(tree), glob="*.py", output_mode="content", case_insensitive=True, context=1, limit=5
        )
    }
    assert "a.py" in found["text"] and "hay = 2" in grepped["text"] and "b.md" not in grepped["text"]


async def test_an_image_comes_back_with_its_blocks(tree):
    from PIL import Image

    picture = tree / "dot.png"
    Image.new("RGB", (2, 2), "red").save(picture)

    got = await _result(protocol.READ, {"path": str(picture), "roots": [str(tree)]})

    want = await _local(ReadFileTool, tree).execute(str(picture))
    assert got == {
        "text": want.model_text,
        "display": want.display_text,
        "ok": True,
        "blocks": want.blocks,
        "sha256": hashlib.sha256(picture.read_bytes()).hexdigest(),
    }
    assert [block["type"] for block in got["blocks"]] == ["text", "image_url"]


async def test_a_relative_path_and_a_tilde_root_mean_the_home_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "notes.txt").write_text("from home\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    got = await _result(protocol.READ, {"path": "notes.txt", "roots": ["~"]})

    assert "from home" in got["text"]


# --- the fence ----------------------------------------------------------------


async def test_a_file_outside_the_roots_is_not_read(tree):
    outside = str(tree.parent / "secret.txt")

    got = await _result(protocol.READ, {"path": outside, "roots": [str(tree)]})

    assert "top secret" not in got["text"]
    assert got == {"text": await _local(ReadFileTool, tree).execute(outside)}, "a refusal names no digest"


async def test_a_link_inside_the_roots_does_not_lead_out_of_them(tree):
    (tree / "link.txt").symlink_to(tree.parent / "secret.txt")

    got = await _result(protocol.READ, {"path": str(tree / "link.txt"), "roots": [str(tree)]})

    assert "top secret" not in got["text"]


@pytest.mark.parametrize(
    ("roots", "said"),
    [
        (None, "roots must be a non-empty list of directories"),
        ([], "roots must be a non-empty list of directories"),
        ("/", "roots must be a non-empty list of directories"),
        ([7], "roots must be a non-empty list of directories"),
        (["  "], "roots must be a non-empty list of directories"),
        (["relative/dir"], "root 'relative/dir' is not an absolute directory"),
    ],
)
async def test_a_call_that_names_no_usable_roots_is_refused(tree, roots, said):
    params: dict[str, Any] = {"path": str(tree / "b.md")}
    if roots is not None:
        params["roots"] = roots

    assert await _refusal(protocol.READ, params) == (-32602, said)


# --- what a request may carry ---------------------------------------------------


@pytest.mark.parametrize(
    ("method", "params", "said"),
    [
        (protocol.READ, {"path": ""}, "path must be a non-empty string"),
        (protocol.READ, {"path": 3}, "path must be a non-empty string"),
        (protocol.READ, {"path": "b.md", "offset": True}, "offset must be a whole number"),
        (protocol.READ, {"path": "b.md", "limit": "5"}, "limit must be a whole number"),
        (protocol.LIST, {"path": ".", "recursive": "yes"}, "recursive must be true or false"),
        (protocol.FIND, {"pattern": ""}, "pattern must be a non-empty string"),
        (protocol.GREP, {"pattern": "x", "glob": 3}, "glob must be a string"),
        (protocol.GREP, {"pattern": "x", "output_mode": 3}, "output_mode must be a string"),
        (protocol.GREP, {"pattern": "x", "context": 1.5}, "context must be a whole number"),
    ],
)
async def test_a_malformed_field_is_refused_by_name(tree, method, params, said):
    assert await _refusal(method, {**params, "roots": [str(tree)]}) == (-32602, said)


async def test_params_that_are_not_an_object_are_refused():
    assert await _refusal(protocol.READ, ["b.md"]) == (-32602, "params must be an object")


async def test_an_unknown_method_is_refused():
    assert await _refusal("fs/nothing", {}) == (-32601, "unknown method 'fs/nothing'")


async def test_a_failure_inside_a_tool_is_an_answer_and_the_node_keeps_serving(monkeypatch):
    async def broken(params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setitem(server._HANDLERS, protocol.READ, broken)

    assert await _refusal(protocol.READ, {}) == (-32603, "RuntimeError: boom")
    assert (await _result(protocol.HELLO, {}))["protocol"] == protocol.PROTOCOL


@pytest.mark.parametrize(
    "frame",
    [
        {"jsonrpc": "2.0", "method": protocol.HELLO},
        {"jsonrpc": "2.0", "id": None, "method": protocol.HELLO},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        [{"jsonrpc": "2.0", "id": 1, "method": protocol.HELLO}],
    ],
)
async def test_nothing_is_owed_for_a_notification_or_a_frame_that_is_not_a_request(frame):
    assert await server.handle(frame) is None


# --- the handshake --------------------------------------------------------------


async def test_hello_says_what_the_host_checks_before_using_a_node(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    hello = await _result(protocol.HELLO, {"protocol": protocol.PROTOCOL})

    assert hello["protocol"] == protocol.PROTOCOL
    assert hello["digest"] == bundle.digest()
    assert hello["home"] == str(tmp_path)
    assert hello["pid"] == os.getpid()
    assert hello["python"].count(".") == 2
    assert isinstance(hello["rg"], bool)


async def test_hello_from_a_host_of_another_protocol_is_refused():
    assert await _refusal(protocol.HELLO, {"protocol": protocol.PROTOCOL + 1}) == (
        -32600,
        f"this node speaks protocol {protocol.PROTOCOL}, not {protocol.PROTOCOL + 1}",
    )


async def test_hello_says_whether_grep_runs_rg_here(monkeypatch):
    from raven.agent.tools import file_search

    monkeypatch.setattr(file_search, "_resolve_rg", lambda: None)
    assert (await _result(protocol.HELLO, {}))["rg"] is False
    monkeypatch.setattr(file_search, "_resolve_rg", lambda: "/opt/rg")
    assert (await _result(protocol.HELLO, {}))["rg"] is True


# --- the line loop --------------------------------------------------------------


async def test_serve_answers_line_by_line_and_skips_what_it_cannot_read():
    reader = asyncio.StreamReader(limit=256)
    for line in (
        b"\n",
        b"x" * 400 + b"\n",
        b"not json\n",
        json.dumps({"jsonrpc": "2.0", "method": protocol.HELLO}).encode() + b"\n",
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": protocol.HELLO}).encode() + b"\n",
        json.dumps({"jsonrpc": "2.0", "id": 8, "method": "fs/nothing"}).encode(),
    ):
        reader.feed_data(line)
    reader.feed_eof()
    out = io.BytesIO()

    await server.serve(reader, out)

    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [a["id"] for a in answers] == [None, 7, 8]
    assert answers[0]["error"] == {"code": -32700, "message": "not a JSON object"}
    assert answers[1]["result"]["protocol"] == protocol.PROTOCOL
    assert answers[2]["error"]["code"] == -32601


_NOISY_NODE = """
import sys
import time
from raven.node import server

async def noisy(params):
    print("a library talking on stdout")
    return {"text": "ok"}

server._HANDLERS["x/noisy"] = noisy
sys.exit(server.run_stdio())
"""


def _node(argv: list[str], requests: list[dict[str, Any]], home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv],
        input="".join(json.dumps(r) + "\n" for r in requests),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "HOME": str(home)},
        cwd=home,
    )


@pytest.mark.slow
def test_a_node_process_answers_until_its_stdin_closes(tree):
    done = _node(
        ["-m", "raven.node", "--stdio"],
        [
            {"jsonrpc": "2.0", "id": 1, "method": protocol.HELLO, "params": {"protocol": protocol.PROTOCOL}},
            {"jsonrpc": "2.0", "id": 2, "method": protocol.READ, "params": {"path": "b.md", "roots": [str(tree)]}},
        ],
        tree,
    )

    assert done.returncode == 0, done.stderr
    first, second = (json.loads(line) for line in done.stdout.splitlines())
    assert first["result"]["digest"] == bundle.digest()
    assert "hay" in second["result"]["text"]


# A node with nothing importable beyond the standard library, its own code and
# the packages it is installed with: what the next change to raven/agent/tools
# must not break, since every installed node runs it.
_BUDGETED_NODE = """
import sys
allowed = set(sys.stdlib_module_names) | set(sys.modules) | {"raven", *%r}


class _Budget:
    def find_spec(self, name, path=None, target=None):
        # The build's own sysconfig data is the standard library's, under a name of the platform's.
        if name.partition(".")[0] in allowed or name.startswith("_sysconfigdata"):
            return None
        raise ImportError(f"{name} is not among a node's requirements")


sys.meta_path.insert(0, _Budget())
from raven.node import server
sys.exit(server.run_stdio())
"""
_MODULE_OF = {"pillow": "PIL"}


@pytest.mark.slow
def test_a_node_answers_every_file_call_with_only_its_requirements_importable(tree):
    importable = tuple(_MODULE_OF.get(name, name) for name in protocol.REQUIREMENTS)
    calls = [
        (protocol.READ, {"path": "b.md"}),
        (protocol.LIST, {"path": "."}),
        (protocol.FIND, {"pattern": "*.py", "path": "."}),
        (protocol.GREP, {"pattern": "needle", "path": "."}),
        (protocol.STAT, {"path": "b.md", "hash": True}),
        (protocol.WRITE, {"path": "new.txt", "content": "x\n"}),
        (protocol.EDIT, {"path": "new.txt", "old_text": "x", "new_text": "y"}),
        (protocol.PULL, {"path": "b.md"}),
    ]
    requests = [{"jsonrpc": "2.0", "id": 1, "method": protocol.HELLO, "params": {"protocol": protocol.PROTOCOL}}]
    requests += [
        {"jsonrpc": "2.0", "id": n, "method": method, "params": {**params, "roots": [str(tree)]}}
        for n, (method, params) in enumerate(calls, start=2)
    ]

    done = _node(["-c", _BUDGETED_NODE % (importable,)], requests, tree)

    assert done.returncode == 0, done.stderr
    answers = [json.loads(line) for line in done.stdout.splitlines()]
    assert [answer.get("error") for answer in answers] == [None] * len(requests), answers
    assert answers[0]["result"]["digest"] == bundle.digest()


@pytest.mark.slow
def test_stdout_carries_only_frames_whatever_else_prints(tree):
    done = _node(["-c", _NOISY_NODE], [{"jsonrpc": "2.0", "id": 1, "method": "x/noisy"}], tree)

    assert done.returncode == 0, done.stderr
    assert [json.loads(line) for line in done.stdout.splitlines()] == [
        {"jsonrpc": "2.0", "id": 1, "result": {"text": "ok"}}
    ]
    assert "a library talking on stdout" in done.stderr


def test_run_stdio_serves_this_process_s_own_stdin_and_moves_print_to_stderr(monkeypatch):
    read_end, write_end = os.pipe()
    os.write(write_end, (json.dumps({"jsonrpc": "2.0", "id": 3, "method": protocol.HELLO}) + "\n").encode())
    os.close(write_end)
    frames = io.BytesIO()

    class _Stdout:
        buffer = frames

    monkeypatch.setattr(sys, "stdin", os.fdopen(read_end, "rb", buffering=0))
    monkeypatch.setattr(sys, "stdout", _Stdout())

    assert server.run_stdio() == 0

    (answer,) = [json.loads(line) for line in frames.getvalue().splitlines()]
    assert answer["id"] == 3 and answer["result"]["protocol"] == protocol.PROTOCOL
    assert sys.stdout is sys.stderr


# --- python -m raven.node ---------------------------------------------------------


def test_version_prints_the_protocol_and_the_digest(capsys):
    assert node_main.main(["--version"]) == 0
    assert json.loads(capsys.readouterr().out) == {"protocol": protocol.PROTOCOL, "digest": bundle.digest()}


def test_stdio_serves(monkeypatch):
    monkeypatch.setattr(server, "run_stdio", lambda: 0)

    assert node_main.main(["--stdio"]) == 0


def test_a_daemon_refused_its_socket_s_place_says_so_in_one_line_and_leaves(tmp_path, monkeypatch, capsys):
    def refuse(root: Path, *, idle_s: float) -> int:
        raise PermissionError(f"{root} is in the way of the daemon")

    monkeypatch.setattr("raven.node.server.run_daemon", refuse)

    assert node_main.main(["--serve", "--dir", str(tmp_path)]) == 1

    assert f"raven node: {tmp_path} is in the way of the daemon" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["--stdio", "--version"]])
def test_a_node_is_started_for_exactly_one_purpose(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        node_main.main(argv)

    assert caught.value.code == 2


# --- the link's own methods --------------------------------------------------------


async def test_a_ping_comes_back_with_what_it_carried():
    assert await _result(protocol.PING, {"t": 12.5}) == {"t": 12.5}


async def test_the_session_directory_is_made_and_resolved_here(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    got = await _result(protocol.SESSION_DIR, {"path": "~/raven-work/a b"})

    assert got["path"] == str((tmp_path / "raven-work" / "a b").resolve())
    assert Path(got["path"]).is_dir()
    assert abs(got["since"] - time.time()) < 60, "this machine's clock, as the directory was made"


async def test_a_session_directory_that_cannot_be_made_is_refused_with_the_reason(tmp_path):
    (tmp_path / "f").write_text("a file\n", encoding="utf-8")

    code, said = await _refusal(protocol.SESSION_DIR, {"path": str(tmp_path / "f" / "t1")})

    assert code == -32603 and said.startswith(f"could not make {tmp_path / 'f' / 't1'}: ")


async def test_the_machine_probe_reports_room_python_shell_and_load():
    got = await _result(protocol.PROBE_MACHINE, {})

    assert set(got) == {"disk_free_kb", "python", "shell", "uv", "load"}
    assert str(Path.home()) in got["disk_free_kb"] and got["python"].count(".") == 2


def test_the_node_drives_the_login_shells_the_host_does():
    from raven.agent.subagent.backends.env import _DRIVABLE_SHELLS

    assert server._DRIVABLE_SHELLS == _DRIVABLE_SHELLS


def test_the_login_environment_is_captured_from_a_drivable_shell_and_left_alone_otherwise(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/csh")
    assert server.capture_login_env() == dict(os.environ), "a shell it cannot drive leaves ssh's environment"

    bash = tmp_path / "bash"
    bash.write_text("#!/bin/sh\nprintf 'PATH=/from/profile\\0X=1\\0garbage\\0'\n", encoding="utf-8")
    bash.chmod(0o755)
    monkeypatch.setenv("SHELL", str(bash))
    assert server.capture_login_env() == {"PATH": "/from/profile", "X": "1"}

    bash.write_text("#!/bin/sh\nprintf 'X=1\\0'\n", encoding="utf-8")
    assert server.capture_login_env() == dict(os.environ), "a capture without a PATH is no capture"
    bash.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    assert server.capture_login_env() == dict(os.environ), "a failed capture leaves ssh's environment"


# --- agents the node starts -----------------------------------------------------------

# An agent for these tests: says one thing on stderr, answers every request
# with its params (or its environment, or a child it started), and leaves when
# its stdin closes.
_AGENT = """
import json, os, subprocess, sys
sys.stderr.write("agent: started\\n")
sys.stderr.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    method = request.get("method")
    if method == "env":
        result = {"login": os.environ.get("FROM_LOGIN"), "entry": os.environ.get("FROM_ENTRY"), "cwd": os.getcwd()}
    elif method == "spawn":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        result = {"pid": child.pid}
    elif method == "detach":
        # In a session of its own, the way Claude Code's shell tool starts a command.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
        result = {"pid": child.pid}
    else:
        result = {"echo": request.get("params")}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


class _Host:
    """The host's end of the connection: every line the node wrote, decoded."""

    def __init__(self) -> None:
        self.out = io.BytesIO()
        self.node = server.Node(self.out)

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.out.getvalue().splitlines() if line.strip()]

    def notified(self, method: str) -> list[dict[str, Any]]:
        return [frame["params"] for frame in self.frames() if frame.get("method") == method]

    async def ask(self, method: str, params: dict[str, Any], request_id: int = 1) -> dict[str, Any]:
        answer = await server.handle(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, self.node
        )
        assert answer is not None
        return answer

    async def tell(self, method: str, params: dict[str, Any]) -> None:
        assert await server.handle({"jsonrpc": "2.0", "method": method, "params": params}, self.node) is None

    async def until(self, check: Any, *, timeout: float = 10.0) -> None:
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
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == pid
    except ChildProcessError:
        return False


@pytest.fixture
def agent(tmp_path: Path) -> str:
    script = tmp_path / "agent.py"
    script.write_text(_AGENT, encoding="utf-8")
    return f"{sys.executable} {script}"


def _frame(method: str, request_id: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method})


@pytest.mark.slow
async def test_an_agent_starts_in_its_root_in_the_login_environment_plus_the_entry_s_and_is_forwarded(
    tmp_path, agent, monkeypatch
):
    monkeypatch.setattr(server, "capture_login_env", lambda: {**os.environ, "FROM_LOGIN": "yes"})
    host = _Host()
    root = tmp_path / "root" / "deep"

    started = await host.ask(
        protocol.AGENT_LAUNCH, {"channel": 1, "command": agent, "cwd": str(root), "env": {"FROM_ENTRY": "1"}}
    )

    assert isinstance(started["result"]["pid"], int) and root.is_dir(), "the root is made for it"
    await host.tell(protocol.AGENT_FRAME, {"channel": 1, "line": _frame("env", 5)})
    await host.until(lambda: host.notified(protocol.AGENT_FRAME))
    answer = json.loads(host.notified(protocol.AGENT_FRAME)[0]["line"])
    assert answer["id"] == 5
    assert answer["result"]["login"] == "yes" and answer["result"]["entry"] == "1"
    assert Path(answer["result"]["cwd"]).resolve() == root.resolve()
    await host.until(lambda: host.notified(protocol.AGENT_STDERR))
    assert host.notified(protocol.AGENT_STDERR) == [{"channel": 1, "text": "agent: started"}]

    closed = await host.ask(protocol.AGENT_CLOSE, {"channel": 1}, request_id=2)

    assert closed["result"] == {"rc": 0}, "stdin closed, the agent left on its own"
    await host.until(lambda: host.notified(protocol.AGENT_EXIT))
    assert host.notified(protocol.AGENT_EXIT) == [{"channel": 1, "rc": 0}]
    assert host.node.agents == {}


@pytest.mark.slow
async def test_a_cli_agent_s_prompt_reaches_its_stdin_as_it_is_and_the_end_of_it_lets_it_leave(tmp_path, agent):
    host = _Host()
    await host.ask(protocol.AGENT_LAUNCH, {"channel": 1, "command": agent, "cwd": str(tmp_path)})

    prompt = _frame("one") + "\n" + _frame("two", 2) + "\n"
    await host.tell(protocol.AGENT_STDIN, {"channel": 1, "data": prompt, "eof": True})

    await host.until(lambda: host.notified(protocol.AGENT_EXIT))
    answered = [json.loads(frame["line"])["id"] for frame in host.notified(protocol.AGENT_FRAME)]
    assert answered == [1, 2], "both lines, in order, before the end of its stdin"
    assert host.notified(protocol.AGENT_EXIT) == [{"channel": 1, "rc": 0}], "stdin closed, the agent left on its own"
    assert host.node.agents == {}


@pytest.mark.slow
async def test_the_end_of_stdin_alone_is_a_frame_too_and_one_for_no_channel_is_dropped(tmp_path, agent):
    host = _Host()
    await host.ask(protocol.AGENT_LAUNCH, {"channel": 1, "command": agent, "cwd": str(tmp_path)})

    await host.tell(protocol.AGENT_STDIN, {"channel": 9, "data": "nobody", "eof": True})
    await host.tell(protocol.AGENT_STDIN, {"channel": 1, "eof": True})

    await host.until(lambda: host.notified(protocol.AGENT_EXIT))
    assert host.notified(protocol.AGENT_FRAME) == [] and host.notified(protocol.AGENT_EXIT) == [{"channel": 1, "rc": 0}]


@pytest.mark.slow
async def test_input_after_the_end_of_stdin_is_dropped_and_the_agent_is_left_alone(tmp_path):
    host = _Host()
    await host.ask(protocol.AGENT_LAUNCH, {"channel": 1, "command": "sleep 2", "cwd": str(tmp_path)})

    await host.tell(protocol.AGENT_STDIN, {"channel": 1, "eof": True})
    await host.tell(protocol.AGENT_STDIN, {"channel": 1, "data": "late", "eof": True})

    await host.until(lambda: host.notified(protocol.AGENT_EXIT))
    assert host.notified(protocol.AGENT_EXIT) == [{"channel": 1, "rc": 0}], "ended on its own, not by the late input"


@pytest.mark.slow
async def test_cancel_ends_the_agent_s_whole_process_group(tmp_path, agent):
    host = _Host()
    started = await host.ask(protocol.AGENT_LAUNCH, {"channel": 2, "command": agent, "cwd": str(tmp_path)})
    pid = started["result"]["pid"]
    await host.tell(protocol.AGENT_FRAME, {"channel": 2, "line": _frame("spawn")})
    await host.until(lambda: host.notified(protocol.AGENT_FRAME))
    child = json.loads(host.notified(protocol.AGENT_FRAME)[0]["line"])["result"]["pid"]
    assert not _gone(pid) and not _gone(child)

    assert (await host.ask(protocol.AGENT_CANCEL, {"channel": 2}, request_id=3))["result"] == {}

    await host.until(lambda: host.notified(protocol.AGENT_EXIT))
    assert host.notified(protocol.AGENT_EXIT)[0]["channel"] == 2
    assert host.notified(protocol.AGENT_EXIT)[0]["rc"] < 0, "ended by a signal"
    await host.until(lambda: _gone(pid) and _gone(child))


@pytest.mark.slow
async def test_cancel_ends_what_the_agent_put_in_a_session_of_its_own(tmp_path, agent):
    host = _Host()
    started = await host.ask(protocol.AGENT_LAUNCH, {"channel": 2, "command": agent, "cwd": str(tmp_path)})
    pid = started["result"]["pid"]
    await host.tell(protocol.AGENT_FRAME, {"channel": 2, "line": _frame("detach")})
    await host.until(lambda: host.notified(protocol.AGENT_FRAME))
    child = json.loads(host.notified(protocol.AGENT_FRAME)[0]["line"])["result"]["pid"]
    assert os.getpgid(child) != os.getpgid(pid), "the child escaped the agent's group"

    await host.ask(protocol.AGENT_CANCEL, {"channel": 2}, request_id=3)

    await host.until(lambda: _gone(pid) and _gone(child))


def _tree(*, own_sessions: bool) -> tuple[subprocess.Popen[str], int]:
    """A process with a child under it, each in a session of its own or both in this one."""
    flag = "start_new_session=True" if own_sessions else ""
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time; print(subprocess.Popen([sys.executable, '-c',"
            f" 'import time; time.sleep(30)'], {flag}).pid, flush=True); time.sleep(30)",
        ],
        start_new_session=own_sessions,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    return parent, int(parent.stdout.readline())


def _ends(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while not _gone(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return _gone(pid)


def _end_quietly(parent: subprocess.Popen[str], child: int) -> None:
    for pid in (parent.pid, child):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    parent.wait(timeout=10)


def test_a_tree_is_read_off_ps_and_ended_with_what_left_its_group():
    # A leader of its own session, as the node starts an agent, with a child in
    # a session of its own under it, as Claude Code's shell tool starts a command.
    parent, child = _tree(own_sessions=True)
    try:
        assert os.getpgid(child) != os.getpgid(parent.pid)
        assert server._descendants(parent.pid) == [child]
        assert server._descendants(2**22 - 1) == []

        server._end_tree(parent.pid)

        assert parent.wait(timeout=10) != 0
        assert _ends(child), "the child's own session was ended with the tree"
    finally:
        _end_quietly(parent, child)


def test_a_tree_in_the_node_s_own_group_is_ended_one_by_one_and_the_group_stays():
    parent, child = _tree(own_sessions=False)
    try:
        assert os.getpgid(parent.pid) == os.getpgid(child) == os.getpgrp()

        server._end_tree(parent.pid)

        assert parent.wait(timeout=10) != 0
        assert _ends(child)
    finally:
        _end_quietly(parent, child)


@pytest.mark.slow
async def test_a_command_not_on_the_machine_and_a_full_node_are_refused_by_code(tmp_path, agent, monkeypatch):
    host = _Host()

    refused = await host.ask(
        protocol.AGENT_LAUNCH, {"channel": 1, "command": "no-such-agent-for-raven-tests --acp", "cwd": str(tmp_path)}
    )
    assert refused["error"] == {
        "code": protocol.NOT_FOUND,
        "message": "'no-such-agent-for-raven-tests' is not on this machine's PATH",
    }

    monkeypatch.setattr(protocol, "MAX_AGENTS", 0)
    full = await host.ask(protocol.AGENT_LAUNCH, {"channel": 1, "command": agent, "cwd": str(tmp_path)})
    assert full["error"] == {"code": protocol.BUSY, "message": "0 agents are already running on this machine"}
    assert host.node.agents == {}


async def test_a_root_the_machine_cannot_make_refuses_the_launch_with_the_reason(tmp_path, agent):
    host = _Host()
    (tmp_path / "f").write_text("a file\n", encoding="utf-8")

    refused = await host.ask(protocol.AGENT_LAUNCH, {"channel": 1, "command": agent, "cwd": str(tmp_path / "f" / "r")})

    assert refused["error"]["code"] == -32603
    assert refused["error"]["message"].startswith(f"could not make {tmp_path / 'f' / 'r'}: ")


@pytest.mark.parametrize(
    ("method", "params", "said"),
    [
        (protocol.AGENT_CANCEL, {"channel": 9}, "no agent on channel 9"),
        (protocol.AGENT_CLOSE, {"channel": "x"}, "channel must be a whole number"),
        (protocol.AGENT_LAUNCH, {"command": "x", "cwd": "/"}, "channel must be a whole number"),
        (protocol.AGENT_LAUNCH, {"channel": 1, "command": "", "cwd": "/"}, "command must be a non-empty string"),
        (
            protocol.AGENT_LAUNCH,
            {"channel": 1, "command": "x 'y", "cwd": "/"},
            "command cannot be parsed: No closing quotation",
        ),
        (
            protocol.AGENT_LAUNCH,
            {"channel": 1, "command": "x", "cwd": "/", "env": {"A": 1}},
            "env must be an object of strings",
        ),
        (protocol.PROBE_AGENT, {"command": "  "}, "command must be a non-empty string"),
    ],
)
async def test_a_channel_call_that_names_nothing_usable_is_refused_by_name(method, params, said):
    host = _Host()

    refused = await host.ask(method, params)

    assert refused["error"] == {"code": -32602, "message": said}


async def test_a_frame_for_a_channel_nobody_opened_is_dropped():
    host = _Host()

    await host.tell(protocol.AGENT_FRAME, {"channel": 9, "line": _frame("x")})
    await host.tell(protocol.AGENT_FRAME, {"channel": ["not", "a", "number"], "line": _frame("x")})

    assert host.frames() == []


async def test_probe_agent_answers_on_the_path_an_agent_here_would_get(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "capture_login_env", lambda: {"PATH": str(tmp_path)})
    host = _Host()
    found = tmp_path / "fake-agent"
    found.write_text("#!/bin/sh\n", encoding="utf-8")
    found.chmod(0o755)

    assert (await host.ask(protocol.PROBE_AGENT, {"command": "fake-agent --acp"}))["result"] == {"found": str(found)}
    assert (await host.ask(protocol.PROBE_AGENT, {"command": "nothing-here"}))["result"] == {"found": None}
    overridden = await host.ask(protocol.PROBE_AGENT, {"command": "fake-agent", "env": {"PATH": "/nonexistent"}})
    assert overridden["result"] == {"found": None}, "the entry's own PATH wins, as it does at launch"


@pytest.mark.slow
async def test_shutdown_ends_every_agent_and_the_loop_leaves_without_an_eof(tmp_path, agent):
    reader = asyncio.StreamReader()
    launch = {"jsonrpc": "2.0", "id": 1, "method": protocol.AGENT_LAUNCH}
    launch["params"] = {"channel": 1, "command": agent, "cwd": str(tmp_path)}
    reader.feed_data(json.dumps(launch).encode() + b"\n")
    reader.feed_data(json.dumps({"jsonrpc": "2.0", "id": 2, "method": protocol.SHUTDOWN}).encode() + b"\n")
    out = io.BytesIO()

    await asyncio.wait_for(server.serve(reader, out), timeout=30)

    answers = {
        frame["id"]: frame for frame in (json.loads(line) for line in out.getvalue().splitlines()) if "id" in frame
    }
    assert answers[2]["result"] == {}
    assert _gone(answers[1]["result"]["pid"])


async def test_requests_are_answered_as_they_finish_not_in_order(monkeypatch):
    async def slow(params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.3)
        return {"text": "slow"}

    monkeypatch.setitem(server._HANDLERS, "x/slow", slow)
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "x/slow"}).encode() + b"\n")
    reader.feed_data(json.dumps({"jsonrpc": "2.0", "id": 2, "method": protocol.PING}).encode() + b"\n")
    reader.feed_eof()
    out = io.BytesIO()

    await server.serve(reader, out)

    assert [frame["id"] for frame in (json.loads(line) for line in out.getvalue().splitlines())] == [2, 1]


# --- what the host writes, sends and collects --------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def test_stat_says_what_is_there_and_hashes_a_file_on_request(tree):
    roots = [str(tree)]

    file = await _result(protocol.STAT, {"path": str(tree / "b.md"), "roots": roots, "hash": True})
    folder = await _result(protocol.STAT, {"path": str(tree / "sub"), "roots": roots})
    missing = await _result(protocol.STAT, {"path": str(tree / "nothing"), "roots": roots})

    assert file["exists"] and file["kind"] == "file" and file["size"] == 4
    assert file["sha256"] == hashlib.sha256(b"hay\n").hexdigest()
    assert abs(file["mtime"] - (tree / "b.md").stat().st_mtime) < 1e-6
    assert folder["kind"] == "dir" and folder["exists"] and "sha256" not in folder
    assert missing == {"exists": False, "kind": "missing", "size": 0, "mtime": None}


async def test_a_call_outside_the_roots_is_refused_whatever_it_is(tree):
    outside = str(tree.parent / "secret.txt")
    for method in (protocol.STAT, protocol.PULL):
        code, said = await _refusal(method, {"path": outside, "roots": [str(tree)]})
        assert code == -32602 and "outside allowed directories" in said, method


async def test_a_write_through_the_node_is_the_write_the_host_would_have_made(tree):
    path = str(tree / "new" / "note.md")
    roots = [str(tree)]

    got = await _result(protocol.WRITE, {"path": path, "content": "one\n", "roots": roots})
    appended = await _result(protocol.WRITE, {"path": path, "content": "two\n", "mode": "append", "roots": roots})

    assert got["text"] == f"Successfully wrote 4 bytes to {path}"
    assert got["file_change"] == {"path": path, "after": "one\n", "before": None}
    assert got["diff"].startswith(f"--- {path}") and "+one" in got["diff"]
    assert appended == {"text": f"Successfully appended 4 bytes to {path}"}
    assert (tree / "new" / "note.md").read_text(encoding="utf-8") == "one\ntwo\n"


async def test_a_write_that_expects_the_file_as_it_was_read_is_refused_once_it_changed(tree):
    path = str(tree / "b.md")
    roots = [str(tree)]
    read = hashlib.sha256(b"hay\n").hexdigest()

    first = await _result(protocol.WRITE, {"path": path, "content": "x\n", "expect": read, "roots": roots})
    code, said = await _refusal(protocol.WRITE, {"path": path, "content": "y\n", "expect": read, "roots": roots})

    assert first["text"].startswith("Successfully wrote")
    assert (code, said) == (protocol.CHANGED, f"{path} changed after it was read; nothing was written")
    assert (tree / "b.md").read_text(encoding="utf-8") == "x\n"
    # A file expected absent that is there, and one expected absent that is.
    assert (await _refusal(protocol.WRITE, {"path": path, "content": "z\n", "expect": "absent", "roots": roots}))[
        0
    ] == protocol.CHANGED
    fresh = str(tree / "fresh.md")
    made = await _result(protocol.WRITE, {"path": fresh, "content": "z\n", "expect": "absent", "roots": roots})
    assert made["file_change"]["before"] is None and (tree / "fresh.md").read_text(encoding="utf-8") == "z\n"


async def test_an_edit_through_the_node_is_the_edit_the_host_would_have_made(tree):
    path = str(tree / "sub" / "a.py")
    roots = [str(tree)]

    got = await _result(
        protocol.EDIT, {"path": path, "old_text": "needle = 1", "new_text": "needle = 2", "roots": roots}
    )
    miss = await _result(
        protocol.EDIT, {"path": path, "old_text": "nothing like this", "new_text": "x", "roots": roots}
    )

    assert got["text"] == f"Successfully edited {path}"
    assert got["file_change"] == {"path": path, "after": "needle = 2\nhay = 2\n", "before": "needle = 1\nhay = 2\n"}
    assert "-needle = 1" in got["diff"] and "+needle = 2" in got["diff"]
    assert miss["text"].startswith(f"Error: old_text not found in {path}")
    assert (tree / "sub" / "a.py").read_text(encoding="utf-8") == "needle = 2\nhay = 2\n"


async def test_a_pull_returns_the_bytes_asked_for_with_the_whole_file_s_digest_first(tree):
    data = (tree / "big.txt").read_bytes()
    path = str(tree / "big.txt")
    roots = [str(tree)]

    first = await _result(protocol.PULL, {"path": path, "offset": 0, "length": 1000, "roots": roots})
    last = await _result(protocol.PULL, {"path": path, "offset": len(data) - 10, "length": 1000, "roots": roots})

    assert base64.b64decode(first["data"]) == data[:1000] and first["total"] == len(data)
    assert first["sha256"] == hashlib.sha256(data).hexdigest() and first["eof"] is False
    assert base64.b64decode(last["data"]) == data[-10:] and last["eof"] is True and "sha256" not in last
    assert (await _refusal(protocol.PULL, {"path": str(tree / "sub"), "roots": roots}))[1].endswith(
        "is not a file on this machine"
    )
    code, said = await _refusal(protocol.PULL, {"path": path, "length": protocol.CHUNK + 1, "roots": roots})
    assert code == -32602 and f"between 1 and {protocol.CHUNK}" in said


# --- tasks that outlive a connection ---------------------------------------------------


class _Conn(_Host):
    """One connection to a daemon several connections share."""

    def __init__(self, daemon: server.Daemon) -> None:
        self.out = io.BytesIO()
        self.node = server.Node(self.out, daemon)


def _record(daemon: server.Daemon, task: str) -> dict[str, Any]:
    return json.loads((daemon.record_dir(task) / "task.json").read_text(encoding="utf-8"))


@pytest.mark.slow
async def test_a_blank_line_an_agent_prints_is_forwarded_recorded_and_replayed(tmp_path):
    """A CLI agent's answer is its lines as printed: a blank line is a frame, numbered, kept and replayed like any."""
    daemon = server.Daemon(tmp_path / "node")
    first = _Conn(daemon)
    command = f"{sys.executable} -c \"print('a'); print(); print('b')\""
    await first.ask(
        protocol.AGENT_LAUNCH, {"channel": 1, "task": "t-blank", "command": command, "cwd": str(tmp_path / "w")}
    )
    await first.until(lambda: first.notified(protocol.AGENT_EXIT))

    assert [(f["line"], f["n"]) for f in first.notified(protocol.AGENT_FRAME)] == [("a", 1), ("", 2), ("b", 3)]
    assert (daemon.record_dir("t-blank") / "frames.jsonl").read_bytes() == b"a\n\nb\n"

    second = _Conn(daemon)
    await second.ask(protocol.AGENT_ATTACH, {"channel": 5, "task": "t-blank", "after": 1}, request_id=2)
    await second.until(lambda: second.notified(protocol.AGENT_EXIT))
    assert [(f["line"], f["n"]) for f in second.notified(protocol.AGENT_FRAME)] == [("", 2), ("b", 3)]


@pytest.mark.slow
async def test_a_task_is_recorded_as_it_runs_and_goes_on_when_its_connection_ends(tmp_path, agent):
    daemon = server.Daemon(tmp_path / "node")
    first = _Conn(daemon)
    started = await first.ask(
        protocol.AGENT_LAUNCH, {"channel": 1, "task": "t-one", "command": agent, "cwd": str(tmp_path / "w")}
    )
    assert started["result"]["task"] == "t-one"
    pid = started["result"]["pid"]
    await first.tell(protocol.AGENT_FRAME, {"channel": 1, "line": _frame("one")})
    await first.until(lambda: len(first.notified(protocol.AGENT_FRAME)) == 1)
    assert first.notified(protocol.AGENT_FRAME)[0]["n"] == 1
    assert _record(daemon, "t-one")["state"] == "running"

    await first.node.detach_all()

    assert not _gone(pid), "the agent runs on without its connection"
    task = daemon.tasks["t-one"]
    assert task.subscriber is None and daemon.connections == set()
    await task.write(_frame("two", 2))
    await first.until(lambda: task.frames == 2)
    assert (daemon.record_dir("t-one") / "frames.jsonl").read_text(encoding="utf-8").count("\n") == 2

    second = _Conn(daemon)
    attached = await second.ask(protocol.AGENT_ATTACH, {"channel": 7, "task": "t-one", "after": 1}, request_id=2)
    assert attached["result"]["replayed"] == 1 and attached["result"]["rc"] is None
    assert [f["n"] for f in second.notified(protocol.AGENT_FRAME)] == [2], "only what the host had not read"
    assert second.node.channels[7] is task
    await second.ask(protocol.AGENT_CLOSE, {"channel": 7}, request_id=3)
    await second.until(lambda: second.notified(protocol.AGENT_EXIT))
    assert second.notified(protocol.AGENT_EXIT) == [{"channel": 7, "rc": 0}]
    assert first.notified(protocol.AGENT_EXIT) == [], "the connection that ended hears nothing"
    assert _record(daemon, "t-one")["state"] == "exited" and _record(daemon, "t-one")["rc"] == 0
    assert 7 not in second.node.channels and daemon.running() == []


@pytest.mark.slow
async def test_a_finished_task_is_replayed_from_its_record_by_a_daemon_that_never_ran_it(tmp_path, agent):
    root = tmp_path / "node"
    first = _Conn(server.Daemon(root))
    await first.ask(protocol.AGENT_LAUNCH, {"channel": 1, "task": "t-done", "command": agent, "cwd": str(tmp_path)})
    await first.tell(protocol.AGENT_FRAME, {"channel": 1, "line": _frame("a")})
    await first.tell(protocol.AGENT_FRAME, {"channel": 1, "line": _frame("b", 2)})
    await first.until(lambda: len(first.notified(protocol.AGENT_FRAME)) == 2)
    await first.ask(protocol.AGENT_CLOSE, {"channel": 1}, request_id=2)
    await first.until(lambda: first.notified(protocol.AGENT_EXIT))

    later = _Conn(server.Daemon(root))
    attached = await later.ask(protocol.AGENT_ATTACH, {"channel": 1, "task": "t-done", "after": 0})

    assert attached["result"]["replayed"] == 2 and attached["result"]["rc"] == 0
    assert [f["n"] for f in later.notified(protocol.AGENT_FRAME)] == [1, 2]
    assert later.notified(protocol.AGENT_EXIT) == [{"channel": 1, "rc": 0}]
    assert 1 not in later.node.channels, "a finished task holds no channel"


async def test_a_record_left_running_by_a_daemon_that_died_is_read_as_lost(tmp_path):
    root = tmp_path / "node"
    daemon = server.Daemon(root)
    record = daemon.record_dir("t-gone")
    record.mkdir(parents=True)
    (record / "task.json").write_text(
        json.dumps({"task": "t-gone", "state": "running", "pid": 1, "started": 1.0, "frames": 1}), encoding="utf-8"
    )
    (record / "frames.jsonl").write_text('{"jsonrpc":"2.0","id":1,"result":{}}\n', encoding="utf-8")
    conn = _Conn(daemon)

    attached = await conn.ask(protocol.AGENT_ATTACH, {"channel": 1, "task": "t-gone"})

    assert attached["result"]["rc"] == server.LOST_RC and attached["result"]["replayed"] == 1
    assert conn.notified(protocol.AGENT_EXIT) == [{"channel": 1, "rc": server.LOST_RC}]
    assert _record(daemon, "t-gone")["state"] == "lost"


@pytest.mark.parametrize(
    ("params", "code", "said"),
    [
        ({"channel": 1, "task": "t-nobody"}, protocol.UNKNOWN_TASK, "no task 't-nobody' is running or recorded"),
        ({"channel": 1, "task": "../etc"}, -32602, "task must be a short name"),
        ({"channel": 1, "task": "t-x", "after": -1}, -32602, "after must be a whole number"),
    ],
)
async def test_an_attach_that_names_nothing_usable_is_refused_by_code(tmp_path, params, code, said):
    conn = _Conn(server.Daemon(tmp_path / "node"))

    answer = await conn.ask(protocol.AGENT_ATTACH, params)

    assert answer["error"]["code"] == code and said in answer["error"]["message"]


@pytest.mark.slow
async def test_a_launch_under_a_running_task_s_id_is_refused_and_the_list_names_every_task(tmp_path, agent):
    daemon = server.Daemon(tmp_path / "node")
    old = daemon.record_dir("t-old")
    old.mkdir(parents=True)
    (old / "task.json").write_text(
        json.dumps({"task": "t-old", "state": "exited", "rc": 0, "started": 1.0, "ended": 2.0, "frames": 3}),
        encoding="utf-8",
    )
    conn = _Conn(daemon)
    await conn.ask(protocol.AGENT_LAUNCH, {"channel": 1, "task": "t-run", "command": agent, "cwd": str(tmp_path)})

    again = await conn.ask(
        protocol.AGENT_LAUNCH, {"channel": 2, "task": "t-run", "command": agent, "cwd": str(tmp_path)}, request_id=2
    )
    listed = (await conn.ask(protocol.TASK_LIST, {}, request_id=3))["result"]["tasks"]
    hello = (await conn.ask(protocol.HELLO, {"protocol": protocol.PROTOCOL}, request_id=4))["result"]

    assert again["error"]["code"] == -32602 and "already running; attach to it" in again["error"]["message"]
    assert [(row["task"], row["state"], row["attached"]) for row in listed] == [
        ("t-old", "exited", False),
        ("t-run", "running", True),
    ]
    assert hello["mode"] == "daemon" and hello["tasks"] == 1
    await conn.ask(protocol.AGENT_CANCEL, {"channel": 1}, request_id=5)
    await conn.until(lambda: conn.notified(protocol.AGENT_EXIT))


def test_sweep_drops_the_records_of_tasks_that_ended_long_ago_and_keeps_the_rest(tmp_path):
    daemon = server.Daemon(tmp_path / "node")
    for name, state, ended in (
        ("t-ancient", "exited", time.time() - protocol.TASK_KEEP_S - 60),
        ("t-recent", "exited", time.time() - 60),
        ("t-still", "running", None),
    ):
        record = daemon.record_dir(name)
        record.mkdir(parents=True)
        (record / "task.json").write_text(json.dumps({"task": name, "state": state, "ended": ended}), encoding="utf-8")

    assert daemon.sweep() == 1
    assert sorted(p.name for p in (tmp_path / "node" / server.TASKS_DIR).iterdir()) == ["t-recent", "t-still"]


@pytest.mark.slow
async def test_a_task_s_records_are_its_owner_s_alone(tmp_path, agent):
    daemon = server.Daemon(tmp_path / "node")
    conn = _Conn(daemon)

    started = await conn.ask(
        protocol.AGENT_LAUNCH, {"channel": 1, "task": "t-own", "command": agent, "cwd": str(tmp_path / "w")}
    )

    try:
        assert started["result"]["task"] == "t-own"
        record = daemon.record_dir("t-own")
        assert (tmp_path / "node" / server.TASKS_DIR).stat().st_mode & 0o777 == 0o700
        assert record.stat().st_mode & 0o777 == 0o700
        assert {p.name: p.stat().st_mode & 0o777 for p in record.iterdir()} == {
            "task.json": 0o600,
            "frames.jsonl": 0o600,
            "stderr.log": 0o600,
        }, "the frames hold the agent's output as it was"
    finally:
        daemon.tasks["t-own"].end()
        await daemon.tasks["t-own"].proc.wait()


@pytest.mark.slow
async def test_shutdown_from_a_host_leaves_a_daemon_s_tasks_running(tmp_path, agent):
    daemon = server.Daemon(tmp_path / "node")
    reader = asyncio.StreamReader()
    launch = {"jsonrpc": "2.0", "id": 1, "method": protocol.AGENT_LAUNCH}
    launch["params"] = {"channel": 1, "task": "t-keep", "command": agent, "cwd": str(tmp_path)}
    reader.feed_data(json.dumps(launch).encode() + b"\n")
    reader.feed_data(json.dumps({"jsonrpc": "2.0", "id": 2, "method": protocol.SHUTDOWN}).encode() + b"\n")
    out = io.BytesIO()

    await asyncio.wait_for(server.serve(reader, out, daemon), timeout=30)

    answers = {f["id"]: f for f in (json.loads(line) for line in out.getvalue().splitlines()) if "id" in f}
    pid = answers[1]["result"]["pid"]
    try:
        assert not _gone(pid) and daemon.tasks["t-keep"].subscriber is None
    finally:
        daemon.tasks["t-keep"].end()
        await daemon.tasks["t-keep"].proc.wait()


@pytest.mark.slow
async def test_the_daemon_serves_on_its_socket_across_connections_and_leaves_when_idle(tmp_path, agent, monkeypatch):
    monkeypatch.setattr(server, "_IDLE_CHECK_S", 0.1)
    root = tmp_path / "n"
    running = asyncio.create_task(server.run_daemon_async(root, idle_s=0.3))
    sock = server.socket_path(root)
    deadline = time.monotonic() + 10
    while not sock.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    async def ask(
        writer: asyncio.StreamWriter, reader: asyncio.StreamReader, request: dict[str, Any]
    ) -> dict[str, Any]:
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        while True:
            frame = json.loads(await reader.readline())
            if frame.get("id") == request["id"]:
                return frame

    reader, writer = await asyncio.open_unix_connection(str(sock))
    hello = await ask(writer, reader, {"jsonrpc": "2.0", "id": 1, "method": protocol.HELLO, "params": {}})
    assert hello["result"]["mode"] == "daemon" and hello["result"]["pid"] == os.getpid()
    launch = {"channel": 1, "task": "t-sock", "command": agent, "cwd": str(tmp_path)}
    started = await ask(writer, reader, {"jsonrpc": "2.0", "id": 2, "method": protocol.AGENT_LAUNCH, "params": launch})
    pid = started["result"]["pid"]
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.2)
    assert not _gone(pid), "the agent survives its connection"

    reader, writer = await asyncio.open_unix_connection(str(sock))
    attach = {"channel": 3, "task": "t-sock", "after": 0}
    answer = await ask(writer, reader, {"jsonrpc": "2.0", "id": 3, "method": protocol.AGENT_ATTACH, "params": attach})
    assert answer["result"]["rc"] is None
    await ask(writer, reader, {"jsonrpc": "2.0", "id": 4, "method": protocol.AGENT_CANCEL, "params": {"channel": 3}})
    exited = json.loads(await reader.readline())
    assert exited["method"] == protocol.AGENT_EXIT and exited["params"]["channel"] == 3
    writer.close()
    await writer.wait_closed()

    assert await asyncio.wait_for(running, timeout=10) == 0, "nothing to do: the daemon leaves"
    assert not sock.exists()
    assert _record(server.Daemon(root), "t-sock")["state"] == "exited"


@pytest.mark.slow
async def test_a_second_daemon_under_the_same_directory_leaves_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_IDLE_CHECK_S", 0.1)
    root = tmp_path / "n"
    first = asyncio.create_task(server.run_daemon_async(root, idle_s=0.5))
    deadline = time.monotonic() + 10
    while not server.socket_path(root).exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    assert await asyncio.wait_for(server.run_daemon_async(root), timeout=5) == 0
    assert server.socket_path(root).exists(), "the first daemon's socket is untouched"
    assert await asyncio.wait_for(first, timeout=10) == 0


def test_the_socket_s_directory_is_this_user_s_own_or_is_not_used(tmp_path, monkeypatch):
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    assert server.socket_path(Path("/srv/node")) == Path("/srv/node") / server.RUN_DIR / f"{bundle.install_name()}.sock"
    sock = server.socket_path(tmp_path / ("n" * 120))
    assert sock.parent == tmp_path / "tmp" / f"raven-node-{os.getuid()}" and sock.suffix == ".sock"
    assert len(os.fsencode(sock)) < len(os.fsencode(tmp_path / ("n" * 120) / server.RUN_DIR))

    assert server.own_dir(sock.parent) == sock.parent
    assert sock.parent.stat().st_mode & 0o777 == 0o700
    os.chmod(sock.parent, 0o755)
    server.own_dir(sock.parent)
    assert sock.parent.stat().st_mode & 0o777 == 0o700, "this user's, with wider bits: narrowed"

    (tmp_path / "file").write_text("", encoding="utf-8")
    (tmp_path / "real").mkdir()
    os.chmod(tmp_path / "real", 0o755)  # an explicit mode: mkdir's is whatever the umask leaves
    (tmp_path / "link").symlink_to(tmp_path / "real")
    for other in (tmp_path / "file", tmp_path / "link"):
        with pytest.raises(PermissionError, match="is not a directory only this user may open"):
            server.own_dir(other)
    assert server.owned(tmp_path / "file") and server.owned(tmp_path / "nothing-there")

    me = os.getuid()
    monkeypatch.setattr(server.os, "getuid", lambda: me + 1)
    with pytest.raises(PermissionError, match="is not a directory only this user may open"):
        server.own_dir(tmp_path / "real")
    assert (tmp_path / "real").stat().st_mode & 0o777 == 0o755, "another user's: left as it is"
    assert not server.owned(tmp_path / "file") and server.owned(tmp_path / "nothing-there")


@pytest.mark.slow
async def test_the_daemon_does_not_listen_beside_a_thing_it_cannot_remove_from_its_socket_s_place(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    root = tmp_path / "n"
    sock = server.socket_path(root)
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.mkdir()

    with pytest.raises(PermissionError, match="is in the way of the daemon and cannot be removed"):
        await server.run_daemon_async(root, idle_s=0.3)

    assert sock.is_dir() and not server.pid_path(root).exists()


async def test_a_whole_file_s_digest_is_taken_off_the_loop_the_heartbeat_is_answered_on(tree, monkeypatch):
    digest = server._sha256

    def slowly(path: Path) -> str:
        time.sleep(0.4)
        return digest(path)

    monkeypatch.setattr(server, "_sha256", slowly)
    read = asyncio.create_task(_result(protocol.READ, {"path": str(tree / "b.md"), "roots": [str(tree)]}))
    await asyncio.sleep(0.05)
    before = time.monotonic()

    assert await _result(protocol.PING, {"t": 1}) == {"t": 1}

    assert time.monotonic() - before < 0.3, "answered while the digest was still being taken"
    assert (await read)["sha256"] == hashlib.sha256(b"hay\n").hexdigest()


async def test_a_read_never_lands_inside_a_write_of_the_same_file(tree, monkeypatch):
    """The page and the digest a read answers with are one state of the file, whatever a write does meanwhile."""
    digest = server._sha256

    def slowly(path: Path) -> str:
        time.sleep(0.3)
        return digest(path)

    monkeypatch.setattr(server, "_sha256", slowly)
    path = str(tree / "b.md")
    read = asyncio.create_task(_result(protocol.READ, {"path": path, "roots": [str(tree)]}))
    await asyncio.sleep(0.05)
    write = asyncio.create_task(_result(protocol.WRITE, {"path": path, "content": "straw\n", "roots": [str(tree)]}))

    page, written = await asyncio.gather(read, write)

    assert page["text"].startswith("1| hay") and page["sha256"] == hashlib.sha256(b"hay\n").hexdigest()
    assert written["ok"] and (tree / "b.md").read_text(encoding="utf-8") == "straw\n", "the write landed after"


def test_a_writer_that_waits_goes_before_new_readers():
    gate = server._FileGate()
    first_in = threading.Event()
    writer_waiting = threading.Event()
    writer_done = threading.Event()
    order: list[str] = []

    def first_reader() -> None:
        with gate.reading():
            first_in.set()
            writer_waiting.wait(5)
            time.sleep(0.1)
            order.append("first reader out")

    def writer() -> None:
        writer_waiting.set()
        with gate.writing():
            order.append("writer")
        writer_done.set()

    def second_reader() -> None:
        with gate.reading():
            order.append("second reader")

    threads = [
        threading.Thread(target=first_reader),
        threading.Thread(target=writer),
        threading.Thread(target=second_reader),
    ]
    threads[0].start()
    first_in.wait(5)
    threads[1].start()
    writer_waiting.wait(5)
    time.sleep(0.02)
    threads[2].start()
    for thread in threads:
        thread.join(5)

    assert order == ["first reader out", "writer", "second reader"]


@pytest.mark.slow
async def test_a_page_read_of_a_large_file_keeps_the_heartbeat_answered(tmp_path):
    """The page is read a chunk at a time, so no one step holds the interpreter for the whole file."""
    big = tmp_path / "big.log"
    with big.open("wb") as handle:
        line = b"2026-10-13 12:00:00 one line of a log that goes on for a while, as log lines do\n"
        for _ in range(120 * 1024 * 1024 // len(line)):
            handle.write(line)
    read = asyncio.create_task(_result(protocol.READ, {"path": str(big), "limit": 3, "roots": [str(tmp_path)]}))
    gaps: list[float] = []
    last = time.monotonic()
    while not read.done():
        assert await _result(protocol.PING, {"t": 1}) == {"t": 1}
        now = time.monotonic()
        gaps.append(now - last)
        last = now
        await asyncio.sleep(0.005)

    assert max(gaps) < 0.25, f"a ping waited {max(gaps):.2f}s on the read"
    assert (await read)["text"].startswith("1| 2026-10-13 12:00:00 one line")


@pytest.mark.parametrize("argv", [["--serve"], ["--attach"]])
def test_serve_and_attach_need_the_node_directory(argv, capsys):
    with pytest.raises(SystemExit):
        node_main.main(argv)
    assert "--dir" in capsys.readouterr().err
