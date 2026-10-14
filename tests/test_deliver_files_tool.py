"""Tests for deliver_files path validation and turn-stream manifests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path, PurePosixPath

import pytest

from raven.agent.tools.deliver import DeliverFilesTool
from raven.agent.tools.deliverables import DeliverableStore
from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.tool_search import TOOL_CALL_NAME, ToolCallTool, ToolSearchController
from raven.agent.workdir import bind


@pytest.fixture
def tool(tmp_path):
    workspace = tmp_path / "chanwork"
    workspace.mkdir()
    store = DeliverableStore(tmp_path / "deliverables.json")
    t = DeliverFilesTool(store, workspace=workspace, allowed_dirs=())
    t.set_context("web", "default", "web:s1")
    return t


def _write(tool_workspace: Path, name: str, body: bytes = b"hello") -> Path:
    fp = tool_workspace / name
    fp.write_bytes(body)
    return fp


def test_name_is_snake_case(tool) -> None:
    assert tool.name == "deliver_files"


def test_description_forbids_substituting_a_path_or_a_link(tool) -> None:
    """The observed failure is a reply carrying a self-composed download URL
    instead of a call, which resolves to nothing. Nothing else in the prompt
    says a path is not a delivery, so this description has to."""
    desc = tool.description
    assert "only way" in desc
    assert "Never write a path or a link instead" in desc


async def test_happy_path_manifest_shape(tool, tmp_path) -> None:
    _write(tmp_path / "chanwork", "report.pdf", b"12345")

    summary = await tool.execute(
        files=[{"path": "report.pdf", "title": "Q3 report", "description": "final"}],
        message="here you go",
    )

    assert "report.pdf" in summary
    manifest = tool.take_metadata()["raven_delivery"]
    assert manifest["message"] == "here you go"
    assert manifest["invalid"] == []
    assert len(manifest["files"]) == 1
    entry = manifest["files"][0]
    assert entry["name"] == "report.pdf"
    assert entry["title"] == "Q3 report"
    assert entry["description"] == "final"
    assert entry["size"] == 5
    assert entry["media_type"] == "application/pdf"
    assert entry["token"]
    assert entry["download_path"] == f"/files/download?token={entry['token']}"
    assert "bytes" not in entry and "content" not in entry


async def test_the_manifest_says_when_the_delivery_happened(tool, tmp_path) -> None:
    """One clock for both sides of the wire.

    The page ranks deliveries against each other and had nothing to rank them by:
    the message's own stamp is naive local wall time and only exists once the turn
    is stored, so a delivery still streaming had no time at all. This field rides
    the same manifest object down the live event and the replayed message, so both
    read the same number.
    """
    before = int(time.time() * 1000)
    _write(tmp_path / "chanwork", "report.pdf", b"12345")

    await tool.execute(files=[{"path": "report.pdf"}], message="here")

    manifest = tool.take_metadata()["raven_delivery"]
    assert isinstance(manifest["delivered_at"], int)
    assert before <= manifest["delivered_at"] <= int(time.time() * 1000) + 100


def test_no_two_deliveries_share_a_stamp() -> None:
    """A clock alone is not enough. `time.time()` repeats inside a millisecond --
    fifty sequential calls through this tool were measured producing thirty-five
    distinct values -- and two deliveries sharing a stamp leave the reader ranking
    them by the order the page happened to paint them, which is the thing the
    stamp exists to stop."""
    from raven.agent.tools.deliver import _stamp

    seen = [_stamp() for _ in range(50)]

    assert len(set(seen)) == 50
    assert all(b > a for a, b in zip(seen, seen[1:]))


async def test_take_metadata_is_consumed_once(tool, tmp_path) -> None:
    _write(tmp_path / "chanwork", "a.txt")

    await tool.execute(files=[{"path": "a.txt"}])

    assert tool.take_metadata() is not None
    assert tool.take_metadata() is None


async def test_non_web_channel_delivers_the_same_manifest(tool, tmp_path) -> None:
    _write(tmp_path / "chanwork", "real.txt")
    tool.set_context("whatsapp", "123", "whatsapp:123")

    result = await tool.execute(files=[{"path": "real.txt"}])

    assert result.startswith("Delivered")
    assert [item["name"] for item in tool.take_metadata()["raven_delivery"]["files"]] == ["real.txt"]


async def test_missing_file_yields_an_error_and_no_manifest(tool) -> None:
    """Nothing was delivered, so there is no manifest to render; the renderer
    falls back to the text summary, which names the failure."""
    result = await tool.execute(files=[{"path": "nope.txt"}])

    assert result.startswith("Error")
    assert "nope.txt" in result
    assert tool.take_metadata() is None


async def test_mixed_valid_and_invalid(tool, tmp_path) -> None:
    _write(tmp_path / "chanwork", "good.txt")

    summary = await tool.execute(files=[{"path": "good.txt"}, {"path": "bad.txt"}])

    assert "good.txt" in summary
    assert "bad.txt" in summary
    manifest = tool.take_metadata()["raven_delivery"]
    assert [f["name"] for f in manifest["files"]] == ["good.txt"]
    assert manifest["invalid"] == [{"path": "bad.txt", "reason": "not found or not a regular file"}]


async def test_directory_is_invalid(tool, tmp_path) -> None:
    (tmp_path / "chanwork" / "sub").mkdir()

    result = await tool.execute(files=[{"path": "sub"}])

    assert result.startswith("Error")


async def test_duplicate_paths_are_deduplicated(tool, tmp_path) -> None:
    _write(tmp_path / "chanwork", "a.txt")

    await tool.execute(files=[{"path": "a.txt"}, {"path": "./a.txt"}])

    manifest = tool.take_metadata()["raven_delivery"]
    assert len(manifest["files"]) == 1


async def test_redelivery_in_same_conversation_reuses_token(tool, tmp_path) -> None:
    _write(tmp_path / "chanwork", "a.txt")

    await tool.execute(files=[{"path": "a.txt"}])
    first = tool.take_metadata()["raven_delivery"]["files"][0]["token"]
    await tool.execute(files=[{"path": "a.txt"}])
    second = tool.take_metadata()["raven_delivery"]["files"][0]["token"]

    assert first == second


async def test_path_outside_allowed_dir_is_invalid_when_restricted(tmp_path) -> None:
    workspace = tmp_path / "chanwork"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"nope")
    store = DeliverableStore(tmp_path / "deliverables.json")
    restricted = DeliverFilesTool(store, workspace=workspace, allowed_dirs=(workspace,))
    restricted.set_context("web", "default", "web:s1")

    result = await restricted.execute(files=[{"path": str(outside)}])

    assert result.startswith("Error")
    assert restricted.take_metadata() is None


async def test_bound_session_dir_is_allowed_even_when_only_home_is_static(tmp_path) -> None:
    """Mirrors AgentLoop._register_default_tools: constructed once with only agent
    home in allowed_dirs, a later-bound session dir outside home must still work."""
    home = tmp_path / "home"
    session = tmp_path / "session"
    home.mkdir()
    session.mkdir()
    _write(session, "report.pdf", b"12345")
    store = DeliverableStore(tmp_path / "deliverables.json")
    restricted = DeliverFilesTool(store, workspace=home, allowed_dirs=(home,))
    restricted.set_context("web", "default", "web:s1")

    with bind(session):
        result = await restricted.execute(files=[{"path": "report.pdf"}])

    assert result.startswith("Delivered"), result


async def test_two_different_files_get_different_tokens(tool, tmp_path) -> None:
    _write(tmp_path / "chanwork", "a.txt")
    _write(tmp_path / "chanwork", "b.txt")

    await tool.execute(files=[{"path": "a.txt"}, {"path": "b.txt"}])

    tokens = [f["token"] for f in tool.take_metadata()["raven_delivery"]["files"]]
    assert tokens[0] != tokens[1]
    assert "a.txt" not in tokens[0]


async def test_a_failed_call_does_not_inherit_the_previous_manifest(tool, tmp_path) -> None:
    """A manifest nobody collected must not outlive its call: an all-invalid
    delivery that pops the earlier one would hang another call's files off a
    failure, and the UI would show files that this call never delivered."""
    _write(tmp_path / "chanwork", "a.txt")
    await tool.execute(files=[{"path": "a.txt"}])

    result = await tool.execute(files=[{"path": "gone.txt"}])

    assert result.startswith("Error")
    assert tool.take_metadata() is None


async def test_concurrent_turns_keep_their_own_manifest(tmp_path) -> None:
    """Two sessions delivering at once must not cross manifests. The failure is
    silent when it happens — one turn's file list rendered in another turn's
    card — so it needs a guard rather than a manual check."""
    workspace = tmp_path / "chanwork"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"A")
    (workspace / "b.txt").write_bytes(b"B")
    store = DeliverableStore(tmp_path / "deliverables.json")
    shared = DeliverFilesTool(store, workspace=workspace, allowed_dirs=())

    async def turn(session: str, filename: str) -> dict:
        shared.set_context("web", session, f"web:{session}")
        await asyncio.wait_for(shared.execute(files=[{"path": filename}]), timeout=5)
        return shared.take_metadata()

    first, second = await asyncio.gather(turn("s1", "a.txt"), turn("s2", "b.txt"))

    assert [f["name"] for f in first["raven_delivery"]["files"]] == ["a.txt"]
    assert [f["name"] for f in second["raven_delivery"]["files"]] == ["b.txt"]


async def test_manifest_survives_the_tool_call_forwarder(tool, tmp_path) -> None:
    """Above ``compaction_threshold`` the model reaches deliver_files through
    ``tool_call``, so the loop asks the registry about a tool that owns no
    manifest. Resolving the owner first is what keeps the delivery visible."""
    _write(tmp_path / "chanwork", "report.pdf", b"12345")
    registry = ToolRegistry()
    registry.register(tool)
    controller = ToolSearchController(registry, always_visible=set())
    registry.register(ToolCallTool(controller))

    arguments = {"name": "deliver_files", "arguments": {"files": [{"path": "report.pdf"}]}}
    summary = await registry.execute(TOOL_CALL_NAME, arguments)

    assert "report.pdf" in summary
    payload = registry.take_metadata(TOOL_CALL_NAME, arguments)
    assert [f["name"] for f in payload["raven_delivery"]["files"]] == ["report.pdf"]


# ---------------------------------------------------------------------------
# _set_tool_context whitelist — deliver_files must receive channel + session key
# ---------------------------------------------------------------------------


def test_set_tool_context_hands_deliver_files_the_channel_and_session_key() -> None:
    """The tool needs the turn's channel and session key for delivery routing
    and token reuse; only _set_tool_context supplies them."""
    from raven.agent.loop.main import AgentLoop

    seen: dict[str, tuple[str, str, str]] = {}

    class _FakeDeliver:
        def set_context(self, channel: str, chat_id: str, session_key: str) -> None:
            seen["args"] = (channel, chat_id, session_key)

    class _Tools:
        def get(self, name: str):
            return _FakeDeliver() if name == "deliver_files" else None

        def set_channel(self, channel: str | None) -> None:
            seen["channel"] = channel

    class _Stub:
        tools = _Tools()
        _playbooks = None

    AgentLoop._set_tool_context(_Stub(), "web", "default", None, session_key="web:s1")

    assert seen["args"] == ("web", "default", "web:s1")
    assert seen["channel"] == "web"


def test_declares_itself_available_to_every_channel(tool) -> None:
    assert tool.channels is None


def test_every_surface_sees_deliver_files_in_the_schema(tool) -> None:
    from raven.agent.loop.main import AgentLoop

    registry = ToolRegistry()
    registry.register(tool)

    class _Loop:
        tools = registry
        _playbooks = None

    def offered() -> set[str]:
        return {d["function"]["name"] for d in registry.get_definitions()}

    AgentLoop._set_tool_context(_Loop(), "telegram", "c1", None, session_key="telegram:c1")
    assert "deliver_files" in offered()

    AgentLoop._set_tool_context(_Loop(), "web", "c1", None, session_key="web:c1")
    assert "deliver_files" in offered()


def test_the_call_puts_its_title_and_description_in_the_registry(tmp_path) -> None:
    """Not only on the turn event. The manifest reaches whichever client is
    connected at that moment; the registry is what every later reader asks --
    a reconnect, a second client, a shelf rebuilt after a compaction archived
    the turn. Storing only the path and the size there would hand those readers
    a file name where the agent had written a title."""
    workspace = tmp_path / "chanwork"
    workspace.mkdir()
    store = DeliverableStore(tmp_path / "deliverables.json")
    tool = DeliverFilesTool(store, workspace=workspace, allowed_dirs=())
    tool.set_context("web", "default", "web:s1")
    _write(workspace, "brief.md")

    with bind(workspace):
        asyncio.run(tool.execute(files=[{"path": "brief.md", "title": "The brief", "description": "what it is"}]))

    rows = store.for_conversation("web:s1")
    assert [(r.name, r.title, r.description) for r in rows] == [("brief.md", "The brief", "what it is")]


def test_a_re_delivery_refreshes_what_the_registry_says_the_file_is(tmp_path) -> None:
    """The token is reused for a path this conversation already delivered, so
    the row is rewritten rather than added -- and it has to carry the new
    title, or the shelf keeps naming the file by an earlier draft of itself."""
    workspace = tmp_path / "chanwork"
    workspace.mkdir()
    store = DeliverableStore(tmp_path / "deliverables.json")
    tool = DeliverFilesTool(store, workspace=workspace, allowed_dirs=())
    tool.set_context("web", "default", "web:s1")
    _write(workspace, "brief.md")

    with bind(workspace):
        asyncio.run(tool.execute(files=[{"path": "brief.md", "title": "first cut"}]))
        asyncio.run(tool.execute(files=[{"path": "brief.md", "title": "second cut"}]))

    rows = store.for_conversation("web:s1")
    assert [r.title for r in rows] == ["second cut"]


# --- a file on a registered machine is brought back first ----------------------------------


@pytest.fixture
def machines(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from raven.ops import remote_path
    from tests.test_node_client import row

    registry = tmp_path / "connections.json"
    registry.write_text(json.dumps({"connections": [row()]}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(registry))
    monkeypatch.setenv("RAVEN_DEBUG_MACHINES", "1")
    monkeypatch.setattr(remote_path, "_HOMES", {})
    monkeypatch.setattr(remote_path, "_READ", {})
    monkeypatch.setattr(
        "raven.config.loader.load_config", lambda *a, **k: SimpleNamespace(subagents=SimpleNamespace(agents=[]))
    )
    pool = _RemotePool()
    monkeypatch.setattr(remote_path, "_NODES", lambda: pool)
    return pool


def _landed(tmp_path: Path, place: str, *, name: str | None = None, machine: str = "box") -> Path:
    """Where a file of the machine lands, as deliver.py lays it out: under a key of its path, by its name."""
    key = hashlib.sha256(f"{machine}\0{place}".encode()).hexdigest()[:16]
    return tmp_path / "chanwork" / "pulled" / machine / key / (name or PurePosixPath(place).name)


class _RemotePool:
    """A node pool with one file of a given size, which it hands over as x's."""

    def __init__(self) -> None:
        self.size = 5
        self.kind = "file"
        self.pulled: list[tuple[str, Path, list[str]]] = []

    async def home(self, machine: str) -> str:
        return "/root"

    async def stat(self, machine: str, path: str, *, roots: list[str], hash: bool = False) -> dict:
        return {"exists": self.kind != "missing", "kind": self.kind, "size": self.size, "mtime": 1.0}

    async def pull_file(self, machine: str, remote_file: str, local: Path, *, roots: list[str]) -> int:
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"x" * self.size)
        self.pulled.append((remote_file, local, roots))
        return self.size


async def test_a_file_on_a_machine_is_brought_back_and_delivered(tool, tmp_path, machines) -> None:
    summary = await tool.execute(files=[{"path": "box:raven-work/t1/report.pdf", "title": "Report"}])

    manifest = tool.take_metadata()["raven_delivery"]
    assert manifest["invalid"] == [], summary
    (entry,) = manifest["files"]
    landed = _landed(tmp_path, "/root/raven-work/t1/report.pdf")
    assert entry["name"] == "report.pdf" and entry["path"] == str(landed) and entry["size"] == 5
    assert landed.read_bytes() == b"xxxxx"
    assert machines.pulled == [("/root/raven-work/t1/report.pdf", landed, ["/root/raven-work"])]


async def test_a_name_holding_the_other_platform_s_separators_lands_as_one_name_under_the_delivery_directory(
    tool, tmp_path, machines
) -> None:
    # A Linux file name may hold backslashes, which a Windows host reads as steps up.
    summary = await tool.execute(files=[{"path": "box:raven-work/t1/..\\..\\..\\review_escape.txt"}])

    manifest = tool.take_metadata()["raven_delivery"]
    assert manifest["invalid"] == [], summary
    (landed,) = (Path(entry["path"]) for entry in manifest["files"])
    assert landed == _landed(
        tmp_path, "/root/raven-work/t1/..\\..\\..\\review_escape.txt", name="..%5C..%5C..%5Creview_escape.txt"
    )
    assert landed.is_file() and not list(tmp_path.glob("**/review_escape.txt"))
    assert machines.pulled[0][0] == "/root/raven-work/t1/..\\..\\..\\review_escape.txt"


async def test_two_names_that_differ_only_by_one_written_out_land_apart(tool, tmp_path, machines) -> None:
    # `a\b.txt` is written `a%5Cb.txt` here, so a file named `a%5Cb.txt` must not land on it.
    summary = await tool.execute(
        files=[{"path": "box:raven-work/t1/a\\b.txt"}, {"path": "box:raven-work/t1/a%5Cb.txt"}]
    )

    manifest = tool.take_metadata()["raven_delivery"]
    assert manifest["invalid"] == [], summary
    landed = [Path(entry["path"]) for entry in manifest["files"]]
    assert landed == [
        _landed(tmp_path, "/root/raven-work/t1/a\\b.txt", name="a%5Cb.txt"),
        _landed(tmp_path, "/root/raven-work/t1/a%5Cb.txt", name="a%255Cb.txt"),
    ]
    assert all(place.is_file() for place in landed)
    assert [where for where, _, _ in machines.pulled] == [
        "/root/raven-work/t1/a\\b.txt",
        "/root/raven-work/t1/a%5Cb.txt",
    ]


async def test_two_paths_this_filesystem_would_read_as_one_name_land_apart(tool, tmp_path, machines) -> None:
    # Windows and macOS read `Report.txt` and `report.txt` as one name, and Windows drops a trailing dot: the
    # key of each path keeps them apart, whatever the filesystem makes of the name.
    names = ("Report.txt", "report.txt", "report.txt.")

    summary = await tool.execute(files=[{"path": f"box:raven-work/t1/{name}"} for name in names])

    manifest = tool.take_metadata()["raven_delivery"]
    assert manifest["invalid"] == [], summary
    landed = [Path(entry["path"]) for entry in manifest["files"]]
    assert landed == [_landed(tmp_path, f"/root/raven-work/t1/{name}") for name in names]
    assert len({place.parent for place in landed}) == 3 and all(place.is_file() for place in landed)
    assert [where for where, _, _ in machines.pulled] == [f"/root/raven-work/t1/{name}" for name in names]


async def test_nothing_is_fetched_to_a_landing_outside_the_delivery_directory(
    tool, tmp_path, machines, monkeypatch
) -> None:
    from raven.agent.tools import deliver

    monkeypatch.setattr(deliver, "_local_name", lambda part: "..")

    summary = await tool.execute(files=[{"path": "box:raven-work/t1/report.pdf"}])

    assert "has a name this computer cannot keep under its delivery directory; read it there instead" in summary
    assert machines.pulled == [] and not (tmp_path / "chanwork" / "pulled").exists()


async def test_two_files_of_one_name_from_two_directories_land_apart(tool, tmp_path, machines) -> None:
    summary = await tool.execute(
        files=[{"path": "box:raven-work/a/report.pdf"}, {"path": "box:raven-work/b/report.pdf"}]
    )

    manifest = tool.take_metadata()["raven_delivery"]
    assert manifest["invalid"] == [], summary
    landed = [Path(entry["path"]) for entry in manifest["files"]]
    assert landed == [
        _landed(tmp_path, "/root/raven-work/a/report.pdf"),
        _landed(tmp_path, "/root/raven-work/b/report.pdf"),
    ]
    assert [where for where, _, _ in machines.pulled] == [
        "/root/raven-work/a/report.pdf",
        "/root/raven-work/b/report.pdf",
    ]


async def test_a_file_over_the_budget_stays_on_its_machine(tool, machines) -> None:
    machines.size = 21 * 1024 * 1024

    await tool.execute(files=[{"path": "box:raven-work/t1/model.bin"}])

    assert tool.take_metadata() is None, "nothing was delivered, so no manifest"
    assert machines.pulled == []


async def test_what_was_not_delivered_says_why(tool, machines) -> None:
    from raven.ops import remote_path

    machines.size = 21 * 1024 * 1024
    big = await tool.execute(files=[{"path": "box:raven-work/t1/model.bin"}])
    machines.size = 5
    outside = await tool.execute(files=[{"path": "box:.ssh/id_rsa"}])
    remote_path.remember_read("box", "/root/.ssh/id_rsa", "ab" * 32)
    read_first = await tool.execute(files=[{"path": "box:.ssh/id_rsa"}])
    after_a_read = read_first
    nobody = await tool.execute(files=[{"path": "nobox:/etc/hostname"}])
    machines.kind = "dir"
    folder = await tool.execute(files=[{"path": "box:raven-work/t1"}])

    assert (
        "21.0 MB is over the 20.0 MB limit for a file brought back from machine 'Lab box' (box); read it there instead"
        in big
    )
    assert (
        "is outside the directories this Raven's agents work in on machine 'Lab box' (box); read it there instead"
        in outside
    )
    assert "outside" in after_a_read, "a read by some conversation is no evidence for this one"
    assert machines.pulled == [], "nothing outside the roots is fetched"
    assert "is written as a path on another machine" in nobody
    assert "not found or not a regular file on machine 'Lab box' (box)" in folder
    # The machine is named by its label in every sentence; its address appears
    # only inside a path shown the way scp writes it.
    assert (
        outside.count("203.0.113.7") == 1 and "scp://worker@203.0.113.7:58717//root/.ssh/id_rsa is outside" in outside
    )
    assert "203.0.113.7" not in big + nobody + folder


async def test_a_file_one_byte_over_the_budget_is_refused_in_bytes_not_in_a_size_that_reads_as_the_limit(
    tool, machines
) -> None:
    """20 MiB goes; 20 MiB + 1 is refused, and the sentence does not read "20.0 MB is over the 20.0 MB limit"."""
    from raven.agent.tools.deliver import PULL_FILE_BUDGET

    machines.size = PULL_FILE_BUDGET
    exact = await tool.execute(files=[{"path": "box:raven-work/t1/exact.bin"}])
    tool.take_metadata()
    machines.size = PULL_FILE_BUDGET + 1
    over = await tool.execute(files=[{"path": "box:raven-work/t1/over.bin"}])

    assert exact.startswith("Delivered 1 file(s): exact.bin (20.0 MB)")
    assert (
        "20,971,521 bytes is over the 20,971,520-byte (20.0 MB) limit for a file brought back from machine "
        "'Lab box' (box); read it there instead" in over
    )
    assert "20.0 MB is over the 20.0 MB" not in over
    assert [where for where, _, _ in machines.pulled] == ["/root/raven-work/t1/exact.bin"]
