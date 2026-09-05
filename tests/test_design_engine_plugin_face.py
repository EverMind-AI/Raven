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
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.contracts.llm_provider import LLMResponse
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
    DesignEngineHook,
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
    assert isinstance(hook, DesignEngineHook)
    assert len(hook._selector.cards) == 15
    assert hook._manager is None, "the shipped slice carries no stateRoot until the launcher renders one"


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
    assert hook._manager is not None


# --- the selector hook: the four D1 clauses --------------------------------------


@pytest.mark.asyncio
async def test_command_shaped_and_blank_inbounds_pass_untouched():
    stub = _StubSelector(_selection())
    hook = DesignEngineHook(EngineConfig.from_slice(SHIPPED_SLICE), stub, None)
    for text in ("/new", "  /model haiku", "", "   ", None):
        decision = await hook.before_user_inbound(SimpleNamespace(inbound_content=text))
        assert decision.modified_content is None
    assert stub.queries == [], "no selection call was spent on any of them"


@pytest.mark.asyncio
async def test_the_selection_block_rides_below_a_separator_with_local_ids():
    hook = DesignEngineHook(EngineConfig.from_slice(SHIPPED_SLICE), _StubSelector(_selection()), None)
    decision = await hook.before_user_inbound(SimpleNamespace(inbound_content="design a logo"))
    rewritten = decision.modified_content
    assert rewritten.startswith("design a logo\n\n---\n")
    assert f"`{FOUNDATION_SKILL_ID}`" in rewritten
    assert "`local/design-brand-identities`" in rewritten
    assert "## Preferred Skills" in rewritten
    body_forbidden = "## 0."
    assert body_forbidden not in rewritten, "cards only, never SKILL.md bodies"


@pytest.mark.asyncio
async def test_a_selection_failure_outside_the_selectors_guard_passes_untouched():
    hook = DesignEngineHook(EngineConfig.from_slice(SHIPPED_SLICE), _StubSelector(error=RuntimeError("boom")), None)
    decision = await hook.before_user_inbound(SimpleNamespace(inbound_content="design a logo"))
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
async def test_task_state_projection_rides_append_note_per_iteration(tmp_path):
    manager = TaskStateManager(tmp_path / "ts")
    seen_keys: list[str] = []

    class Probe(DesignEngineHook):
        async def before_iteration(self, ctx):
            key = self._session_key()
            if key is not None:
                seen_keys.append(key)
            return await super().before_iteration(ctx)

    cfg = EngineConfig.from_slice(SHIPPED_SLICE)
    hook = Probe(cfg, None, manager)

    provider = _ScriptedProvider([_text_response("noted")])
    loop = _loop(tmp_path, provider, [hook])
    # This minimal harness wires no session-workdir resolver, so the turn is
    # bound explicitly -- the seat the ACP host binds per turn (w109 family).
    with workdir.bind(tmp_path):
        out = await loop._process_message(_req("start the task"))
    assert out is not None and seen_keys, "the phase fired inside the workdir bind"
    assert seen_keys[0] == str(tmp_path)

    manager.apply(
        seen_keys[0],
        [{"operation": "initialize", "state": {"goal": "Ship the poster", "items": [{"title": "Sketch"}]}}],
    )
    provider2 = _ScriptedProvider([_text_response("working")])
    loop2 = _loop(tmp_path, provider2, [Probe(cfg, None, manager)])
    with workdir.bind(tmp_path):
        out2 = await loop2._process_message(_req("continue"))
    assert out2 is not None
    dispatched = str(provider2.calls[0]["messages"][-1]["content"])
    assert "<task_state>" in dispatched, "the projection reached the model call as an appended note"
    assert "Ship the poster" in dispatched


@pytest.mark.asyncio
async def test_completion_notice_rides_after_send(tmp_path):
    manager = TaskStateManager(tmp_path / "ts")
    hook = DesignEngineHook(EngineConfig.from_slice(SHIPPED_SLICE), None, manager)
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


# --- the parameterized history pin (replaces the A/B no-pollution axis) ----------


@pytest.mark.asyncio
async def test_history_keeps_the_users_words_not_the_selector_cards(tmp_path):
    """The design-parameterized form of the generic pin
    (test_agent_loop_iteration_hooks.py): the model sees the card block, the
    persisted user row keeps the user's own words -- and the memory store's
    payload is read from that restored slice, so the block structurally
    cannot reach extraction."""
    hook = DesignEngineHook(EngineConfig.from_slice(SHIPPED_SLICE), _StubSelector(_selection()), None)
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

    hook = DesignEngineHook(EngineConfig.from_slice(SHIPPED_SLICE), _StubSelector(_selection()), None)
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
    from raven_design.plugin.hook import DesignEngineHook

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "logo.svg").write_text("<svg/>")

    manager = TaskStateManager(tmp_path / "ts")
    hook = DesignEngineHook(EngineConfig.from_slice(SHIPPED_SLICE), None, manager)
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
    from raven_design.plugin.hook import DesignEngineHook

    hook = DesignEngineHook(EngineConfig.from_slice(SHIPPED_SLICE), None, None)

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
