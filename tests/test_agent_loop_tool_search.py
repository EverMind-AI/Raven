"""Wiring tests for tool-search registration inside AgentLoop.__init__.

Covers the ``_register_default_tools`` block: the meta-tools land in the
registry and the strategy is inserted *first* (so it filters before
CacheOptimizer marks the final tool), and the whole thing is a no-op when the
feature is disabled.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.config.schema import ToolSearchConfig
from raven.contracts.token_strategy import TokenStrategy
from raven.providers.base import LLMProvider, LLMResponse
from raven.token_wise.registry import StrategyRegistry


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="stub", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


class _MarkerStrategy(TokenStrategy):
    @property
    def name(self) -> str:
        return "marker"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _make_loop(workspace: Path, cfg, strategies=None) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        tool_search_config=cfg,
        strategies=strategies,
        # web_search is the cataloged domain tool these tests fold away, and the
        # loop only registers it when a search key resolves. Supplying one keeps
        # the subject of the test present for the right reason.
        brave_api_key="test-serper-key",
    )


def test_enabled_registers_meta_tools(workspace) -> None:
    loop = _make_loop(workspace, ToolSearchConfig(enabled=True))
    for name in ("tool_search", "tool_call"):
        assert loop.tools.has(name), f"{name} should be registered"
    assert loop.strategies.get("tool_search") is not None


def test_strategy_registered_first(workspace) -> None:
    # A pre-existing strategy must end up *after* tool_search (which is inserted
    # first so it filters tools before any cache-marking strategy runs).
    registry = StrategyRegistry([_MarkerStrategy()])
    loop = _make_loop(workspace, ToolSearchConfig(enabled=True), strategies=registry)
    names = [s.name for s in loop.strategies.strategies]
    assert names[0] == "tool_search", f"tool_search must run first, got {names}"
    assert "marker" in names


def test_disabled_registers_no_search_but_keeps_tool_call(workspace) -> None:
    # tool_call is not part of the feature switch: it is the only route by which
    # a model can name a schema-hidden tool (the DAG controls), and those exist
    # whether or not this deploy folds its catalog.
    loop = _make_loop(workspace, ToolSearchConfig(enabled=False))
    assert not loop.tools.has("tool_search")
    assert loop.strategies.get("tool_search") is None
    assert loop.tools.has("tool_call")


def test_none_config_registers_no_search_but_keeps_tool_call(workspace) -> None:
    loop = _make_loop(workspace, None)
    assert not loop.tools.has("tool_search")
    assert loop.strategies.get("tool_search") is None
    assert loop.tools.has("tool_call")


def test_dag_controls_are_reachable_without_progressive_disclosure(workspace) -> None:
    # The bug this pair of assertions pins: cancel_dag / dag_status /
    # resolve_dag_node are hidden from the schema and advertised in
    # run_subagent_dag's own result text, so an unreachable tool_call left the
    # advertisement pointing at nothing and a suspended node timing out.
    loop = _make_loop(workspace, ToolSearchConfig(enabled=False))
    hidden = loop.tools.schema_hidden_names()
    assert {"cancel_dag", "dag_status", "resolve_dag_node"} <= hidden
    assert loop.dag_control_reachable()


@pytest.mark.asyncio
async def test_enabled_loop_keeps_interaction_primitives_visible(workspace) -> None:
    # Above the threshold the strategy compacts the real loop's tool list: the
    # file/interaction/orchestration primitives (read_file / message / ask_user /
    # spawn) and the meta-tools keep their schema, while a cataloged domain tool
    # (web_search) is withheld and reachable only via tool_search.
    loop = _make_loop(workspace, ToolSearchConfig(enabled=True, compaction_threshold=5))
    assert loop.tools.has("ask_user") and loop.tools.has("spawn") and loop.tools.has("web_search")
    tools = loop.tools.get_definitions()
    _, out, _ = await loop.strategies.before_llm_call([], tools, "stub")
    names = {t["function"]["name"] for t in out}
    assert {"read_file", "message", "ask_user", "spawn"} <= names, "primitives must stay visible"
    assert {"tool_search", "tool_call"} <= names, "meta-tools must stay visible"
    assert "web_search" not in names, "cataloged domain tools are withheld above threshold"


@pytest.mark.asyncio
async def test_tool_call_does_not_name_tool_search_when_it_is_absent(workspace) -> None:
    # The refusal text used to point at tool_search unconditionally, which is the
    # same broken promise -- a route named in a result that this deploy does not
    # ship -- that leaving tool_call behind the feature switch created.
    loop = _make_loop(workspace, ToolSearchConfig(enabled=False))
    out = await loop.tools.execute("tool_call", {"name": "no_such_tool"})
    assert "tool_search" not in out

    # Enabled is not enough: below the threshold the strategy drops tool_search
    # from the request too, so the pointer is only honest above the fold.
    unfolded = _make_loop(workspace, ToolSearchConfig(enabled=True, compaction_threshold=500))
    out = await unfolded.tools.execute("tool_call", {"name": "no_such_tool"})
    assert "tool_search" not in out

    folded = _make_loop(workspace, ToolSearchConfig(enabled=True, compaction_threshold=5))
    out = await folded.tools.execute("tool_call", {"name": "no_such_tool"})
    assert "tool_search lists what is currently loaded" in out
