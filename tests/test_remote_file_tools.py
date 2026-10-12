"""read_file, list_dir, find and grep on a registered machine, answered by its Raven node.

A remote path never touches this computer's disk. The tool places it on its
machine against the login home the node reported, fences the call to the root
it is under (else to the directory the permission check asked about), sends it
to the machine's node, and shows every path in the answer the way scp writes
it. The unit tests stand in for the node pool; the last ones run the real node
behind the stand-in ssh of tests/test_node_client.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.agent.tools.file_search import FindTool, GrepTool
from raven.agent.tools.filesystem import ListDirTool, ReadFileTool
from raven.contracts.tool import ToolResult
from raven.node.client import NodeError, NodePool
from raven.ops import remote_path
from tests.test_node_client import HOST, PORT, Machine, row

HOME = "/home/worker"
SHOWN = f"scp://worker@{HOST}:{PORT}/"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(remote_path, "_HOMES", {})
    monkeypatch.setattr(remote_path, "_NODES", None)
    monkeypatch.setattr(
        "raven.config.loader.load_config", lambda *a, **k: SimpleNamespace(subagents=SimpleNamespace(agents=[]))
    )


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": [row()]}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(path))
    monkeypatch.setenv("RAVEN_DEBUG_MACHINES", "1")
    return path


class _Pool:
    """A node pool that answers each call with ``answer`` and records it."""

    def __init__(self, answer: dict[str, Any] | None = None, *, fails: str = "") -> None:
        self.answer = answer if answer is not None else {"text": "ok"}
        self.fails = fails
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def home(self, machine_id: str) -> str:
        if self.fails:
            raise NodeError(self.fails)
        return HOME

    async def call(self, machine_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((machine_id, method, params))
        return self.answer


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> _Pool:
    nodes = _Pool()
    remote_path.use_nodes(lambda: nodes)
    return nodes


def _tools(tmp_path: Path) -> dict[str, Any]:
    fenced = {"workspace": tmp_path, "allowed_dirs": (tmp_path,), "follow_binding": False}
    return {
        "read_file": ReadFileTool(**fenced),
        "list_dir": ListDirTool(**fenced),
        "find": FindTool(**fenced),
        "grep": GrepTool(**fenced),
    }


# --- what reaches the node ------------------------------------------------------------


async def test_a_read_goes_to_the_machine_s_node_placed_and_fenced(registry, pool, tmp_path):
    pool.answer = {"text": "2| second\n\n(Showing lines 2-3 of 9. Use offset=4 to continue.)"}

    got = await _tools(tmp_path)["read_file"].execute("box:raven-work/t1/a.md", offset=2, limit=3)

    assert got == pool.answer["text"]
    assert pool.calls == [
        (
            "box",
            "fs/read",
            {"offset": 2, "limit": 3, "path": f"{HOME}/raven-work/t1/a.md", "roots": [f"{HOME}/raven-work"]},
        )
    ]


async def test_list_find_and_grep_send_their_own_arguments(registry, pool, tmp_path):
    tools = _tools(tmp_path)

    await tools["list_dir"].execute("box:raven-work", recursive=True, max_entries=5)
    await tools["find"].execute("*.py", path="box:/srv/code", limit=9)
    await tools["grep"].execute(
        "needle", path="box:", glob="*.md", output_mode="files", case_insensitive=True, context=2, limit=4
    )

    assert pool.calls == [
        (
            "box",
            "fs/list",
            {"recursive": True, "max_entries": 5, "path": f"{HOME}/raven-work", "roots": [f"{HOME}/raven-work"]},
        ),
        ("box", "fs/find", {"pattern": "*.py", "limit": 9, "path": "/srv/code", "roots": ["/srv/code"]}),
        (
            "box",
            "fs/grep",
            {
                "pattern": "needle",
                "glob": "*.md",
                "output_mode": "files",
                "case_insensitive": True,
                "context": 2,
                "limit": 4,
                "path": HOME,
                "roots": [HOME],
            },
        ),
    ]


async def test_every_path_in_the_answer_is_shown_the_way_scp_writes_it(registry, pool, tmp_path):
    pool.answer = {
        "text": (
            f"Error: Path {HOME}/raven-work/t1/link is outside allowed directories {HOME}/raven-work; "
            f"also {HOME}/raven-work/t1/link"
        )
    }

    got = await _tools(tmp_path)["read_file"].execute("box:raven-work/t1/link")

    assert got == (
        f"Error: Path {SHOWN}{HOME}/raven-work/t1/link is outside allowed directories {SHOWN}{HOME}/raven-work; "
        f"also {SHOWN}{HOME}/raven-work/t1/link"
    )


async def test_an_image_comes_back_as_a_result_with_its_blocks(registry, pool, tmp_path):
    pool.answer = {
        "text": f"Image {HOME}/raven-work/p.png (2x2)",
        "display": f"{HOME}/raven-work/p.png",
        "ok": True,
        "blocks": [
            {"type": "text", "text": f"Image {HOME}/raven-work/p.png (2x2)"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }

    got = await _tools(tmp_path)["read_file"].execute("box:raven-work/p.png")

    assert isinstance(got, ToolResult)
    assert got.model_text == f"Image {SHOWN}{HOME}/raven-work/p.png (2x2)"
    assert got.display_text == f"{SHOWN}{HOME}/raven-work/p.png"
    assert got.blocks == [
        {"type": "text", "text": f"Image {SHOWN}{HOME}/raven-work/p.png (2x2)"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    assert got.ok


# --- what never reaches it ----------------------------------------------------------------


async def test_a_path_that_names_no_machine_is_refused_and_nothing_is_sent(registry, pool, tmp_path):
    got = await _tools(tmp_path)["read_file"].execute("nobox:/etc/hostname")

    assert got.startswith("Error: 'nobox:/etc/hostname' is written as a path on another machine")
    assert "./name" in got
    assert pool.calls == []


async def test_a_process_without_nodes_says_so(registry, tmp_path):
    got = await _tools(tmp_path)["list_dir"].execute("box:raven-work")

    assert (
        got == "Error: box:raven-work is on another machine, and this process does not read registered machines' files"
    )


async def test_a_node_that_cannot_answer_is_one_sentence(registry, monkeypatch, tmp_path):
    failing = _Pool(fails="Raven node 0.1-x is not installed on machine 'Lab box' (box); install it with `x`")
    remote_path.use_nodes(lambda: failing)

    got = await _tools(tmp_path)["grep"].execute("x", path="box:raven-work")

    assert got == f"Error: {failing.fails}"
    assert HOST not in got


async def test_with_the_lane_off_a_remote_path_is_read_as_it_always_was(registry, pool, tmp_path, monkeypatch):
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES")

    got = await _tools(tmp_path)["read_file"].execute("box:raven-work/t1/a.md")

    assert got.startswith("Error: File not found: box:raven-work/t1/a.md")
    assert pool.calls == []


async def test_a_local_path_is_read_here_as_before(registry, pool, tmp_path):
    (tmp_path / "notes.md").write_text("here\n", encoding="utf-8")

    got = await _tools(tmp_path)["read_file"].execute(str(tmp_path / "notes.md"))

    assert "here" in got
    assert pool.calls == []


@pytest.mark.parametrize("path", ["\\\\?\\C:\\work\\review_marker.txt", "C:review_marker.txt", "\\\\box\\share\\f"])
async def test_a_windows_path_form_is_this_computer_s_not_a_machine_s(registry, pool, tmp_path, monkeypatch, path):
    # Drive-relative and extended paths, as a Windows host writes them: the lane
    # on, each is left to this computer's tool rather than refused as a machine nobody registered.
    monkeypatch.setattr(remote_path, "_WINDOWS", True)

    got = await _tools(tmp_path)["read_file"].execute(path)

    assert "registered machine" not in got and "machine" not in got.lower().split("error")[-1][:40], got
    assert pool.calls == []


# --- the real node, behind a stand-in ssh ----------------------------------------------------


@pytest.fixture
async def real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not Path("/bin/sh").exists():
        pytest.skip("the stand-in ssh hands the launch line to a POSIX shell")
    machine = Machine(tmp_path, monkeypatch)
    machine.node()
    work = machine.home / "raven-work" / "t1"
    work.mkdir(parents=True)
    (work / "notes.txt").write_text("".join(f"note {i}\n" for i in range(1, 21)), encoding="utf-8")
    (machine.home / "secret.txt").write_text("top secret\n", encoding="utf-8")
    (work / "link.txt").symlink_to(machine.home / "secret.txt")
    nodes = NodePool()
    remote_path.use_nodes(lambda: nodes)
    yield machine
    await nodes.close_all()


@pytest.mark.slow
async def test_a_remote_read_through_the_real_node_matches_a_local_one(real, tmp_path):
    home = str(real.home)
    local = ReadFileTool(workspace=real.home, allowed_dirs=(real.home,), follow_binding=False)

    got = await _tools(tmp_path)["read_file"].execute("box:raven-work/t1/notes.txt", offset=5, limit=3)

    assert got == await local.execute(f"{home}/raven-work/t1/notes.txt", offset=5, limit=3)
    assert remote_path.known_home("box") == home
    listed = await _tools(tmp_path)["list_dir"].execute("box:raven-work/t1")
    assert "notes.txt" in listed
    hits = await _tools(tmp_path)["grep"].execute("note 7", path="box:raven-work")
    assert "notes.txt" in hits


@pytest.mark.slow
async def test_a_link_out_of_the_root_is_not_followed_and_the_refusal_shows_scp_paths(real, tmp_path):
    home = str(real.home)

    got = await _tools(tmp_path)["read_file"].execute("box:raven-work/t1/link.txt")

    assert "top secret" not in got
    assert f"{SHOWN}{home}/raven-work/t1/link.txt" in got
    assert f" {home}/" not in got, "every path is shown the way scp writes it"


# --- assembly ---------------------------------------------------------------------------


def test_assembly_hands_the_file_tools_this_process_s_node_pool(tmp_path, monkeypatch):
    from raven.config.raven import MemoryConfig, PluginsConfig, RavenConfig
    from raven.config.schema import Config
    from raven.contracts.llm_provider import LLMResponse
    from raven.core import runtime
    from raven.node.client import get_node_pool
    from raven.providers.base import LLMProvider

    monkeypatch.setattr(runtime.token_wise_stack, "install_from_config", lambda *a, **k: None)
    monkeypatch.setattr(runtime.token_wise_stack, "caching_probe", lambda *a, **k: False)

    class _Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        async def chat(self, messages, **kwargs):
            return LLMResponse(content="ok", finish_reason="stop")

        def get_default_model(self) -> str:
            return "fake/default"

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    ec_config = RavenConfig(memory=MemoryConfig(backend=None), plugins=PluginsConfig(), base=config)
    assert remote_path.nodes() is None

    built = runtime.build_runtime(config, ec_config, provider=_Provider())
    try:
        assert remote_path.nodes() is get_node_pool()
    finally:
        built.discard()


# --- writes: the same way there, with what was read as the expectation --------------------


def _writers(tmp_path: Path) -> dict[str, Any]:
    from raven.agent.tools.filesystem import EditFileTool, WriteFileTool

    fenced = {"workspace": tmp_path, "allowed_dirs": (tmp_path,), "follow_binding": False}
    return {"write_file": WriteFileTool(**fenced), "edit_file": EditFileTool(**fenced)}


async def test_a_read_s_own_lines_come_back_as_they_are_and_only_the_tool_s_sentences_show_addresses(
    registry, pool, tmp_path
):
    pool.answer = {
        "text": f"1| export ROOT={HOME}/raven-work\n2| cd {HOME}/raven-work/t1\n\n(Showing lines 1-2 of 2. Use offset=3 to continue.)",
        "sha256": "ab" * 32,
    }

    got = await _tools(tmp_path)["read_file"].execute("box:raven-work/t1/env.sh")

    assert got.startswith(f"1| export ROOT={HOME}/raven-work\n2| cd {HOME}/raven-work/t1\n"), "the file's bytes"
    assert SHOWN not in got.split("\n\n")[0] and "(Showing lines 1-2 of 2." in got


async def test_a_search_s_matches_and_a_diff_s_body_keep_their_lines(registry, pool, tmp_path):
    pool.answer = {
        "text": f"t1/env.sh:1:export ROOT={HOME}/raven-work\nt1/env.sh-2-cd {HOME}/raven-work/t1\n\n1 match in 1 file"
    }
    matches = await _tools(tmp_path)["grep"].execute("ROOT", "box:raven-work")

    place = f"{HOME}/raven-work/t1/env.sh"
    pool.answer = {
        "text": f"Successfully wrote 30 bytes to {place}",
        "display": None,
        "ok": True,
        "blocks": [],
        "diff": f"--- {place}\n+++ {place}\n@@ -0,0 +1 @@\n+export ROOT={HOME}/raven-work",
        "file_change": {"path": place, "after": f"export ROOT={HOME}/raven-work\n", "before": None},
    }
    written = await _writers(tmp_path)["write_file"].execute(
        "box:raven-work/t1/env.sh", f"export ROOT={HOME}/raven-work\n"
    )

    assert matches.startswith(f"t1/env.sh:1:export ROOT={HOME}/raven-work\nt1/env.sh-2-cd {HOME}/raven-work/t1\n")
    assert SHOWN not in matches
    assert isinstance(written, ToolResult)
    assert written.model_text == f"Successfully wrote 30 bytes to {SHOWN}{place}"
    assert written.diff == f"--- {SHOWN}{place}\n+++ {SHOWN}{place}\n@@ -0,0 +1 @@\n+export ROOT={HOME}/raven-work"
    assert written.file_change is not None and written.file_change.path == f"{SHOWN}{place}"


async def test_a_write_and_an_edit_go_to_the_node_and_come_back_as_the_tool_s_result(registry, pool, tmp_path):
    place = f"{HOME}/raven-work/t1/a.md"
    pool.answer = {
        "text": f"Successfully wrote 4 bytes to {place}",
        "display": None,
        "ok": True,
        "blocks": [],
        "diff": f"--- {place}\n+++ {place}\n@@ -0,0 +1 @@\n+one",
        "file_change": {"path": place, "after": "one\n", "before": None},
    }
    tools = _writers(tmp_path)

    written = await tools["write_file"].execute("box:raven-work/t1/a.md", "one\n")
    await tools["edit_file"].execute("box:raven-work/t1/a.md", "one", "two", replace_all=True)

    assert isinstance(written, ToolResult)
    assert written.model_text == f"Successfully wrote 4 bytes to {SHOWN}{place}"
    assert written.diff == f"--- {SHOWN}{place}\n+++ {SHOWN}{place}\n@@ -0,0 +1 @@\n+one"
    assert written.file_change is not None and written.file_change.path == f"{SHOWN}{place}"
    assert written.file_change.after == "one\n" and written.file_change.before is None
    assert pool.calls == [
        ("box", "fs/write", {"content": "one\n", "mode": "overwrite", "path": place, "roots": [f"{HOME}/raven-work"]}),
        (
            "box",
            "fs/edit",
            {"old_text": "one", "new_text": "two", "replace_all": True, "path": place, "roots": [f"{HOME}/raven-work"]},
        ),
    ]


async def test_a_read_s_digest_is_what_the_next_overwrite_expects_and_a_write_forgets_it(registry, pool, tmp_path):
    place = f"{HOME}/raven-work/t1/a.md"
    pool.answer = {"text": "1| one", "sha256": "ab" * 32}
    await _tools(tmp_path)["read_file"].execute("box:raven-work/t1/a.md")
    pool.answer = {"text": "ok"}
    tools = _writers(tmp_path)

    await tools["write_file"].execute("box:raven-work/t1/a.md", "two\n")
    await tools["write_file"].execute("box:raven-work/t1/a.md", "three\n")
    await _tools(tmp_path)["read_file"].execute("box:raven-work/t1/a.md")
    await tools["write_file"].execute("box:raven-work/t1/a.md", "four\n", mode="append")
    await tools["write_file"].execute("box:raven-work/t1/a.md", "five\n")

    sent = [params for _, method, params in pool.calls if method == "fs/write"]
    assert sent[0]["expect"] == "ab" * 32, "the whole file is replaced: say what it held when it was read"
    assert "expect" not in sent[1], "after a write of its own the file is as this process left it"
    assert "expect" not in sent[2], "an append replaces nothing"
    assert "expect" not in sent[3], "and leaves the file other than it was read, so the next overwrite expects nothing"


async def test_a_file_changed_since_it_was_read_is_left_alone_and_the_sentence_says_to_read_again(registry, tmp_path):
    class _Changed(_Pool):
        async def call(self, machine_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((machine_id, method, params))
            raise NodeError("the Raven node on machine 'Lab box' (box) refused fs/write: changed", reason="changed")

    changed = _Changed()
    remote_path.use_nodes(lambda: changed)
    remote_path.remember_read("box", f"{HOME}/raven-work/t1/a.md", "ab" * 32)

    got = await _writers(tmp_path)["write_file"].execute("box:raven-work/t1/a.md", "two\n")

    assert got == (
        f"Error: {SHOWN}{HOME}/raven-work/t1/a.md on machine 'Lab box' (box) changed after it was read; "
        "nothing was written. Read it again before writing."
    )
    assert HOST not in got.split("on machine")[1]
    assert remote_path.last_read("box", f"{HOME}/raven-work/t1/a.md") is None, "the next write reads first"


def test_the_approval_card_for_a_write_on_a_machine_names_the_machine_and_the_path_as_written(registry, tmp_path):
    tools = _writers(tmp_path)

    write = tools["write_file"].approval_evidence({"path": "box:raven-work/t1/a.md", "content": "one\n"})
    edit = tools["edit_file"].approval_evidence(
        {"path": "box:raven-work/t1/a.md", "old_text": "one", "new_text": "two"}
    )
    local = tools["write_file"].approval_evidence({"path": str(tmp_path / "here.md"), "content": "x"})

    assert write == {"path": "box:raven-work/t1/a.md", "machine": "box"}
    assert edit["machine"] == "box" and edit["path"] == "box:raven-work/t1/a.md" and "+two" in edit["diff"]
    assert "machine" not in local and local["created"] is True


@pytest.mark.slow
async def test_a_remote_write_and_edit_through_the_real_node_match_local_ones(real, tmp_path):
    from raven.agent.tools.filesystem import EditFileTool, WriteFileTool

    home = str(real.home)
    there = real.home / "raven-work" / "t1" / "new.md"
    tools = _writers(tmp_path)

    written = await tools["write_file"].execute("box:raven-work/t1/new.md", "one\ntwo\n")
    edited = await tools["edit_file"].execute("box:raven-work/t1/new.md", "two", "three")

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    local_write = WriteFileTool(workspace=local_dir, allowed_dirs=(local_dir,), follow_binding=False)
    local_edit = EditFileTool(workspace=local_dir, allowed_dirs=(local_dir,), follow_binding=False)
    want_written = await local_write.execute(str(local_dir / "new.md"), "one\ntwo\n")
    want_edited = await local_edit.execute(str(local_dir / "new.md"), "two", "three")
    swap = (str(local_dir / "new.md"), f"{SHOWN}{home}/raven-work/t1/new.md")
    assert written.model_text == want_written.model_text.replace(*swap)
    assert written.diff == want_written.diff.replace(*swap)
    assert edited.model_text == want_edited.model_text.replace(*swap)
    assert edited.file_change.after == want_edited.file_change.after == "one\nthree\n"
    assert there.read_text(encoding="utf-8") == "one\nthree\n"

    # Read, then changed behind this process's back: the overwrite is refused and says so.
    await _tools(tmp_path)["read_file"].execute("box:raven-work/t1/new.md")
    there.write_text("someone else\n", encoding="utf-8")
    refused = await tools["write_file"].execute("box:raven-work/t1/new.md", "four\n")
    assert refused.startswith(f"Error: {SHOWN}{home}/raven-work/t1/new.md on machine 'Lab box' (box) changed after")
    assert there.read_text(encoding="utf-8") == "someone else\n"
    # Read again, and the write goes through.
    await _tools(tmp_path)["read_file"].execute("box:raven-work/t1/new.md")
    assert (await tools["write_file"].execute("box:raven-work/t1/new.md", "four\n")).model_text.startswith(
        "Successfully"
    )
    assert there.read_text(encoding="utf-8") == "four\n"
