"""Tests for progressive tool disclosure (tool_index + tool_search)."""

import json
from typing import Any

import pytest

from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.tool_index import ToolIndex, _schema_text
from raven.agent.tools.tool_search import (
    DEFAULT_ALWAYS_VISIBLE,
    META_TOOL_NAMES,
    TOOL_CALL_NAME,
    TOOL_SEARCH_NAME,
    ToolCallTool,
    ToolSearchController,
    ToolSearchStrategy,
    ToolSearchTool,
)
from raven.config.schema import ToolSearchConfig
from raven.contracts.tool import Tool


class _FakeTool(Tool):
    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._description = description
        self._parameters = parameters or {"type": "object", "properties": {}}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        return f"ran {self._name}"


# ---- ToolIndex ----


def test_index_ranks_name_match_first() -> None:
    idx = ToolIndex()
    tools = [
        _FakeTool("create_issue", "open a github issue"),
        _FakeTool("send_message", "post to a slack channel"),
    ]
    idx.ensure(tools)
    assert idx.search("issue", limit=5)[0] == "create_issue"


def test_index_chinese_query_hits() -> None:
    idx = ToolIndex()
    tools = [
        _FakeTool("image_generate", "生成图片 from a text prompt"),
        _FakeTool("read_file", "读取文件 contents"),
    ]
    idx.ensure(tools)
    assert idx.search("生成图片", limit=5)[0] == "image_generate"


def test_index_search_before_ensure_returns_empty() -> None:
    assert ToolIndex().search("anything", limit=5) == []


def test_index_rebuilds_on_name_or_description_change() -> None:
    idx = ToolIndex()
    idx.ensure([_FakeTool("a", "alpha")])
    first = idx._bm25
    idx.ensure([_FakeTool("a", "alpha")])  # identical catalog
    assert idx._bm25 is first, "should not rebuild when (name, description) is unchanged"
    idx.ensure([_FakeTool("a", "alpha changed")])  # description changed
    assert idx._bm25 is not first, "should rebuild when a description changes"
    idx.ensure([_FakeTool("a", "alpha changed"), _FakeTool("b", "beta")])  # name set grew
    assert idx._bm25 is not first, "should rebuild when the name set changes"


def test_index_matches_parameter_schema_keywords() -> None:
    # A discriminating keyword living only in the parameter schema (not the
    # one-line description) should still make the tool findable.
    idx = ToolIndex()
    tools = [
        _FakeTool(
            "create_issue",
            "open a ticket",
            parameters={
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "target github repository"},
                },
            },
        ),
        _FakeTool("send_message", "post to a channel"),
    ]
    idx.ensure(tools)
    assert idx.search("github repository", limit=5)[0] == "create_issue"


def test_schema_text_extracts_names_descriptions_enums_and_nesting() -> None:
    schema = {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "slack channel id"},
            "mode": {"type": "string", "enum": ["fast", "thorough"]},
            "opts": {
                "type": "object",
                "properties": {"retries": {"type": "integer", "description": "retry count"}},
            },
            "tags": {"type": "array", "items": {"type": "string", "description": "a label"}},
        },
    }
    text = _schema_text(schema)
    for token in (
        "channel",
        "slack channel id",
        "mode",
        "fast",
        "thorough",
        "opts",
        "retries",
        "retry count",
        "tags",
        "a label",
    ):
        assert token in text, f"{token!r} missing from schema text"


def test_schema_text_handles_non_dict_and_empty() -> None:
    assert _schema_text(None) == ""
    assert _schema_text([1, 2]) == ""  # type: ignore[arg-type]
    assert _schema_text({"type": "object"}) == ""  # no properties/desc/enum


def test_schema_text_respects_depth_cap() -> None:
    # Build nesting deeper than the cap; the deepest description must be dropped.
    node: dict[str, Any] = {"type": "string", "description": "TOODEEP"}
    for _ in range(10):
        node = {"type": "object", "properties": {"n": node}}
    assert "TOODEEP" not in _schema_text(node)


def test_index_rebuilds_on_parameters_change() -> None:
    base = {"type": "object", "properties": {"a": {"type": "string", "description": "alpha"}}}
    idx = ToolIndex()
    idx.ensure([_FakeTool("t", "desc", parameters=base)])
    first = idx._bm25
    idx.ensure([_FakeTool("t", "desc", parameters=dict(base))])  # same schema content
    assert idx._bm25 is first, "should not rebuild when parameters are unchanged"
    changed = {"type": "object", "properties": {"a": {"type": "string", "description": "beta"}}}
    idx.ensure([_FakeTool("t", "desc", parameters=changed)])  # param description changed
    assert idx._bm25 is not first, "should rebuild when a parameter description changes"


def test_index_reused_across_instances_for_same_catalog() -> None:
    tools = [_FakeTool("x", "xray"), _FakeTool("y", "yankee")]
    first = ToolIndex()
    first.ensure(tools)
    second = ToolIndex()
    second.ensure([_FakeTool("x", "xray"), _FakeTool("y", "yankee")])
    assert second._bm25 is first._bm25, "same catalog should reuse the cached BM25"


# ---- ToolSearchController ----


def _controller(registry: ToolRegistry) -> ToolSearchController:
    return ToolSearchController(
        registry,
        always_visible={"read_file"},
        search_result_limit=10,
    )


def test_controller_search_includes_parameters() -> None:
    schema = {
        "type": "object",
        "properties": {"repo": {"type": "string", "description": "target repository"}},
        "required": ["repo"],
    }
    reg = ToolRegistry()
    reg.register(_FakeTool("create_issue", "open a github issue", parameters=schema))
    ctrl = _controller(reg)
    ctrl.refresh()
    hits = ctrl.search("github issue")
    assert hits[0]["name"] == "create_issue"
    assert hits[0]["parameters"] == schema


def test_meta_includes_tool_call() -> None:
    assert TOOL_CALL_NAME in META_TOOL_NAMES


def test_meta_no_longer_includes_describe() -> None:
    assert "tool_describe" not in META_TOOL_NAMES
    assert META_TOOL_NAMES == {"tool_search", TOOL_CALL_NAME}


def test_default_search_result_limit_is_ten() -> None:
    assert ToolSearchConfig().search_result_limit == 10
    ctrl = ToolSearchController(ToolRegistry(), always_visible=set())
    assert ctrl.search_result_limit == 10


def test_default_always_visible_covers_core_and_interaction_primitives() -> None:
    # File/search/exec primitives plus the interaction/orchestration primitives
    # (message / ask_user / spawn) the agent must reach on any turn. Guards
    # against silently dropping one (which would strand it behind tool_search).
    assert set(DEFAULT_ALWAYS_VISIBLE) >= {
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "grep",
        "find",
        "exec",
        "message",
        "ask_user",
        "spawn",
    }


def test_optional_orchestration_and_skill_fetch_stay_searchable() -> None:
    """A minute-scale sub-agent fan-out and a skill-body fetch are deliberate
    acts, not per-turn primitives: they are reached through tool_search so the
    resident tool list does not grow with every optional capability. A deploy
    that wants either resident adds it via tools.tool_search.always_visible."""
    always = set(DEFAULT_ALWAYS_VISIBLE)
    assert "run_subagent_dag" not in always
    assert "read_skill" not in always
    assert "use_skill" not in always


def test_visible_names_are_stable_and_include_meta() -> None:
    ctrl = ToolSearchController(ToolRegistry(), always_visible={"read_file"})
    assert META_TOOL_NAMES <= ctrl.visible_names()
    assert ctrl.visible_names() == ctrl.visible_names()


def test_meta_tools_not_self_searchable() -> None:
    reg = ToolRegistry()
    ctrl = _controller(reg)
    reg.register(ToolSearchTool(ctrl))
    reg.register(ToolCallTool(ctrl))
    reg.register(_FakeTool("create_issue", "open a github issue"))
    ctrl.refresh()
    names = [h["name"] for h in ctrl.search("tool search describe call")]
    assert not (META_TOOL_NAMES & set(names))


# ---- meta-tools execute ----


@pytest.mark.asyncio
async def test_tool_search_tool_returns_json_hits_with_parameters() -> None:
    schema = {"type": "object", "properties": {"repo": {"type": "string"}}}
    reg = ToolRegistry()
    reg.register(_FakeTool("create_issue", "open a github issue", parameters=schema))
    ctrl = _controller(reg)
    ctrl.refresh()
    out = await ToolSearchTool(ctrl).execute(query="github issue")
    hit = json.loads(out)[0]
    assert hit["name"] == "create_issue"
    assert hit["parameters"] == schema


@pytest.mark.asyncio
async def test_tool_search_tool_no_match_message() -> None:
    reg = ToolRegistry()
    reg.register(_FakeTool("create_issue", "open a github issue"))
    ctrl = _controller(reg)
    ctrl.refresh()
    out = await ToolSearchTool(ctrl).execute(query="zzzznomatch")
    assert "No tools matched" in out


@pytest.mark.asyncio
async def test_tool_call_forwards_to_registry() -> None:
    reg = ToolRegistry()
    reg.register(_FakeTool("create_issue", "open a github issue"))
    ctrl = _controller(reg)
    out = await ToolCallTool(ctrl).execute(name="create_issue", arguments={})
    assert out == "ran create_issue"


@pytest.mark.asyncio
async def test_tool_call_rejects_meta_and_missing() -> None:
    ctrl = _controller(ToolRegistry())
    assert "cannot be invoked" in await ctrl.call("tool_search", {})
    assert "not available" in await ctrl.call("nope", {})


@pytest.mark.asyncio
async def test_tool_call_parses_stringified_arguments() -> None:
    reg = ToolRegistry()
    reg.register(_FakeTool("create_issue", "open a github issue"))
    ctrl = _controller(reg)
    # Model emitted the nested arguments as a JSON string instead of an object.
    out = await ctrl.call("create_issue", '{"x": 1}')
    assert out == "ran create_issue"


@pytest.mark.asyncio
async def test_tool_call_rejects_unparseable_arguments() -> None:
    ctrl = _controller(ToolRegistry())
    assert "must be a JSON object" in await ctrl.call("anything", "not json {")


# ---- ToolSearchStrategy ----


def _registry_with_n(n: int) -> tuple[ToolRegistry, ToolSearchController]:
    reg = ToolRegistry()
    for i in range(n):
        reg.register(_FakeTool(f"extra_{i}", f"extra tool number {i}"))
    ctrl = ToolSearchController(
        reg,
        always_visible=set(DEFAULT_ALWAYS_VISIBLE),
        search_result_limit=10,
    )
    reg.register(ToolSearchTool(ctrl))
    reg.register(ToolCallTool(ctrl))
    return reg, ctrl


@pytest.mark.asyncio
async def test_strategy_small_catalog_passthrough_drops_only_tool_search() -> None:
    reg, ctrl = _registry_with_n(3)
    strat = ToolSearchStrategy(ctrl, compaction_threshold=25)
    tools = reg.get_definitions()
    _, out, _ = await strat.before_llm_call([], tools, "m")
    out_names = {t["function"]["name"] for t in out}
    assert TOOL_SEARCH_NAME not in out_names, "nothing to search below threshold"
    assert TOOL_CALL_NAME in out_names, "schema-hidden tools need a name route at any catalog size"
    assert "extra_0" in out_names, "all real tools exposed below threshold"


@pytest.mark.asyncio
async def test_strategy_large_catalog_compacts_to_visible() -> None:
    reg, ctrl = _registry_with_n(40)
    strat = ToolSearchStrategy(ctrl, compaction_threshold=25)
    tools = reg.get_definitions()
    _, out, _ = await strat.before_llm_call([], tools, "m")
    out_names = {t["function"]["name"] for t in out}
    assert META_TOOL_NAMES <= out_names, "meta-tools stay visible above threshold"
    assert "extra_0" not in out_names, "cataloged tools are withheld above threshold"


@pytest.mark.asyncio
async def test_strategy_keeps_interaction_primitives_visible_above_threshold() -> None:
    # ask_user / spawn are in DEFAULT_ALWAYS_VISIBLE, so above the threshold they
    # keep their schema while ordinary cataloged tools are withheld.
    reg, ctrl = _registry_with_n(40)
    reg.register(_FakeTool("ask_user", "ask the user a clarifying question"))
    reg.register(_FakeTool("spawn", "spawn a subagent to handle a subtask"))
    strat = ToolSearchStrategy(ctrl, compaction_threshold=25)
    _, out, _ = await strat.before_llm_call([], reg.get_definitions(), "m")
    out_names = {t["function"]["name"] for t in out}
    assert {"ask_user", "spawn"} <= out_names, "interaction primitives must stay visible"
    assert "extra_0" not in out_names, "ordinary cataloged tools are still withheld"


@pytest.mark.asyncio
async def test_strategy_tool_list_stable_across_turns() -> None:
    # Core guarantee: the compacted tool list never changes turn-to-turn, so the
    # prompt cache stays valid (tools sit ahead of system+messages in the prefix).
    reg, ctrl = _registry_with_n(40)
    strat = ToolSearchStrategy(ctrl, compaction_threshold=25)
    first = {t["function"]["name"] for t in (await strat.before_llm_call([], reg.get_definitions(), "m"))[1]}
    second = {t["function"]["name"] for t in (await strat.before_llm_call([], reg.get_definitions(), "m"))[1]}
    assert first == second
    assert META_TOOL_NAMES <= first and "extra_0" not in first


@pytest.mark.asyncio
async def test_strategy_none_tools_passthrough() -> None:
    _, ctrl = _registry_with_n(40)
    strat = ToolSearchStrategy(ctrl, compaction_threshold=25)
    msgs, out, model = await strat.before_llm_call([{"role": "user"}], None, "m")
    assert out is None and model == "m"


@pytest.mark.asyncio
async def test_strategy_passthrough_when_meta_tools_absent() -> None:
    # Above threshold but meta-tools missing (e.g. removed via disabled_tools):
    # expose everything rather than strand cataloged tools.
    reg, ctrl = _registry_with_n(40)
    reg.unregister("tool_search")
    reg.unregister(TOOL_CALL_NAME)
    strat = ToolSearchStrategy(ctrl, compaction_threshold=25)
    tools = reg.get_definitions()
    _, out, _ = await strat.before_llm_call([], tools, "m")
    assert {t["function"]["name"] for t in out} == {t["function"]["name"] for t in tools}


def test_registry_register_first_runs_before_others() -> None:
    from raven.contracts.token_strategy import TokenStrategy
    from raven.token_wise.registry import StrategyRegistry

    class _Noop(TokenStrategy):
        def __init__(self, tag: str) -> None:
            self._tag = tag

        @property
        def name(self) -> str:
            return self._tag

    reg = StrategyRegistry([_Noop("a"), _Noop("b")])
    reg.register(_Noop("front"), first=True)
    reg.register(_Noop("back"))
    assert [s.name for s in reg.strategies] == ["front", "a", "b", "back"]


# ---- tool_call must not flatten its target's blocking verdict ----


def test_tool_call_reports_its_target_as_blocking() -> None:
    """``tool_call`` is a passthrough, so its blocking verdict is the target's.

    Reading the meta-tool's own flag instead double-wraps a blocking target: the
    registry applies its default ceiling to the outer ``tool_call`` and kills a
    sub-agent run that is deliberately deadline-free, and the turn stream is told
    the call is clockable when it is not.
    """
    reg = ToolRegistry()
    reg.register(_FakeTool("grep", "search files"))
    blocking = _FakeTool("run_subagent_dag", "orchestrate sub-agents")
    blocking.blocking_interaction = True
    reg.register(blocking)
    ctrl = _controller(reg)
    reg.register(ToolCallTool(ctrl))

    assert reg.is_blocking(TOOL_CALL_NAME, {"name": "run_subagent_dag"}) is True
    assert reg.is_blocking(TOOL_CALL_NAME, {"name": "grep"}) is False


def test_tool_call_with_an_unknown_or_meta_target_is_not_blocking() -> None:
    """No target, an unknown one, or a meta-tool: fall back to non-blocking so the
    registry keeps its backstop on a call that is going to error out anyway."""
    reg = ToolRegistry()
    ctrl = _controller(reg)
    reg.register(ToolCallTool(ctrl))
    reg.register(ToolSearchTool(ctrl))

    assert reg.is_blocking(TOOL_CALL_NAME, {}) is False
    assert reg.is_blocking(TOOL_CALL_NAME, {"name": "nope"}) is False
    assert reg.is_blocking(TOOL_CALL_NAME, {"name": TOOL_CALL_NAME}) is False
    assert reg.is_blocking(TOOL_CALL_NAME, {"name": "tool_search"}) is False


class _MetadataTool(_FakeTool):
    def take_metadata(self) -> dict[str, Any] | None:
        return {"raven_delivery": {"files": ["report.pdf"]}}


def test_tool_call_hands_back_its_target_metadata() -> None:
    """``tool_call`` produces no metadata of its own, so a forwarded tool's
    payload has to be collected from the target. Reading the forwarder instead
    strands the payload: the call succeeds, the model is told so, and the UI is
    handed nothing at all."""
    reg = ToolRegistry()
    reg.register(_MetadataTool("deliver_files", "hand files to the user"))
    ctrl = _controller(reg)
    reg.register(ToolCallTool(ctrl))

    payload = reg.take_metadata(TOOL_CALL_NAME, {"name": "deliver_files", "arguments": {}})

    assert payload == {"raven_delivery": {"files": ["report.pdf"]}}


def test_tool_call_with_an_unknown_or_meta_target_has_no_metadata() -> None:
    """No target, an unknown one, or a meta-tool: fall back to the forwarder's
    own (absent) metadata rather than raising on a call that already errors."""
    reg = ToolRegistry()
    ctrl = _controller(reg)
    reg.register(ToolCallTool(ctrl))
    reg.register(ToolSearchTool(ctrl))

    assert reg.take_metadata(TOOL_CALL_NAME, {}) is None
    assert reg.take_metadata(TOOL_CALL_NAME, {"name": "nope"}) is None
    assert reg.take_metadata(TOOL_CALL_NAME, {"name": TOOL_CALL_NAME}) is None
    assert reg.take_metadata("nope", {}) is None


def test_direct_call_still_takes_its_own_metadata() -> None:
    reg = ToolRegistry()
    reg.register(_MetadataTool("deliver_files", "hand files to the user"))

    assert reg.take_metadata("deliver_files", {}) == {"raven_delivery": {"files": ["report.pdf"]}}


@pytest.mark.asyncio
async def test_tool_call_does_not_timer_kill_a_blocking_target() -> None:
    """End-to-end: a slow blocking target reached via tool_call outlives the
    registry ceiling, instead of being cut off by the outer wrapper."""
    import asyncio

    class _SlowBlocking(_FakeTool):
        blocking_interaction = True

        async def execute(self, **kwargs: Any) -> str:
            await asyncio.sleep(0.2)
            return "ran run_subagent_dag"

    reg = ToolRegistry()
    reg.DEFAULT_TOOL_TIMEOUT_S = 0.05
    reg.register(_SlowBlocking("run_subagent_dag", "orchestrate sub-agents"))
    ctrl = _controller(reg)
    reg.register(ToolCallTool(ctrl))

    out = await reg.execute(TOOL_CALL_NAME, {"name": "run_subagent_dag", "arguments": {}})

    assert out == "ran run_subagent_dag"


# ---------------------------------------------------------------------------
# channel-bound tools: hidden from search must also mean unreachable by name
# ---------------------------------------------------------------------------


class _WebOnlyTool(_FakeTool):
    channels = frozenset({"web"})


def _web_only_registry() -> tuple[ToolRegistry, ToolSearchController]:
    reg = ToolRegistry()
    reg.register(_WebOnlyTool("deliver_files", "deliver finished output files to the user"))
    ctrl = _controller(reg)
    reg.register(ToolCallTool(ctrl))
    ctrl.refresh()
    return reg, ctrl


def test_search_hides_a_tool_this_channel_cannot_use() -> None:
    reg, ctrl = _web_only_registry()
    reg.set_channel("whatsapp")
    assert ctrl.search("deliver files") == []


def test_search_surfaces_it_on_its_own_channel() -> None:
    reg, ctrl = _web_only_registry()
    reg.set_channel("web")
    assert [h["name"] for h in ctrl.search("deliver files")] == ["deliver_files"]


def test_the_index_itself_stays_channel_independent() -> None:
    """The BM25 index is keyed on the catalog and shared by concurrent turns, so
    it must not vary per channel -- filtering happens on the read instead."""
    reg, ctrl = _web_only_registry()
    reg.set_channel("whatsapp")
    ctrl.refresh()
    assert "deliver_files" in [t.name for t in ctrl._catalog_tools()]


@pytest.mark.asyncio
async def test_tool_call_refuses_a_tool_this_channel_cannot_use() -> None:
    """Naming it directly is the way around a search-only filter, so tool_call
    has to apply the same predicate rather than fall through to the registry."""
    reg, _ = _web_only_registry()
    reg.set_channel("whatsapp")

    out = await reg.execute(TOOL_CALL_NAME, {"name": "deliver_files", "arguments": {}})

    assert "not available" in out
    # Worded exactly as an absent tool is: "it exists but not for you" is the
    # way around the filter that keeping it out of the schema was meant to close.
    assert out.replace("deliver_files", "ghost") == await reg.execute(
        TOOL_CALL_NAME, {"name": "ghost", "arguments": {}}
    )


@pytest.mark.asyncio
async def test_a_withheld_tool_does_not_consume_a_result_slot() -> None:
    """The filter must run before truncation. With it after, a withheld rank-1
    hit ate the only slot and the model was told nothing matched while a usable
    rank-2 tool existed -- worse than not having the filter at all."""
    reg = ToolRegistry()
    reg.register(_WebOnlyTool("deliver_files", "deliver files to the user"))
    reg.register(_FakeTool("message", "send a message with files to the user"))
    ctrl = _controller(reg)
    ctrl.refresh()

    # Rank 1 is the web-only tool, so a telegram turn has to fall through to it.
    assert ctrl._index.search("deliver files to the user", 10)[0] == "deliver_files"

    reg.set_channel("telegram")
    assert [h["name"] for h in ctrl.search("deliver files to the user", limit=1)] == ["message"]

    reg.set_channel("web")
    assert [h["name"] for h in ctrl.search("deliver files to the user", limit=1)] == ["deliver_files"]


class TestAlwaysVisibleResolvesConfiguredNames:
    """A configured name has to keep matching after its tool is renamed.

    ``always_visible`` is what a deploy uses to hold a tool resident while the
    rest of the catalog is folded behind ``tool_search``. It matched by exact
    set membership, so a namespaced tool whose registered name differs from the
    configured spelling -- sanitising and the length cap both do that -- dropped
    out of the resident set and back into the catalog. No error: the model just
    has to search for it first.
    """

    def _mcp(self, registry, server: str, tool: str):
        from raven.mcp.naming import MCPToolRef, tool_name

        name = tool_name(server, tool, taken=registry)
        registry.register(
            _FakeTool(name, f"{tool} on {server}"), origin=MCPToolRef(name=name, server=server, tool=tool)
        )
        return name

    def test_a_configured_name_that_was_sanitised_still_resolves(self):
        reg = ToolRegistry()
        # The registered name loses the slash; the config entry still has it.
        registered = self._mcp(reg, "gh", "actions/download-logs")
        assert registered == "mcp_gh_actions_download-logs"

        search = ToolSearchController(reg, always_visible={"mcp_gh_actions/download-logs"})
        assert registered in search.visible_names()

    def test_an_exactly_named_tool_is_visible(self):
        reg = ToolRegistry()
        registered = self._mcp(reg, "gh", "list_repos")
        search = ToolSearchController(reg, always_visible={registered})
        assert registered in search.visible_names()

    def test_a_builtin_still_matches_by_its_own_name(self):
        reg = ToolRegistry()
        reg.register(_FakeTool("grep", "search files"))
        search = ToolSearchController(reg, always_visible={"grep"})
        assert "grep" in search.visible_names()

    def test_a_name_matching_nothing_is_kept_as_written(self):
        # Usually a tool this deploy does not have, which is ordinary. Dropping
        # it would hide a typo rather than surface one.
        search = ToolSearchController(ToolRegistry(), always_visible={"not_installed_here"})
        assert "not_installed_here" in search.visible_names()

    def test_the_meta_tools_are_always_in(self):
        search = ToolSearchController(ToolRegistry(), always_visible=set())
        assert META_TOOL_NAMES <= search.visible_names()

    def test_a_tool_registered_after_construction_resolves(self):
        # MCP servers attach while raven runs, so resolution cannot happen once
        # at construction: the tool is not there yet.
        reg = ToolRegistry()
        search = ToolSearchController(reg, always_visible={"mcp_gh_actions/download-logs"})
        assert "mcp_gh_actions_download-logs" not in search.visible_names()

        registered = self._mcp(reg, "gh", "actions/download-logs")
        assert registered in search.visible_names()


class TestAnAbsentNameOnTheFoldedPath:
    """``tool_call`` answers a name it cannot resolve by pointing at tool_search.

    This surface has a catalog to search, so that is the useful advice --
    unlike ``ToolRegistry.execute``, which also serves the unfolded surface
    where there is nothing to search and therefore names both readings instead.
    A tool whose MCP server was unloaded mid-turn lands here too and the search
    finds nothing: one wasted hop, and the accepted price of not tracking what
    used to exist.

    Registering ``ToolSearchTool`` is what makes these the folded path: the
    pointer is withheld where the fold is off, because ``tool_call`` ships there
    too and naming a tool the deploy does not have is the broken promise this
    text is supposed to avoid.
    """

    @pytest.mark.asyncio
    async def test_an_unknown_name_is_told_where_the_catalog_is(self):
        reg = ToolRegistry()
        reg.register(_FakeTool("real_one", "a cataloged tool"))
        ctrl = ToolSearchController(reg, always_visible=set(), compaction_threshold=0)
        reg.register(ToolSearchTool(ctrl))
        out = await ctrl.call("mcp_ghost_thing", {})
        assert "not available" in out
        assert "tool_search" in out

    @pytest.mark.asyncio
    async def test_an_unknown_name_is_not_pointed_at_an_absent_tool_search(self):
        reg = ToolRegistry()
        ctrl = ToolSearchController(reg, always_visible=set())
        out = await ctrl.call("mcp_ghost_thing", {})
        assert "not available" in out
        assert "tool_search" not in out

    @pytest.mark.asyncio
    async def test_it_reads_the_same_way_as_the_direct_path(self):
        # Both surfaces answer a name that did not resolve, and neither can tell
        # a hallucinated one from a tool unloaded mid-turn. Saying it two
        # different ways -- one of them "you got the name wrong" -- was the
        # disagreement this pins shut. Only the catalog pointer differs, because
        # only this surface has a catalog the model cannot already see.
        reg = ToolRegistry()
        ctrl = ToolSearchController(reg, always_visible=set())
        folded = await ctrl.call("ghost", {})
        direct = await reg.execute("ghost", {})
        for phrase in ("is not available", "may have been unloaded", "the name may be wrong"):
            assert phrase in folded, phrase
            assert phrase in direct, phrase
        assert "tool_search" not in direct

    @pytest.mark.asyncio
    async def test_an_unregistered_tool_is_answered_the_same_way(self):
        from raven.mcp.naming import MCPToolRef

        reg = ToolRegistry()
        name = "mcp_openseo_search"
        reg.register(_FakeTool(name, "search openseo"), origin=MCPToolRef(name=name, server="openseo", tool="search"))
        reg.unregister(name)
        reg.register(_FakeTool("real_one", "a cataloged tool"))
        ctrl = ToolSearchController(reg, always_visible=set(), compaction_threshold=0)
        reg.register(ToolSearchTool(ctrl))
        out = await ctrl.call(name, {})
        assert "not available" in out
        assert "tool_search" in out
        # Same answer as a name that never existed: the registry keeps no record
        # of what it used to hold, so there is nothing to say differently.
        assert out.replace(name, "GHOST") == await ctrl.call("GHOST", {})

    @pytest.mark.asyncio
    async def test_a_channel_withheld_tool_is_refused_as_absent(self):
        # The channel filter must not become reachable by naming a tool
        # directly: "it exists but not for you" is the way around the filter
        # that keeping it out of the schema was meant to close.
        class _Bound(_FakeTool):
            channels = frozenset({"web"})

        reg = ToolRegistry()
        reg.register(_Bound("only_on_web", "web-only tool"))
        reg.set_channel("telegram")
        ctrl = ToolSearchController(reg, always_visible=set())

        out = await ctrl.call("only_on_web", {})
        assert "not available" in out
        # And it must not read as "it exists, just not for you" -- pointing at
        # the catalog is safe because ``search`` filters by channel too.
        assert [h["name"] for h in ctrl.search("web-only tool")] == []

    def test_the_search_index_never_offers_an_unregistered_tool(self):
        from raven.mcp.naming import MCPToolRef

        reg = ToolRegistry()
        name = "mcp_openseo_search"
        reg.register(_FakeTool(name, "search openseo"), origin=MCPToolRef(name=name, server="openseo", tool="search"))
        reg.unregister(name)
        ctrl = ToolSearchController(reg, always_visible=set())
        ctrl.refresh()
        assert [h["name"] for h in ctrl.search("openseo search")] == []
