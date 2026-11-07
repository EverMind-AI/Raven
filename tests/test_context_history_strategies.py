"""The history strategy registry and the factory's use of it.

``context.history.strategy`` picks the builder that fills the history slot. A
name the registry does not know stops the engine from being built at all, the
strategy's declared capabilities reach the assembler, and a strategy that is
not selected is never imported.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from raven.agent.context import ContextBuilder
from raven.config.raven import ContextConfig, HubSourceConfig, MemoryConfig, SkillForgeRouterConfig
from raven.context_engine import build_context_engine
from raven.context_engine import history as strategies
from raven.context_engine.assembler import ContextAssembler
from raven.providers.base import LLMProvider, LLMResponse

REPO = Path(__file__).resolve().parent.parent


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


def test_the_registry_names_both_strategies() -> None:
    assert strategies.registered() == ["compacting", "curator"]
    compacting = strategies.resolve("compacting")
    assert (compacting.owns_compaction, compacting.mints_source_refs) == (True, True)
    curator = strategies.resolve("curator")
    assert (curator.owns_compaction, curator.mints_source_refs) == (True, False)


def test_a_name_is_read_without_case_or_padding() -> None:
    assert strategies.resolve("  Curator ").name == "curator"


@pytest.mark.parametrize("name", ["", "passthrough", "unified", "curat0r", None])
def test_an_unknown_strategy_is_refused_naming_the_ones_there_are(name) -> None:
    with pytest.raises(ValueError) as raised:
        strategies.resolve(name)
    assert "compacting" in str(raised.value) and "curator" in str(raised.value)


def test_the_factory_refuses_to_build_on_an_unknown_strategy(tmp_path: Path) -> None:
    config = ContextConfig.model_validate({"history": {"strategy": "passthrough"}})
    with pytest.raises(ValueError, match="context.history.strategy"):
        _engine(tmp_path, config)


def test_the_curator_strategy_builds_the_engine_it_always_did(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ContextConfig.model_validate({"history": {"strategy": "curator"}}))
    assert [b.name for b in engine._phase_b] == ["curator"]
    assert engine.owns_compaction
    assert not engine.mints_source_refs


def test_the_flat_curator_keys_still_reach_the_curator(tmp_path: Path) -> None:
    config = ContextConfig.model_validate({"history": {"strategy": "curator"}, "fastPathThreshold": 0.33})
    engine = _engine(tmp_path, config)
    assert engine._phase_b[0].config.fast_path_threshold == 0.33


def _imported_after_building(tmp_path: Path, strategy: str) -> set[str]:
    script = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(REPO / "tests")!r})
import test_context_history_strategies as t
from raven.config.raven import ContextConfig
from raven.memory_engine.skill_local.watcher import SkillFileWatcher

# The builder starts a native SKILL.md watcher; one left running at interpreter
# shutdown takes the child down with SIGSEGV after it has printed its answer.
started = []
real_start = SkillFileWatcher.start
SkillFileWatcher.start = lambda self: (started.append(self) or True) if real_start(self) else False
try:
    t._engine(Path({str(tmp_path)!r}), ContextConfig.model_validate({{"history": {{"strategy": {strategy!r}}}}}))
finally:
    for watcher in started:
        watcher.stop()
print(" ".join(m for m in sys.modules if m.startswith("raven.context_engine")))
"""
    out = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    return set(out.stdout.split())


def test_a_strategy_that_is_not_selected_is_never_imported(tmp_path: Path) -> None:
    modules = _imported_after_building(tmp_path, "curator")
    assert "raven.context_engine.segments.curator" in modules
    assert not any(m.startswith("raven.context_engine.history.compacting") for m in modules)


@pytest.mark.asyncio
async def test_the_curator_strategy_assembles_byte_for_byte_what_a_hand_built_curator_engine_does(
    tmp_path: Path,
) -> None:
    from raven.context_engine.segments.curator import CuratorSegmentBuilder
    from raven.contracts.assembled import TokenBudget
    from raven.contracts.context import TurnContext

    config = ContextConfig.model_validate({"history": {"strategy": "curator"}})
    built = _engine(tmp_path / "a", config)
    by_hand_builders = [b for b in built._builders if not b.needs_prefix]
    curator = built._phase_b[0]
    by_hand_builders.append(
        CuratorSegmentBuilder(
            pin_resolver=curator._pin_resolver,
            workspace=tmp_path / "a",
            config=config,
            provider=curator._fallback.provider,
            model=curator._fallback.model,
            context_window_tokens=8192,
            get_tool_definitions=list,
        )
    )
    by_hand = ContextAssembler(by_hand_builders, list, turn_scratch=config.cache.turn_scratch)
    by_hand._now_fn = built._now_fn = lambda: __import__("datetime").datetime(2026, 10, 20, 12, 0)
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "answer"}]
    budget = TokenBudget(
        context_length=8192, reserved_output=1024, reserved_tools=0, reserved_system=0, available_history=6000
    )
    turn = TurnContext(current_message="now")
    one = await built.assemble("k", list(history), budget, turn=turn)
    two = await by_hand.assemble("k", list(history), budget, turn=turn)
    assert one.messages == two.messages


class _Hooked:
    def __init__(self, name: str, answer, *, needs_prefix: bool = True, raises: bool = False) -> None:
        self.name, self.order, self.needs_prefix, self.stable = name, 9, needs_prefix, False
        self._answer, self._raises, self.asked = answer, raises, 0

    async def build(self, ctx):
        return None

    async def on_context_overflow(self, session_key, messages, *, attempt, window):
        self.asked += 1
        if self._raises:
            raise RuntimeError("the strategy broke")
        return self._answer


@pytest.mark.asyncio
async def test_the_first_history_builder_with_an_answer_decides_an_overflow() -> None:
    from raven.contracts.context import OverflowOutcome

    answer = OverflowOutcome(messages=[], reclaimed_tokens=10)
    broken = _Hooked("broken", None, raises=True)
    silent = _Hooked("silent", None)
    first = _Hooked("first", answer)
    later = _Hooked("later", OverflowOutcome(messages=[], reclaimed_tokens=1))
    phase_a = _Hooked("phase_a", answer, needs_prefix=False)
    engine = ContextAssembler([phase_a, broken, silent, first, later], list)

    assert await engine.on_context_overflow("k", [], attempt=1, window=8192) is answer
    assert (broken.asked, silent.asked, first.asked, later.asked, phase_a.asked) == (1, 1, 1, 0, 0)


@pytest.mark.asyncio
async def test_an_assembler_with_no_strategy_hook_leaves_overflow_to_the_loop(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ContextConfig.model_validate({"history": {"strategy": "curator"}}))
    assert await engine.on_context_overflow("k", [{"role": "user", "content": "x"}], attempt=1, window=8192) is None


@pytest.mark.parametrize("name", ["curator", "compacting"])
def test_dropping_the_history_slot_by_either_name_drops_it_and_its_refs(tmp_path: Path, name: str) -> None:
    """``curator`` is what a config written before the registry calls the history
    builder; it still drops it under the default strategy, and an engine with no
    history builder does not claim to mint refs."""
    engine = _engine(tmp_path, ContextConfig.model_validate({"dropSegments": [name]}))
    assert engine._phase_b == []
    assert not engine.mints_source_refs
