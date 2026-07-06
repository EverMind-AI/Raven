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
from raven.providers.base import LLMProvider, LLMResponse
from raven.token_wise.base import TokenStrategy
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
    )


def test_enabled_registers_meta_tools(workspace) -> None:
    loop = _make_loop(workspace, ToolSearchConfig(enabled=True))
    for name in ("tool_search", "tool_describe", "tool_call"):
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


def test_disabled_registers_nothing(workspace) -> None:
    loop = _make_loop(workspace, ToolSearchConfig(enabled=False))
    for name in ("tool_search", "tool_describe", "tool_call"):
        assert not loop.tools.has(name)
    assert loop.strategies.get("tool_search") is None


def test_none_config_registers_nothing(workspace) -> None:
    loop = _make_loop(workspace, None)
    assert not loop.tools.has("tool_search")
    assert loop.strategies.get("tool_search") is None
