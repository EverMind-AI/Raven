"""Raven-Code file observations belong to the acting runtime and session."""

import asyncio
import sys
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.contracts.loop_hooks import AgentHookContext
from raven.contracts.tool import ToolResult
from raven.plugins.context import PluginContext, ServiceLocator
from raven.spine import ChatType, Origin, Source, TurnRequest

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "agents/raven-code/plugins/code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow.flow import make_flow_hook, make_session_observer  # noqa: E402
from code_flow.tools import plugin as factories  # noqa: E402


def _ctx(home, flow_enabled=True):
    return PluginContext(
        config={"enabled": flow_enabled, "tools": {"enabled": True}, "projectFiles": []},
        services=ServiceLocator(workspace=home, user_id="u", agent_id="a"),
    )


def _text(result):
    return result.model_text if isinstance(result, ToolResult) else str(result)


async def _bind(hook, key):
    await hook.before_iteration(AgentHookContext(session_key=key, messages=[]))


async def _edit(ctx, target, old="original", new="changed"):
    return _text(await factories.make_edit_file(ctx).execute(file_path=str(target), old_string=old, new_string=new))


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", ["unread", "stale", "read", "written", "new"])
async def test_append_only_preserves_knowledge_of_previously_seen_content(tmp_path, previous):
    ctx = _ctx(tmp_path)
    hook = make_flow_hook(ctx)
    await _bind(hook, "cli:a")
    target = tmp_path / "target.txt"
    if previous != "new":
        target.write_text("original\n")
    if previous in {"read", "stale"}:
        await factories.make_read_file(ctx).execute(file_path=str(target))
    if previous == "stale":
        target.write_text("original with external changes\n")
    if previous == "written":
        await factories.make_write_file(ctx).execute(file_path=str(target), content="original\n")
    appended = await factories.make_write_file(ctx).execute(file_path=str(target), content="tail\n", mode="append")
    assert "Successfully" in _text(appended)
    result = await _edit(ctx, target, old="tail" if previous == "new" else "original")
    if previous in {"unread", "stale"}:
        assert result.startswith("Error:"), result
        assert "original" in target.read_text()
    else:
        assert "Successfully edited" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("same_home", [False, True])
async def test_runtime_instances_do_not_share_observations_even_for_the_same_session(tmp_path, same_home):
    first = _ctx(tmp_path / "first")
    second = _ctx(tmp_path / ("first" if same_home else "second"))
    hook_a, hook_b = make_flow_hook(first), make_flow_hook(second)
    target = tmp_path / "target.txt"
    target.write_text("original\n")
    await _bind(hook_a, "cli:same")
    await factories.make_read_file(first).execute(file_path=str(target))
    await _bind(hook_b, "cli:same")
    result = await _edit(second, target)
    assert result.startswith("Error:"), result
    await _bind(hook_a, "cli:same")
    assert "Successfully edited" in await _edit(first, target)


@pytest.mark.asyncio
async def test_parallel_sessions_do_not_borrow_each_others_read_binding(tmp_path):
    ctx = _ctx(tmp_path)
    hook = make_flow_hook(ctx)
    target = tmp_path / "target.txt"
    target.write_text("original\n")
    read_done, other_done = asyncio.Event(), asyncio.Event()

    async def reader():
        await _bind(hook, "cli:a")
        await factories.make_read_file(ctx).execute(file_path=str(target))
        read_done.set()
        await other_done.wait()
        assert "Successfully edited" in await _edit(ctx, target)

    async def stranger():
        await read_done.wait()
        await _bind(hook, "cli:b")
        try:
            result = await _edit(ctx, target)
            assert result.startswith("Error:"), result
        finally:
            other_done.set()

    await asyncio.gather(reader(), stranger())


@pytest.mark.asyncio
async def test_deletion_clears_only_the_deleted_conversations_observations(tmp_path):
    ctx = _ctx(tmp_path)
    hook = make_flow_hook(ctx)
    targets = {key: tmp_path / f"{key}.txt" for key in ("a", "b")}
    for key, target in targets.items():
        target.write_text("original\n")
        await _bind(hook, f"cli:{key}")
        await factories.make_read_file(ctx).execute(file_path=str(target))
    # Observers and hooks are constructed with separate locators by the host.
    make_session_observer(_ctx(tmp_path)).on_session_deleted("cli:a", True)
    await _bind(hook, "cli:a")
    assert (await _edit(ctx, targets["a"])).startswith("Error:")
    await _bind(hook, "cli:b")
    assert "Successfully edited" in await _edit(ctx, targets["b"])


class _Provider:
    def __init__(self):
        self.responses = []
        self.results = []

    async def chat_with_retry(self, **kwargs):
        self.results.extend(m for m in kwargs["messages"] if m.get("role") == "tool")
        return self.responses.pop(0)

    def get_default_model(self):
        return "fake/model"


def _loop(home, provider, flow_enabled=True):
    # Separate locators match real plugin-stack construction.
    hook = make_flow_hook(_ctx(home, flow_enabled))
    tools = [
        make(_ctx(home, flow_enabled))
        for make in (
            factories.make_read_file,
            factories.make_write_file,
            factories.make_edit_file,
        )
    ]
    loop = AgentLoop(
        provider=provider,
        workspace=home,
        model="fake/model",
        policy=TurnPolicy(max_iterations=4),
        host=HostWiring(hooks=[hook]),
        tools=ToolWiring(plugin_tools=tools),
    )

    async def noop(**kwargs):
        return None

    loop._start_executor = noop
    loop._connect_mcp = noop
    return loop


def _req(text):
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id="a", sender_id="u", chat_type=ChatType.DM),
        text=text,
    )


async def _turn(loop, provider, key, name, args, origin=Origin.USER):
    provider.responses = [
        LLMResponse(content="", tool_calls=[ToolCallRequest(id="call", name=name, arguments=args)]),
        LLMResponse(content="done", finish_reason="stop"),
    ]
    provider.results = []
    await loop._process_message(_req("continue"), session_key=key, origin=origin)
    return provider.results[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_enabled", [False, True])
@pytest.mark.parametrize("origin", [Origin.USER, Origin.SUBAGENT])
async def test_real_loop_binds_the_actual_session_for_file_tools(tmp_path, flow_enabled, origin):
    provider = _Provider()
    loop = _loop(tmp_path, provider, flow_enabled)
    target = tmp_path / "target.txt"
    target.write_text("original\n")
    assert "original" in await _turn(loop, provider, "cli:a", "read_file", {"file_path": str(target)}, origin)
    args = {"file_path": str(target), "old_string": "original", "new_string": "changed"}
    result = await _turn(loop, provider, "cli:b", "edit_file", args, origin)
    assert "Error:" in result, result
    assert "Successfully edited" in await _turn(loop, provider, "cli:a", "edit_file", args, origin)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/new", "  /NEW  ", "/help"])
@pytest.mark.parametrize("flow_enabled", [False, True])
async def test_host_commands_keep_file_observations_scoped_to_the_unchanged_session_key(
    tmp_path, command, flow_enabled, monkeypatch
):
    provider = _Provider()
    loop = _loop(tmp_path, provider, flow_enabled)
    target = tmp_path / "target.txt"
    target.write_text("original\n")
    await _turn(loop, provider, "cli:a", "read_file", {"file_path": str(target)})
    attempts = []

    async def consolidate(session):
        attempts.append(session.key)
        return True

    monkeypatch.setattr(loop.memory_consolidator, "consolidate_unconsolidated", consolidate)
    reply = await loop._process_message(_req(command), session_key="cli:a")
    if command.strip().lower() == "/new":
        assert attempts == ["cli:a"]
        assert reply[0] == "New session started."
    else:
        assert not attempts
    result = await _turn(
        loop,
        provider,
        "cli:a",
        "edit_file",
        {"file_path": str(target), "old_string": "original", "new_string": "changed"},
    )
    assert "Successfully edited" in result, result


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", ["unread", "read"])
async def test_failed_append_does_not_create_or_discard_a_read_record(tmp_path, previous):
    ctx = _ctx(tmp_path)
    hook = make_flow_hook(ctx)
    await _bind(hook, "cli:a")
    target = tmp_path / "target.txt"
    target.write_text("original\n")
    if previous == "read":
        await factories.make_read_file(ctx).execute(file_path=str(target))
    result = await factories.make_write_file(ctx).execute(file_path=str(target), content="", mode="append")
    assert _text(result).startswith("Error:")
    edited = await _edit(ctx, target)
    assert ("Successfully edited" in edited) == (previous == "read")


@pytest.mark.asyncio
async def test_an_ambiguous_edit_does_not_count_as_observing_the_file(tmp_path):
    from code_flow.tools.filesystem import EditFileTool

    ctx = _ctx(tmp_path)
    hook = make_flow_hook(ctx)
    await _bind(hook, "cli:a")
    target = tmp_path / "target.txt"
    target.write_text("original original\n")
    unchecked = EditFileTool(workspace=tmp_path, require_read=False)
    result = await unchecked.execute(file_path=str(target), old_string="original", new_string="changed")
    assert _text(result).startswith("Warning:")
    assert (await _edit(ctx, target, old="original original")).startswith("Error:")
    assert target.read_text() == "original original\n"
    assert "Unread or externally changed files are rejected" not in unchecked.description


@pytest.mark.asyncio
async def test_append_in_one_session_leaves_another_sessions_record_stale(tmp_path):
    ctx = _ctx(tmp_path)
    hook = make_flow_hook(ctx)
    target = tmp_path / "target.txt"
    target.write_text("original\n")
    for key in ("cli:a", "cli:b"):
        await _bind(hook, key)
        await factories.make_read_file(ctx).execute(file_path=str(target))
    await factories.make_write_file(ctx).execute(file_path=str(target), content="tail\n", mode="append")
    await _bind(hook, "cli:a")
    assert "changed since" in await _edit(ctx, target)
    await _bind(hook, "cli:b")
    assert "Successfully edited" in await _edit(ctx, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_enabled", [False, True])
async def test_unbound_and_wrong_owner_calls_cannot_borrow_observations(tmp_path, flow_enabled):
    ctx = _ctx(tmp_path, flow_enabled)
    hook = make_flow_hook(ctx)
    target = tmp_path / "target.txt"
    target.write_text("original\n")
    await _bind(hook, "cli:a")
    await factories.make_read_file(ctx).execute(file_path=str(target))
    assert "no active Raven-Code session" in await _edit(_ctx(tmp_path / "other"), target)
    await hook.after_send(AgentHookContext(session_key="cli:a"))
    assert "no active Raven-Code session" in await _edit(ctx, target)
    await _bind(hook, "cli:a")
    assert "Successfully edited" in await _edit(ctx, target)


@pytest.mark.asyncio
async def test_replacing_a_file_with_the_same_mtime_still_requires_another_read(tmp_path):
    import os

    ctx = _ctx(tmp_path)
    hook = make_flow_hook(ctx)
    target = tmp_path / "target.txt"
    target.write_text("original\n")
    await _bind(hook, "cli:a")
    await factories.make_read_file(ctx).execute(file_path=str(target))
    stat = target.stat()
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("external\n")
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(target)
    assert "changed since" in await _edit(ctx, target, old="external")
