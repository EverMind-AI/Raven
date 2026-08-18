"""Tests for the deliver_files tool: the web-channel gate, path validation, and
the manifest it hands back to the turn stream."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from raven.agent.tools._deliverables import DeliverableStore
from raven.agent.tools.deliver import DeliverFilesTool
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


async def test_take_metadata_is_consumed_once(tool, tmp_path) -> None:
    _write(tmp_path / "chanwork", "a.txt")

    await tool.execute(files=[{"path": "a.txt"}])

    assert tool.take_metadata() is not None
    assert tool.take_metadata() is None


async def test_non_web_channel_refuses_without_touching_the_filesystem(tool, tmp_path) -> None:
    """The refusal must not depend on the path being bad: an existing, valid
    file is still refused, and nothing is registered."""
    _write(tmp_path / "chanwork", "real.txt")
    tool.set_context("whatsapp", "123", "whatsapp:123")

    result = await tool.execute(files=[{"path": "real.txt"}])

    assert result.startswith("Error")
    assert "web" in result
    assert "whatsapp" in result
    assert tool.take_metadata() is None


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
    """The tool needs the turn's channel (for the web gate) and the session key
    (for token reuse); only _set_tool_context supplies them, and it only reaches
    tools named in its whitelist. The registry gets the channel too -- that is
    what withholds the tool from a channel it cannot serve."""
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


def test_declares_itself_web_only(tool) -> None:
    """The declaration is what the registry filters on; losing it puts the tool
    back in every IM channel's schema, refusing only once called."""
    assert tool.channels == frozenset({"web"})


def test_an_im_turn_never_sees_deliver_files_in_the_schema(tool) -> None:
    """The join the test above cannot make: a real registry holding the real
    tool, driven through the real _set_tool_context, read back the way a request
    is actually assembled. Declaration, wiring and schema are each covered
    alone; a break in how they meet would pass all three."""
    from raven.agent.loop.main import AgentLoop

    registry = ToolRegistry()
    registry.register(tool)

    class _Loop:
        tools = registry
        _playbooks = None

    def offered() -> set[str]:
        return {d["function"]["name"] for d in registry.get_definitions()}

    AgentLoop._set_tool_context(_Loop(), "telegram", "c1", None, session_key="telegram:c1")
    assert "deliver_files" not in offered()

    AgentLoop._set_tool_context(_Loop(), "web", "c1", None, session_key="web:c1")
    assert "deliver_files" in offered()
