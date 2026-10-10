"""raven.node.server: Raven's own file tools, answered over stdio.

The property everything else rests on is parity: a file call a node answers
comes back exactly as the same tool's answer on the host's own disk, footer
and limits included. Then the fence: a call reaches only the roots it names,
and one that names none is refused rather than read as "no fence".
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

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
    assert got == {"jsonrpc": "2.0", "id": 1, "result": {"text": want}}
    assert "line 200 of the file" in want and "line 199 of the file" not in want


async def test_a_read_with_no_offset_starts_at_the_first_line(tree):
    path = str(tree / "b.md")

    got = await _result(protocol.READ, {"path": path, "roots": [str(tree)]})

    assert got == {"text": await _local(ReadFileTool, tree).execute(path)}


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
    assert got == {"text": want.model_text, "display": want.display_text, "ok": True, "blocks": want.blocks}
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
    assert got == {"text": await _local(ReadFileTool, tree).execute(outside)}


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
    assert await _refusal("fs/write", {}) == (-32601, "unknown method 'fs/write'")


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


@pytest.mark.parametrize("argv", [[], ["--stdio", "--version"]])
def test_a_node_is_started_for_exactly_one_purpose(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        node_main.main(argv)

    assert caught.value.code == 2
