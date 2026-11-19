"""The history strategy registry and the factory's use of it.

``context.history.strategy`` picks the builder that fills the history slot. A
name the registry does not know stops the engine from being built at all, and
the strategy's declared capabilities reach the assembler.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.context import ContextBuilder
from raven.config.raven import ContextConfig, HubSourceConfig, MemoryConfig, SkillForgeRouterConfig
from raven.context_engine import build_context_engine
from raven.context_engine import history as strategies
from raven.context_engine.assembler import ContextAssembler
from raven.providers.base import LLMProvider, LLMResponse


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content="", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


def _engine(tmp_path: Path, config: ContextConfig) -> ContextAssembler:
    return build_context_engine(
        workspace=tmp_path,
        config=config,
        builder=ContextBuilder(workspace=tmp_path),
        provider=_StubProvider(),
        model="stub",
        context_window_tokens=8192,
        get_tool_definitions=list,
        memory_config=MemoryConfig(),
        skill_forge_router_config=SkillForgeRouterConfig(hub=HubSourceConfig(endpoint=None)),
    )


def test_the_registry_names_the_compacting_strategy() -> None:
    assert strategies.registered() == ["compacting"]
    compacting = strategies.resolve("compacting")
    assert (compacting.owns_compaction, compacting.mints_source_refs) == (True, True)


def test_a_name_is_read_without_case_or_padding() -> None:
    assert strategies.resolve("  Compacting ").name == "compacting"


@pytest.mark.parametrize("name", ["", "curator", "passthrough", "unified", "compact", None])
def test_an_unknown_strategy_is_refused_naming_the_one_there_is(name) -> None:
    with pytest.raises(ValueError) as raised:
        strategies.resolve(name)
    assert "compacting" in str(raised.value)


def test_the_factory_refuses_to_build_on_an_unknown_strategy(tmp_path: Path) -> None:
    config = ContextConfig.model_validate({"history": {"strategy": "passthrough"}})
    with pytest.raises(ValueError, match="context.history.strategy"):
        _engine(tmp_path, config)


def test_the_default_strategy_builds_the_compacting_builder(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ContextConfig())
    assert [b.name for b in engine._phase_b] == ["compacting"]
    assert engine.owns_compaction
    assert engine.mints_source_refs
