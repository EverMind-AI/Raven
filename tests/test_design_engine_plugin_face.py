"""The design-engine plugin face: admission, the sentinel, and the hook seats.

What is pinned here is the wave's authored layer -- the slice parse, the
factory-decline contract, the fail-closed sentinel, and the four D1 rebuild
clauses on the selector hook -- plus the design-parameterized history pin the
amended verdict swapped in for the unrunnable A/B no-pollution axis: the card
block reaches the model's view of the inbound and never the persisted user
row, which is the same restored slice the memory store consumes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent import workdir
from raven.agent.hook.participant import ParticipantHook
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.agent.tools.filesystem import ReadFileTool, WriteFileTool
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.contracts.loop_hooks import AgentHookContext
from raven.spine import ChatType, Origin, Source, TurnRequest
from raven_design.plugin import (
    make_hook,
    make_preview_file,
    make_render_file,
    make_update_task_state,
)
from raven_design.plugin.config import EngineConfig
from raven_design.plugin.hook import (
    FOUNDATION_SKILL_ID,
    DesignParticipant,
    MisconfiguredEngineHook,
    completion_notice,
    render_selection_block,
)
from raven_design.selector import SkillCard, VisualDomainSelection
from raven_design.task_state.manager import TaskStateManager

REPO = Path(__file__).resolve().parent.parent
SHIPPED_SLICE = json.loads((REPO / "agents" / "raven-design" / "config.json").read_text())["plugins"]["config"][
    "design-engine"
]


def _seated(participant: DesignParticipant) -> ParticipantHook:
    """The participant in the hook chain, one instance for the test the way one turn has one."""
    return ParticipantHook("design_engine", lambda: participant)


def _ctx(config: dict) -> SimpleNamespace:
    return SimpleNamespace(config=config)


def _fresh(config: dict) -> SimpleNamespace:
    # _Shared memoizes per config object identity; a copy keeps tests hermetic.
    return _ctx(json.loads(json.dumps(config)))


class _StubSelector:
    def __init__(self, selection: VisualDomainSelection | None = None, error: Exception | None = None) -> None:
        self._selection = selection
        self._error = error
        self.queries: list[str] = []

    async def select(self, query: str) -> VisualDomainSelection:
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return self._selection


def _selection() -> VisualDomainSelection:
    return VisualDomainSelection(
        preferred=(SkillCard("local/design-brand-identities", "design-brand-identities", "brand identities"),),
        alternatives=(SkillCard("local/create-marketing-graphics", "create-marketing-graphics", "marketing"),),
    )


# --- admission and the sentinel ------------------------------------------------


def test_an_absent_or_disabled_slice_casts_nothing():
    for config in ({}, {"enabled": False}):
        ctx = _fresh(config)
        assert make_hook(ctx) is None
        assert make_render_file(ctx) is None
        assert make_preview_file(ctx) is None
        assert make_update_task_state(ctx) is None


@pytest.mark.asyncio
async def test_a_malformed_slice_casts_the_fail_closed_sentinel():
    ctx = _fresh({"enabled": "yes"})
    hook = make_hook(ctx)
    assert isinstance(hook, MisconfiguredEngineHook)
    assert make_render_file(ctx) is None
    assert make_update_task_state(ctx) is None

    decision = await hook.before_user_inbound(SimpleNamespace(inbound_content="design a logo"))
    text = decision.short_circuit_result[0]
    assert 'plugins.config["design-engine"]' in text
    assert "enabled" in text


def test_strict_slice_typing_refuses_coercion_and_unknown_keys():
    for bad in (
        {"enabled": "false"},
        {"enabled": True, "workdirPerSession": "false"},
        {"enabled": True, "visualDomainSelector": {"preferredMax": "2"}},
        {"enabled": True, "visualDomainSelector": {"preferredMax": 99}},
        {"enabled": True, "render": {"timeoutSeconds": True}},
        {"enabled": True, "render": {"backend": "cloud"}},
        {"enabled": True, "render": {"defaultPreviewCount": 9, "maxPreviewCount": 3}},
        {"enabled": True, "taskState": {"stateRoot": 7}},
        {"enabled": True, "mystery": {}},
        {"enabled": True, "visualDomainSelector": {"mystery": 1}},
        # The fork's selector model/provider pinning leaves are retired (no
        # pin seat is granted to a plugin); a carried knob refuses loudly
        # rather than silently doing nothing (the D4 phantom floor).
        {"enabled": True, "visualDomainSelector": {"model": "vendor/pinned"}},
    ):
        with pytest.raises(ValueError):
            EngineConfig.from_slice(bad)


def test_the_shipped_product_slice_casts_the_hook_with_the_full_catalog():
    hook = make_hook(_fresh(SHIPPED_SLICE))
    assert isinstance(hook, ParticipantHook)
    participant = hook.factory()
    assert isinstance(participant, DesignParticipant)
    assert len(participant._selector.cards) == 15
    assert participant._manager is None, "the shipped slice carries no stateRoot until the launcher renders one"


def test_render_factories_decline_without_the_render_extra(monkeypatch):
    import raven_design.plugin as plugin

    monkeypatch.setattr(plugin, "_render_extra_missing", lambda: ["playwright"])
    ctx = _fresh(SHIPPED_SLICE)
    assert make_render_file(ctx) is None
    assert make_preview_file(ctx) is None


def test_render_factories_cast_seated_tools_with_the_extra_present(monkeypatch):
    import raven_design.plugin as plugin

    monkeypatch.setattr(plugin, "_render_extra_missing", lambda: [])
    ctx = _fresh(SHIPPED_SLICE)
    render_tool = make_render_file(ctx)
    preview_tool = make_preview_file(ctx)
    assert render_tool.name == "render_file"
    assert preview_tool.name == "preview_file"
    assert preview_tool.parameters["properties"]["max_previews"]["maximum"] == 12
    assert preview_tool.parameters["properties"]["scale"]["maximum"] == 4
    assert render_tool.parameters["properties"]["scale"]["minimum"] == 1
    assert render_tool.timeout_seconds > SHIPPED_SLICE["render"]["timeoutSeconds"]


@pytest.mark.asyncio
async def test_a_seated_render_tool_refuses_without_a_bound_workdir(monkeypatch):
    import raven_design.plugin as plugin

    monkeypatch.setattr(plugin, "_render_extra_missing", lambda: [])
    tool = make_render_file(_fresh(SHIPPED_SLICE))
    result = await tool.execute(path="page.html")
    assert "working directory" in str(result)


def test_the_render_fence_is_closed_by_default_and_opened_by_the_shipped_slice(monkeypatch, tmp_path):
    """H2: the fork seat passed the HOST's tools.restrictToWorkspace to the
    render path policy; a wheel cannot read that host knob, so the slice owns
    it -- default TRUE (fail-closed for any other installer), spelled false by
    the shipped product slice (fork parity: this product runs unrestricted)."""
    import raven_design.plugin as plugin
    import raven_design.rendering.service as service_mod

    assert EngineConfig.from_slice({"enabled": True}).render.restrict_to_workspace is True
    assert EngineConfig.from_slice(SHIPPED_SLICE).render.restrict_to_workspace is False

    captured = {}

    def fake_from_tool_config(settings, *, workspace, media_root, runtime_root, restrict_to_workspace):
        captured[str(workspace)] = restrict_to_workspace
        return SimpleNamespace(config=settings)

    monkeypatch.setattr(service_mod.RenderService, "from_tool_config", fake_from_tool_config)
    plugin._Shared.for_context(_fresh({"enabled": True})).service_for(tmp_path / "closed")
    plugin._Shared.for_context(_fresh(SHIPPED_SLICE)).service_for(tmp_path / "open")
    assert captured[str(tmp_path / "closed")] is True
    assert captured[str(tmp_path / "open")] is False


def test_a_custom_preview_cap_reaches_the_advertised_schema(monkeypatch):
    """M4: the schema the registry advertises must say what the seated
    service will enforce -- the slice's own caps, not the fork defaults."""
    import raven_design.plugin as plugin

    monkeypatch.setattr(plugin, "_render_extra_missing", lambda: [])
    capped = json.loads(json.dumps(SHIPPED_SLICE))
    capped["render"]["maxPreviewCount"] = 4
    capped["render"]["defaultPreviewCount"] = 2
    tool = make_preview_file(_ctx(capped))
    assert tool.parameters["properties"]["max_previews"]["maximum"] == 4
    assert tool.parameters["properties"]["max_previews"]["default"] == 2


def test_task_state_surfaces_need_a_state_root(tmp_path):
    with_root = json.loads(json.dumps(SHIPPED_SLICE))
    with_root["taskState"] = {"enabled": True, "stateRoot": str(tmp_path / "ts")}
    ctx = _fresh(with_root)
    tool = make_update_task_state(ctx)
    assert tool is not None and tool.name == "update_task_state"
    hook = make_hook(ctx)
    assert hook.factory()._manager is not None


@pytest.mark.asyncio
async def test_directory_isolation_runs_without_selector_or_task_state(tmp_path):
    config = {"enabled": True, "visualDomainSelector": {"enabled": False}, "taskState": {"enabled": False}}
    hook = make_hook(_fresh(config))
    with workdir.bind(tmp_path):
        await hook.before_user_inbound(AgentHookContext(session_key="acp:a", inbound_content="draw a poster"))
        own = workdir.current()
        assert own.parent == tmp_path / "designs" and own.is_dir()

    disabled = make_hook(_fresh({**config, "workdirPerSession": False}))
    assert disabled is None


@pytest.mark.asyncio
async def test_shared_directory_mode_retains_existing_files_and_task_state(tmp_path):
    manager = TaskStateManager(tmp_path / "state")
    manager.apply(
        str(tmp_path),
        [{"operation": "initialize", "state": {"goal": "Existing design", "items": [{"title": "Finish it"}]}}],
    )
    hook = _seated(
        DesignParticipant(EngineConfig.from_slice({"enabled": True, "workdirPerSession": False}), None, manager)
    )
    ctx = AgentHookContext(session_key="acp:a", inbound_content="continue", iteration=1)
    with workdir.bind(tmp_path):
        await hook.before_user_inbound(ctx)
        decision = await hook.before_iteration(ctx)
        assert workdir.current() == tmp_path
        assert "Existing design" in decision.append_note
    assert not (tmp_path / "designs").exists()


@pytest.mark.asyncio
async def test_concurrent_sessions_keep_files_state_and_completion_separate(tmp_path, monkeypatch):
    import raven_design.plugin as plugin
    from raven_design.rendering.service import RenderService

    async def render_paths(service, request):
        return {
            "source": str(service.path_policy.resolve_source(request.path)),
            "output": str(service.path_policy.resolve_output_dir(request.output_dir)),
        }

    monkeypatch.setattr(plugin, "_render_extra_missing", lambda: [])
    monkeypatch.setattr(RenderService, "render", render_paths)
    root = tmp_path / "work"
    root.mkdir()
    (root / "result.txt").write_text("Legacy shared artifact", encoding="utf-8")
    config = {
        "enabled": True,
        "visualDomainSelector": {"enabled": False},
        "render": {"backend": "direct"},
        "taskState": {"stateRoot": str(tmp_path / "state")},
    }
    plugin_ctx = _fresh(config)
    hook = make_hook(plugin_ctx)
    tool = make_update_task_state(plugin_ctx)
    render_tool = make_render_file(plugin_ctx)
    manager = hook.factory()._manager
    manager.apply(
        str(root),
        [{"operation": "initialize", "state": {"goal": "Legacy shared task", "items": [{"title": "Old work"}]}}],
    )
    barrier = asyncio.Barrier(2)
    folders: dict[str, Path] = {}

    async def turn(key: str, goal: str, complete: bool) -> None:
        ctx = AgentHookContext(session_key=key, inbound_content=goal, iteration=1, metadata={})
        with workdir.bind(root):
            await hook.before_user_inbound(ctx)
            first = await hook.before_iteration(ctx)
            assert "Not initialized" in first.append_note
            assert "Legacy shared task" not in first.append_note
            folders[key] = workdir.current()
            result = await tool.execute(
                [{"operation": "initialize", "state": {"goal": goal, "items": [{"title": goal}]}}]
            )
            assert result.startswith("Task State updated"), result
            await WriteFileTool().execute(path="result.txt", content=goal)
            await barrier.wait()

            ctx.iteration = 2
            assert f"Goal: {goal}" in (await hook.before_iteration(ctx)).append_note
            assert goal in await ReadFileTool().execute(path="result.txt")
            paths = json.loads(await render_tool.execute(path="result.txt", output_dir="out"))
            assert paths == {"source": str(folders[key] / "result.txt"), "output": str(folders[key] / "out")}
            if complete:
                await tool.execute([{"operation": "complete", "item_number": 1}])
            ctx.response = _text_response("delivered")
            assert (await hook.after_iteration(ctx)).rollback is not complete
            ctx.outbound_content = "delivered"
            tail = (await hook.after_send(ctx)).modified_content
            # The session-directory line rides on every turn in a minted
            # directory; what the completion gate decides is whether the
            # unfinished-items notice follows it.
            assert tail is not None and tail.startswith("delivered")
            assert ("[Task State]" in tail) is not complete

    await asyncio.wait_for(
        asyncio.gather(turn("acp:a", "Poster A", True), turn("acp:b", "Website B", False)), timeout=10
    )
    assert folders["acp:a"] != folders["acp:b"]
    assert all(folder.parent == root / "designs" for folder in folders.values())
    assert (root / "result.txt").read_text() == "Legacy shared artifact"
    assert manager.get(str(root))["goal"] == "Legacy shared task"
    assert workdir.current() is None

    resumed = _seated(DesignParticipant(EngineConfig.from_slice(config), None, TaskStateManager(tmp_path / "state")))
    with workdir.bind(root):
        ctx = AgentHookContext(session_key="acp:a", iteration=1)
        decision = await resumed.before_iteration(ctx)
        assert workdir.current() == folders["acp:a"]
        assert "Goal: Poster A" in decision.append_note and "Progress: 1/1 completed" in decision.append_note
        assert str(folders["acp:a"]) in decision.append_note
        assert "Poster A" in await ReadFileTool().execute(path="result.txt")
        await resumed.before_iteration(ctx)
        assert workdir.current() == folders["acp:a"]


@pytest.mark.asyncio
async def test_session_folder_names_preserve_distinct_full_keys(tmp_path):
    hook = _seated(DesignParticipant(EngineConfig.from_slice({"enabled": True}), None, None))
    keys = [
        "acp:alpha",
        "tui:alpha",
        "acp:session_a",
        "acp:session-a",
        "acp:" + "a" * 200 + "x",
        "acp:" + "a" * 200 + "y",
        "acp:../../",
        "acp:\u8bbe\u8ba1" * 100,
    ]
    folders = []
    for key in keys:
        with workdir.bind(tmp_path):
            await hook.before_iteration(AgentHookContext(session_key=key, iteration=1))
            own = workdir.current()
            assert own.parent == tmp_path / "designs"
            assert own.name.isascii() and len(own.name) <= 80
            folders.append(own)
    assert len(set(folders)) == len(keys)
    assert workdir.current() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_user_inbound", "before_iteration"])
@pytest.mark.parametrize("link_level", ["designs", "session"])
@pytest.mark.parametrize("target_kind", ["outside", "inside", "missing"])
async def test_session_directory_rejects_redirected_children(tmp_path, phase, link_level, target_kind):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("Outside workspace sentinel")
    target = root / "another-session" if target_kind == "inside" else outside
    if target_kind == "inside":
        target.mkdir()
    elif target_kind == "missing":
        target = outside / "missing"
    hook = _seated(DesignParticipant(EngineConfig.from_slice({"enabled": True}), None, None))
    ctx = AgentHookContext(session_key="acp:linked", inbound_content="draw a poster", iteration=1)
    with workdir.bind(root):
        await hook.before_user_inbound(ctx)
        own = workdir.current()
    own.rmdir()
    link = own
    if link_level == "designs":
        own.parent.rmdir()
        link = own.parent
    link.symlink_to(target, target_is_directory=True)
    before = set(tmp_path.rglob("*"))

    with workdir.bind(root):
        decision = await getattr(hook, phase)(ctx)
        assert decision.short_circuit_result is not None
        assert "symlink" in str(decision.short_circuit_result).lower()
        assert workdir.current() == root
        reader = ReadFileTool(workspace=root, allowed_dirs=(root,))
        assert "Outside workspace sentinel" not in await reader.execute(path=str(secret))
        assert "Outside workspace sentinel" not in await reader.execute(path=str(link / "secret.txt"))
        writer = WriteFileTool(workspace=root, allowed_dirs=(root,))
        result = await writer.execute(path=str(outside / "created.txt"), content="Must not be written")
        assert "outside allowed directories" in result
    assert set(tmp_path.rglob("*")) == before
    assert secret.read_text() == "Outside workspace sentinel"
    assert workdir.current() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("link_level", ["designs", "session"])
async def test_repointed_session_is_revalidated_before_iteration(tmp_path, link_level):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("Outside workspace sentinel")
    hook = _seated(DesignParticipant(EngineConfig.from_slice({"enabled": True}), None, None))
    ctx = AgentHookContext(session_key="acp:replaced", inbound_content="draw a poster", iteration=1)
    with workdir.bind(root):
        await hook.before_user_inbound(ctx)
        own = workdir.current()
        own.rmdir()
        link = own
        if link_level == "designs":
            own.parent.rmdir()
            link = own.parent
        link.symlink_to(outside, target_is_directory=True)
        decision = await hook.before_iteration(ctx)
        assert decision.short_circuit_result is not None
        assert workdir.current() == root
        result = await ReadFileTool(workspace=root, allowed_dirs=(root,)).execute(path=str(outside / "secret.txt"))
        assert "outside allowed directories" in result
    assert not (outside / own.name).exists()


@pytest.mark.asyncio
async def test_session_directory_accepts_a_symlinked_workspace_root(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(root, target_is_directory=True)
    ctx = AgentHookContext(session_key="acp:alias", inbound_content="draw a poster", iteration=1)
    hook = _seated(DesignParticipant(EngineConfig.from_slice({"enabled": True}), None, None))
    with workdir.bind(alias):
        assert (await hook.before_user_inbound(ctx)).short_circuit_result is None
        own = workdir.current()
        assert own == own.resolve()
        assert own.parent == root / "designs"
        assert (await hook.before_iteration(ctx)).short_circuit_result is None
        assert workdir.current() == own
        await WriteFileTool(workspace=alias, allowed_dirs=(alias,)).execute(path="poster.txt", content="Session poster")
    with workdir.bind(alias):
        assert (await hook.before_iteration(ctx)).short_circuit_result is None
        assert workdir.current() == own
        assert "Session poster" in await ReadFileTool(workspace=alias, allowed_dirs=(alias,)).execute(path="poster.txt")


# --- the selector hook: the four D1 clauses --------------------------------------


@pytest.mark.asyncio
async def test_command_shaped_and_blank_inbounds_pass_untouched(tmp_path):
    stub = _StubSelector(_selection())
    hook = _seated(DesignParticipant(EngineConfig.from_slice(SHIPPED_SLICE), stub, None))
    for text in ("/new", "  /model haiku", "", "   ", None):
        with workdir.bind(tmp_path):
            decision = await hook.before_user_inbound(AgentHookContext(session_key="acp:s1", inbound_content=text))
            assert workdir.current() == tmp_path
        assert decision.modified_content is None
    assert stub.queries == [], "no selection call was spent on any of them"
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_the_selection_block_rides_below_a_separator_with_local_ids():
    hook = _seated(DesignParticipant(EngineConfig.from_slice(SHIPPED_SLICE), _StubSelector(_selection()), None))
    decision = await hook.before_user_inbound(AgentHookContext(session_key="acp:s1", inbound_content="design a logo"))
    rewritten = decision.modified_content
    assert rewritten.startswith("design a logo\n\n---\n")
    assert f"`{FOUNDATION_SKILL_ID}`" in rewritten
    assert "`local/design-brand-identities`" in rewritten
    assert "## Preferred Skills" in rewritten
    body_forbidden = "## 0."
    assert body_forbidden not in rewritten, "cards only, never SKILL.md bodies"


@pytest.mark.asyncio
async def test_a_selection_failure_outside_the_selectors_guard_passes_untouched():
    hook = _seated(
        DesignParticipant(EngineConfig.from_slice(SHIPPED_SLICE), _StubSelector(error=RuntimeError("boom")), None)
    )
    decision = await hook.before_user_inbound(AgentHookContext(session_key="acp:s1", inbound_content="design a logo"))
    assert decision.modified_content is None and decision.short_circuit_result is None


def test_the_degraded_catalog_block_says_so():
    degraded = VisualDomainSelection((), _selection().alternatives, degraded=True)
    block = render_selection_block(degraded)
    assert "selector call failed" in block
    assert "## Alternative Skills" in block


def test_the_shipped_config_stays_on_the_pull_lane():
    """D1 clause 3: the seat assumes pull discovery. The shipped product
    config must not spell any skillForge discovery choice -- trunk's factory
    default (pull) is the lane this hook was adjudicated on."""
    config = json.loads((REPO / "agents" / "raven-design" / "config.json").read_text())
    assert "skillForge" not in config


# --- the task-state seats -------------------------------------------------------


def _loop(tmp_path, provider, hooks) -> AgentLoop:
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=4),
        host=HostWiring(hooks=hooks),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


class _ScriptedProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat_with_retry(self, **kwargs):
        snapshot = dict(kwargs)
        snapshot["messages"] = [dict(m) for m in kwargs.get("messages") or []]
        self.calls.append(snapshot)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]

    def get_default_model(self):
        return "fake/model"


def _req(text: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
    )


def _text_response(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [Origin.USER, Origin.SUBAGENT])
async def test_loop_uses_the_session_directory_for_tools_and_prompt(tmp_path, origin):
    config = {
        "enabled": True,
        "visualDomainSelector": {"enabled": False},
        "taskState": {"stateRoot": str(tmp_path / "state")},
    }
    plugin_ctx = _fresh(config)
    hook = make_hook(plugin_ctx)
    task_tool = make_update_task_state(plugin_ctx)
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="state",
                        name="update_task_state",
                        arguments={
                            "operations": [
                                {
                                    "operation": "initialize",
                                    "state": {"goal": "Ship a poster", "items": [{"title": "Write it"}]},
                                }
                            ]
                        },
                    ),
                    ToolCallRequest(
                        id="write", name="write_file", arguments={"path": "poster.txt", "content": "Session poster"}
                    ),
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="complete",
                        name="update_task_state",
                        arguments={"operations": [{"operation": "complete", "item_number": 1}]},
                    )
                ],
                finish_reason="tool_calls",
            ),
            _text_response("delivered"),
        ]
    )
    loop = _loop(tmp_path / "home", provider, [hook])
    loop.tools.register(task_tool)
    root = tmp_path / "work"
    root.mkdir()
    req = _req("make a poster")
    with workdir.bind(root):
        out = await loop._process_message(req, origin=origin)
        own = workdir.current()
    assert out is not None
    assert own.parent == root / "designs"
    assert (own / "poster.txt").read_text() == "Session poster"
    assert not (root / "poster.txt").exists()
    assert str(own) in str(provider.calls[0]["messages"])
    assert "Goal: Ship a poster" in str(provider.calls[1]["messages"])
    assert hook.factory()._manager.get(str(own))["items"][0]["status"] == "completed"
    assert workdir.current() is None


@pytest.mark.asyncio
async def test_the_reply_names_the_session_directory_the_run_wrote_into(tmp_path):
    """The caller's handoff, on the one reply the caller reads.

    This engine keeps a session's files in a directory of its own, one level
    below the directory the caller dispatched into, and the reply names them as
    relative paths. The dispatch receipt names the directory it dispatched into,
    so a caller that resolved those paths against the receipt delivered nothing
    and had to search the disk for files its run had written all along (measured
    on a real dispatch, 2026-09-16). The reply therefore names the directory the
    files are in, and says which way to resolve against it.
    """
    config = {"enabled": True, "visualDomainSelector": {"enabled": False}, "taskState": {"enabled": False}}
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="write", name="write_file", arguments={"path": "poster.txt", "content": "Session poster"}
                    )
                ],
                finish_reason="tool_calls",
            ),
            _text_response("The poster is in poster.txt."),
        ]
    )
    hook = _seated(DesignParticipant(EngineConfig.from_slice(config), None, None))
    loop = _loop(tmp_path / "home", provider, [hook])
    root = tmp_path / "work"
    root.mkdir()
    with workdir.bind(root):
        out = await loop._process_message(_req("make a poster"), origin=Origin.USER)
        own = workdir.current()
    assert out is not None and out[0] is not None
    reply = out[0]
    assert (own / "poster.txt").is_file()
    assert own.parent == root / "designs", "the run wrote into a directory of its own"
    assert f"Design session directory: {own}" in reply
    assert "Paths in this reply resolve against it." in reply
    # What the contract buys: a caller holding nothing but this reply reaches the
    # file the reply names, without searching the disk for it.
    named = Path(reply.split("Design session directory: ", 1)[1].split("\n", 1)[0])
    assert named.is_absolute() and (named / "poster.txt").is_file()


@pytest.mark.asyncio
async def test_no_session_directory_line_for_a_binding_the_engine_did_not_mint(tmp_path):
    """Only a directory this engine minted is named.

    With the per-session mode off the turn runs in the caller's own directory,
    and with no directory phase fired the binding is the caller's either way --
    a line naming it would restate the receipt's own claim, and the caller of a
    run that never moved has nothing to resolve differently.
    """
    for slice_config in ({"enabled": True}, {"enabled": True, "workdirPerSession": False}):
        hook = _seated(DesignParticipant(EngineConfig.from_slice(slice_config), None, None))
        with workdir.bind(tmp_path):
            decision = await hook.after_send(SimpleNamespace(outbound_content="done"))
        assert decision.modified_content is None


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [Origin.USER, Origin.SUBAGENT])
@pytest.mark.parametrize("obstacle", ["symlink", "file"])
async def test_loop_stops_when_session_directory_is_unsafe(tmp_path, origin, obstacle):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if obstacle == "symlink":
        (root / "designs").symlink_to(outside, target_is_directory=True)
    else:
        (root / "designs").write_text("Not a directory")
    provider = _ScriptedProvider([_text_response("The unsafe turn must not reach the model")])
    selector = _StubSelector(_selection())
    manager = TaskStateManager(tmp_path / "state")
    hook = _seated(DesignParticipant(EngineConfig.from_slice({"enabled": True}), selector, manager))
    loop = _loop(tmp_path / "home", provider, [hook])
    with workdir.bind(root):
        result = await loop._process_message(_req("draw a poster"), origin=origin)
        assert workdir.current() == root
    assert result is not None and "Design session directory unavailable" in result[0]
    assert not provider.calls
    assert not selector.queries
    assert not list(outside.iterdir())
    assert not list((tmp_path / "state").rglob("*.json"))


@pytest.mark.asyncio
async def test_task_state_projection_rides_append_note_per_iteration(tmp_path):
    manager = TaskStateManager(tmp_path / "ts")
    seen_keys: list[str] = []

    class Probe(DesignParticipant):
        async def advise(self, step):
            note = await super().advise(step)
            key = self._state_key()
            if key is not None:
                seen_keys.append(key)
            return note

    cfg = EngineConfig.from_slice(SHIPPED_SLICE)
    hook = _seated(Probe(cfg, None, manager))

    provider = _ScriptedProvider([_text_response("noted")])
    loop = _loop(tmp_path, provider, [hook])
    # This minimal harness wires no session-workdir resolver, so the turn is
    # bound explicitly -- the seat the ACP host binds per turn (w109 family).
    with workdir.bind(tmp_path):
        out = await loop._process_message(_req("start the task"))
    assert out is not None and seen_keys, "the phase fired inside the workdir bind"
    assert Path(seen_keys[0]).parent == tmp_path / "designs"

    manager.apply(
        seen_keys[0],
        [{"operation": "initialize", "state": {"goal": "Ship the poster", "items": [{"title": "Sketch"}]}}],
    )
    provider2 = _ScriptedProvider([_text_response("working")])
    loop2 = _loop(tmp_path, provider2, [_seated(Probe(cfg, None, manager))])
    with workdir.bind(tmp_path):
        out2 = await loop2._process_message(_req("continue"))
    assert out2 is not None
    dispatched = str(provider2.calls[0]["messages"][-1]["content"])
    assert "<task_state>" in dispatched, "the projection reached the model call as an appended note"
    assert "Ship the poster" in dispatched


@pytest.mark.asyncio
async def test_completion_notice_rides_after_send(tmp_path):
    manager = TaskStateManager(tmp_path / "ts")
    hook = _seated(DesignParticipant(EngineConfig.from_slice(SHIPPED_SLICE), None, manager))
    with workdir.bind(tmp_path):
        key = str(tmp_path)
        manager.apply(
            key,
            [{"operation": "initialize", "state": {"goal": "Ship it", "items": [{"title": "Draft"}]}}],
        )
        decision = await hook.after_send(SimpleNamespace(outbound_content="done for now"))
        assert decision.modified_content.startswith("done for now\n\n[Task State] This task is not complete")
        manager.apply(key, [{"operation": "complete", "item_number": 1}])
        decision = await hook.after_send(SimpleNamespace(outbound_content="all wrapped"))
        assert decision.modified_content is None
    assert completion_notice(manager, str(tmp_path)) is None


@pytest.mark.asyncio
async def test_stale_ledger_sends_the_turn_back_once(tmp_path):
    """A reply ending the turn on an unfinished ledger is still a draft.

    The notice travels with the reply, and on a dispatch there is no next turn
    to act on it -- the caller's judge reads it as work not done. So the turn
    goes back once and the agent settles its own record; whatever the second
    answer says then stands, because an item still open after that is a fact
    the judge should see.
    """
    manager = TaskStateManager(tmp_path / "ts")
    hook = _seated(DesignParticipant(EngineConfig.from_slice(SHIPPED_SLICE), None, manager))

    def iteration(response: LLMResponse, meta: dict) -> AgentHookContext:
        return AgentHookContext(session_key="s1", iteration=1, messages=[], response=response, metadata=meta)

    ending = LLMResponse(content="delivered", finish_reason="stop")
    with workdir.bind(tmp_path):
        key = str(tmp_path)
        manager.apply(
            key,
            [{"operation": "initialize", "state": {"goal": "Ship it", "items": [{"title": "Draft"}]}}],
        )
        turn: dict = {}
        first = await hook.after_iteration(iteration(ending, turn))
        assert first.rollback is True
        assert "[Task State] This task is not complete" in first.rollback_inject[0]["content"]

        assert (await hook.after_iteration(iteration(ending, turn))).rollback is False, "once per turn"

        mid = LLMResponse(
            content="reading the brief",
            tool_calls=[ToolCallRequest(id="1", name="read_file", arguments={})],
            finish_reason="tool_calls",
        )
        assert (await hook.after_iteration(iteration(mid, {}))).rollback is False, "a tool call is not the turn ending"

        manager.apply(key, [{"operation": "complete", "item_number": 1}])
        settled = await hook.after_iteration(iteration(ending, {}))
        assert settled.rollback is False, "a settled ledger has nothing to reconcile"


# --- the parameterized history pin (replaces the A/B no-pollution axis) ----------


@pytest.mark.asyncio
async def test_history_keeps_the_users_words_not_the_selector_cards(tmp_path):
    """The design-parameterized form of the generic pin
    (test_agent_loop_iteration_hooks.py): the model sees the card block, the
    persisted user row keeps the user's own words -- and the memory store's
    payload is read from that restored slice, so the block structurally
    cannot reach extraction."""
    hook = _seated(DesignParticipant(EngineConfig.from_slice(SHIPPED_SLICE), _StubSelector(_selection()), None))
    provider = _ScriptedProvider([_text_response("on it")])
    loop = _loop(tmp_path, provider, [hook])

    out = await loop._process_message(_req("design a poster for the jazz festival"))

    assert out is not None
    dispatched = str(provider.calls[0]["messages"][-1]["content"])
    assert "## Preferred Skills" in dispatched, "the model saw the cards"
    session = loop.sessions.get_or_create("cli:c")
    users = [m for m in session.messages if m.get("role") == "user"]
    assert users and users[-1]["content"] == "design a poster for the jazz festival"
    assert all("## Preferred Skills" not in str(m.get("content")) for m in session.messages)


@pytest.mark.asyncio
async def test_a_cancelled_turn_still_keeps_the_users_words(tmp_path):
    """The broken-turn half of the same guarantee (the H1 kernel repair):
    a cancelled turn used to persist the hook-rewritten envelope as the
    user's own words -- exactly the outcome an interactive design session
    hits most -- because _save_broken_turn dropped inbound_original. The
    pass-through now rides both exit doors; this pins the cancelled one."""

    class _CancellingProvider:
        def __init__(self):
            self.calls = []

        async def chat_with_retry(self, **kwargs):
            self.calls.append(dict(kwargs))
            raise asyncio.CancelledError()

        def get_default_model(self):
            return "fake/model"

    hook = _seated(DesignParticipant(EngineConfig.from_slice(SHIPPED_SLICE), _StubSelector(_selection()), None))
    provider = _CancellingProvider()
    loop = _loop(tmp_path, provider, [hook])

    with pytest.raises(asyncio.CancelledError):
        await loop._process_message(_req("design a poster"))

    assert provider.calls, "the turn reached the model with the rewrite before the cancel"
    session = loop.sessions.get_or_create("cli:c")
    users = [m for m in session.messages if m.get("role") == "user"]
    assert users and users[-1]["content"] == "design a poster"
    assert all("## Preferred Skills" not in str(m.get("content")) for m in session.messages)


# --- the git-changes reply appendix (the amended D3 rebuild) ---------------------


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.mark.asyncio
async def test_the_reply_carries_the_git_changes_appendix_in_a_checkout(tmp_path):
    """The fork launcher summarized the caller workspace's working tree after
    every answer (names and counts, never a patch); rebuilt on after_send,
    gated to git trees. A dirty tree lists the paths; the notice (when a
    task is unfinished) rides FIRST -- the fork's own order: loop notice,
    then wrapper summary."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "logo.svg").write_text("<svg/>")

    manager = TaskStateManager(tmp_path / "ts")
    hook = _seated(DesignParticipant(EngineConfig.from_slice(SHIPPED_SLICE), None, manager))
    with workdir.bind(repo):
        manager.apply(
            str(repo),
            [{"operation": "initialize", "state": {"goal": "Ship it", "items": [{"title": "Draft"}]}}],
        )
        decision = await hook.after_send(SimpleNamespace(outbound_content="done for now"))

    body = decision.modified_content
    notice_at = body.index("[Task State]")
    summary_at = body.index("--- working tree of")
    assert body.startswith("done for now")
    assert notice_at < summary_at, "notice first, git summary last -- the fork's order"
    assert "logo.svg" in body


@pytest.mark.asyncio
async def test_a_clean_checkout_still_answers_and_a_plain_directory_passes_untouched(tmp_path):
    hook = _seated(DesignParticipant(EngineConfig.from_slice(SHIPPED_SLICE), None, None))

    repo = tmp_path / "clean"
    repo.mkdir()
    _git(repo, "init", "-q")
    with workdir.bind(repo):
        decision = await hook.after_send(SimpleNamespace(outbound_content="all set"))
    assert decision.modified_content.endswith("no files were changed in the working tree")

    plain = tmp_path / "plain"
    plain.mkdir()
    with workdir.bind(plain):
        decision = await hook.after_send(SimpleNamespace(outbound_content="all set"))
    assert decision.modified_content is None
