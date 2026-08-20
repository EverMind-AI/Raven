"""A live model switch must reach everything holding the old provider.

``config.set key="model"`` builds a fresh provider and hands it to
``AgentLoop.set_provider``. The loop is not the only holder: the subagent
manager, the context engine's LLM-backed segments and the consolidator
each captured the provider they were built with. When the switch stopped
at ``loop.provider``, those three kept calling the abandoned endpoint --
subagent spawns and the skill rewriter/gate failed to authenticate while
the main loop worked fine.

This file covers the out-of-turn fallback only -- the reference each holder
keeps for work that runs with no turn bound. What a turn actually runs on is
per session and lives in ``test_agent_loop_session_model.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from raven.agent.loop.main import AgentLoop
from raven.config.raven import ContextConfig, SkillForgeConfig
from raven.context_engine.assembler import ContextAssembler
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.context_engine.segments.skills import SkillsSegmentBuilder
from raven.providers.base import LLMResponse
from raven.providers.binding import ModelBinding

NEW_MODEL = "anthropic/claude-opus-4-8"


class _Provider:
    """Minimal provider stand-in; identity is what the assertions track."""

    def __init__(self, name: str = "old") -> None:
        self.name = name

    def get_default_model(self) -> str:
        return "fake/model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok", finish_reason="stop")


class _Recorder:
    """Holder that remembers the provider/model it was last pointed at."""

    def __init__(self) -> None:
        self.provider: object = "old-provider"
        self.model = "old-model"

    def set_provider(self, provider: object, model: str) -> None:
        self.provider = provider
        self.model = model


class _TextOnlyBuilder:
    """A segment that never calls an LLM, so it has no set_provider."""

    name = "identity"
    order = 1
    needs_prefix = False

    async def build(self, ctx):  # pragma: no cover - never invoked here
        return None


def _loop(tmp_path) -> AgentLoop:
    return AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/model",
        context_config=ContextConfig(),
        skill_forge_config=SkillForgeConfig(),
    )


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


def test_set_provider_reaches_every_holder() -> None:
    loop = object.__new__(AgentLoop)
    loop.subagents = _Recorder()
    loop.context_engine = _Recorder()
    loop.memory_consolidator = _Recorder()
    loop._provider_pool = None
    loop._default_binding = ModelBinding(_Provider("old"), "old-model")
    loop._session_bindings = {}
    loop._configured_window = None
    loop._image_tool_result_ok = {}
    loop._vision_ok = {}

    new_provider = _Provider("new-provider")
    loop.set_provider(new_provider, NEW_MODEL)

    assert loop.provider is new_provider
    assert loop.model == NEW_MODEL
    for holder in (loop.subagents, loop.context_engine, loop.memory_consolidator):
        assert holder.provider is new_provider
        assert holder.model == NEW_MODEL


def test_switch_reaches_the_real_holders_a_loop_builds(tmp_path) -> None:
    """The stubbed fan-out above proves the dispatcher; this proves the
    receivers. Every holder here is the class a real run uses, reached by
    walking the engine the factory actually assembled -- so a setter that is
    renamed, dropped, or quietly wrong fails here instead of in production.
    """
    loop = _loop(tmp_path)
    engine = loop.context_engine
    assert isinstance(engine, ContextAssembler)

    skills = next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))
    curator = next(b for b in engine._builders if isinstance(b, CuratorSegmentBuilder))
    assert skills._gate is not None, "llm_gate_enabled defaults True; the gate is a holder"
    assert skills._rewriter is not None

    new_provider = _Provider("new")
    loop.set_provider(new_provider, NEW_MODEL)

    assert loop.provider is new_provider
    assert loop.subagents.provider is new_provider
    assert loop.subagents.model == NEW_MODEL
    assert loop.memory_consolidator.provider is new_provider
    assert skills._gate._fallback_provider is new_provider
    assert skills._rewriter._fallback_provider is new_provider
    assert curator.provider is new_provider
    assert curator.assembler.provider is new_provider
    assert curator.assembler.trimmer.provider is new_provider


def test_fan_out_targets_still_exist_on_a_real_loop(tmp_path) -> None:
    """Guard against silent drift. ``ContextAssembler.set_provider`` walks its
    builders duck-typed and skips anything without the method, so a renamed
    holder or setter would leave the fan-out green while it quietly stops
    covering that subsystem.
    """
    loop = _loop(tmp_path)
    for attr in ("subagents", "context_engine", "memory_consolidator"):
        holder = getattr(loop, attr, None)
        assert holder is not None, f"AgentLoop.{attr} is gone; set_provider still fans out to it"
        assert callable(getattr(holder, "set_provider", None)), f"AgentLoop.{attr} lost set_provider"


# ---------------------------------------------------------------------------
# Capability verdicts cached across a switch
# ---------------------------------------------------------------------------


def test_a_new_binding_forgets_the_old_transport_verdicts(tmp_path) -> None:
    """Both caches key on a model id but are computed from the provider, so a
    rebuild that keeps the id would keep answering with the old endpoint's
    verdict. The reachable case is an ``apiBase`` repointed at a box with
    different capabilities: the id does not move, so nothing else invalidates
    them, and images stay dropped from tool results for the life of the process
    with nothing in the log to say why.
    """
    loop = _loop(tmp_path)
    loop._image_tool_result_ok["custom/my-model"] = False
    loop._vision_ok["custom/my-model"] = False

    loop.set_default_binding(ModelBinding(_Provider("rebuilt"), "custom/my-model"))

    assert loop._image_tool_result_ok == {}
    assert loop._vision_ok == {}


def test_a_session_switch_forgets_them_too(tmp_path) -> None:
    """The session-scoped path rebuilds a provider exactly as the default one
    does, and the cache key does not record which provider answered.
    """
    loop = _loop(tmp_path)
    loop._image_tool_result_ok["custom/my-model"] = False
    loop._vision_ok["custom/my-model"] = False

    loop.set_session_binding("tui:a", ModelBinding(_Provider("rebuilt"), "custom/my-model"))

    assert loop._image_tool_result_ok == {}
    assert loop._vision_ok == {}


def test_a_pair_free_switch_keeps_a_window_the_user_pinned(tmp_path) -> None:
    """``set_provider`` builds the binding itself, so it is the one path that
    can drop a pinned window on the floor.
    """
    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/model",
        context_window_tokens=32768,
        context_config=ContextConfig(),
        skill_forge_config=SkillForgeConfig(),
    )

    loop.set_provider(_Provider("new"), NEW_MODEL)

    assert loop.default_binding.configured_window == 32768
    assert loop.context_window_tokens == 32768


def test_assembler_forwards_to_llm_backed_builders_only() -> None:
    llm_builder = _Recorder()
    llm_builder.name = "skills"
    llm_builder.order = 5
    llm_builder.needs_prefix = False
    text_builder = _TextOnlyBuilder()

    assembler = ContextAssembler([llm_builder, text_builder], lambda: [])
    new_provider = SimpleNamespace(name="new-provider")

    # The text-only builder has no set_provider; walking must skip it rather
    # than blow up, which is why the fan-out is duck-typed.
    assembler.set_provider(new_provider, NEW_MODEL)

    assert llm_builder.provider is new_provider
    assert llm_builder.model == NEW_MODEL
