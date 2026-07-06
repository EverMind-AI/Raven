"""Tests for progressive tool disclosure (tool_index + tool_search)."""

import json
from typing import Any

import pytest

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.tool_index import ToolIndex
from raven.agent.tools.tool_search import (
    DEFAULT_ALWAYS_VISIBLE,
    META_TOOL_NAMES,
    TOOL_CALL_NAME,
    ToolCallTool,
    ToolDescribeTool,
    ToolSearchController,
    ToolSearchStrategy,
    ToolSearchTool,
)


class _FakeTool(Tool):
    def __init__(self, name: str, description: str) -> None:
        self._name = name
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

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


def test_controller_search_compact_has_no_parameters() -> None:
    reg = ToolRegistry()
    reg.register(_FakeTool("create_issue", "open a github issue"))
    ctrl = _controller(reg)
    ctrl.refresh()
    hits = ctrl.search("github issue")
    assert hits[0]["name"] == "create_issue"
    assert "parameters" not in hits[0]


def test_controller_describe_returns_full_schema() -> None:
    reg = ToolRegistry()
    reg.register(_FakeTool("create_issue", "open a github issue"))
    ctrl = _controller(reg)
    fn = ctrl.describe("create_issue")
    assert fn is not None and fn["name"] == "create_issue" and "parameters" in fn


def test_controller_describe_unknown_returns_none() -> None:
    ctrl = _controller(ToolRegistry())
    assert ctrl.describe("nope") is None


def test_controller_describe_rejects_meta() -> None:
    assert _controller(ToolRegistry()).describe("tool_search") is None


def test_meta_includes_tool_call() -> None:
    assert TOOL_CALL_NAME in META_TOOL_NAMES


def test_visible_names_are_stable_and_include_meta() -> None:
    ctrl = ToolSearchController(ToolRegistry(), always_visible={"read_file"})
    assert META_TOOL_NAMES <= ctrl.visible_names()
    assert ctrl.visible_names() == ctrl.visible_names()


def test_meta_tools_not_self_searchable() -> None:
    reg = ToolRegistry()
    ctrl = _controller(reg)
    reg.register(ToolSearchTool(ctrl))
    reg.register(ToolDescribeTool(ctrl))
    reg.register(ToolCallTool(ctrl))
    reg.register(_FakeTool("create_issue", "open a github issue"))
    ctrl.refresh()
    names = [h["name"] for h in ctrl.search("tool search describe call")]
    assert not (META_TOOL_NAMES & set(names))


# ---- meta-tools execute ----


@pytest.mark.asyncio
async def test_tool_search_tool_returns_json_hits() -> None:
    reg = ToolRegistry()
    reg.register(_FakeTool("create_issue", "open a github issue"))
    ctrl = _controller(reg)
    ctrl.refresh()
    out = await ToolSearchTool(ctrl).execute(query="github issue")
    assert json.loads(out)[0]["name"] == "create_issue"


@pytest.mark.asyncio
async def test_tool_search_tool_no_match_message() -> None:
    reg = ToolRegistry()
    reg.register(_FakeTool("create_issue", "open a github issue"))
    ctrl = _controller(reg)
    ctrl.refresh()
    out = await ToolSearchTool(ctrl).execute(query="zzzznomatch")
    assert "No tools matched" in out


@pytest.mark.asyncio
async def test_tool_describe_tool_returns_schema() -> None:
    reg = ToolRegistry()
    reg.register(_FakeTool("create_issue", "open a github issue"))
    ctrl = _controller(reg)
    out = await ToolDescribeTool(ctrl).execute(name="create_issue")
    assert json.loads(out)["name"] == "create_issue"


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
    assert "not found" in await ctrl.call("nope", {})


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
    reg.register(ToolDescribeTool(ctrl))
    reg.register(ToolCallTool(ctrl))
    return reg, ctrl


@pytest.mark.asyncio
async def test_strategy_small_catalog_passthrough_drops_meta() -> None:
    reg, ctrl = _registry_with_n(3)
    strat = ToolSearchStrategy(ctrl, compaction_threshold=25)
    tools = reg.get_definitions()
    _, out, _ = await strat.before_llm_call([], tools, "m")
    out_names = {t["function"]["name"] for t in out}
    assert not (META_TOOL_NAMES & out_names), "meta-tools dropped below threshold"
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
    reg.unregister("tool_describe")
    reg.unregister(TOOL_CALL_NAME)
    strat = ToolSearchStrategy(ctrl, compaction_threshold=25)
    tools = reg.get_definitions()
    _, out, _ = await strat.before_llm_call([], tools, "m")
    assert {t["function"]["name"] for t in out} == {t["function"]["name"] for t in tools}


def test_registry_register_first_runs_before_others() -> None:
    from raven.token_wise.base import TokenStrategy
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
