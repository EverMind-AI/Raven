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

import asyncio
from types import SimpleNamespace

import pytest

from raven.agent.loop.main import AgentLoop
from raven.agent.subagent.manager import SubagentManager
from raven.config.raven import ContextConfig, SkillForgeConfig
from raven.context_engine.assembler import ContextAssembler
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.context_engine.segments.skills import SkillsSegmentBuilder
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.memory_engine.skill_forge.gate import LLMGateFilter
from raven.providers.binding import ModelBinding

NEW_MODEL = "anthropic/claude-opus-4-8"


class _StubExecutor:
    """``_run_subagent_inner`` only passes this to ExecTool; no command runs."""

    @property
    def is_sandboxed(self) -> bool:
        return False

    async def exec(self, command: str, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _noop_submit(*args, **kwargs) -> None:
    """``_announce_result`` calls the spine submit without awaiting it."""
    return None


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
        # This test walks the push pipeline's holders (rewriter/gate); the
        # pull default builds neither.
        skill_forge_config=SkillForgeConfig(discovery="push"),
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


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def test_pinned_gate_model_survives_the_switch() -> None:
    gate = LLMGateFilter("old-provider", model="openai/gpt-5-mini")
    gate.set_provider(SimpleNamespace(name="new-provider"), NEW_MODEL)
    assert gate._model == "openai/gpt-5-mini"


def test_an_empty_curator_model_follows_the_agent_model_both_times(tmp_path) -> None:
    """The field has no ``min_length``, so ``curator_model: ""`` validates and
    the constructor's ``or model`` follows the agent model. A switch has to
    follow it too, or the same config means one thing at build time and
    another after.
    """
    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/model",
        context_config=ContextConfig(curator_model=""),
        skill_forge_config=SkillForgeConfig(),
    )
    curator = next(b for b in loop.context_engine._builders if isinstance(b, CuratorSegmentBuilder))
    assert curator.curator_model == "fake/model"

    loop.set_provider(_Provider("new"), NEW_MODEL)
    assert curator.curator_model == NEW_MODEL


# ---------------------------------------------------------------------------
# In-flight work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_running_subagent_keeps_the_provider_it_started_with(tmp_path) -> None:
    """A subagent is a detached task that outlives the turn that spawned it,
    so the loop's park cannot cover it -- ``spawn`` snapshots instead. Without
    that, iteration k+1 calls the new vendor carrying k iterations of the old
    vendor's message shapes.
    """
    seen: list[tuple[str, str]] = []
    release = asyncio.Event()

    class _TwoStepProvider(_Provider):
        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            seen.append((self.name, kwargs.get("model")))
            if len(seen) == 1:
                # Hold the task open across the switch, then ask for one more
                # iteration so a re-read of self.provider would show up.
                await release.wait()
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
                    finish_reason="tool_calls",
                )
            return LLMResponse(content="done", finish_reason="stop")

    manager = SubagentManager(
        provider=_TwoStepProvider("started-with"),
        workspace=tmp_path,
        model="started/model",
    )
    manager._submit = _noop_submit

    task = asyncio.create_task(
        manager._run_subagent_inner(
            "t1",
            "do the thing",
            "thing",
            {"channel": "cli", "chat_id": "direct", "session_key": "s"},
            _StubExecutor(),
            manager.provider,
            manager.model,
        )
    )
    await asyncio.sleep(0)
    manager.set_provider(_TwoStepProvider("switched-to"), NEW_MODEL)
    release.set()
    await task

    assert [name for name, _ in seen] == ["started-with", "started-with"]
    assert [model for _, model in seen] == ["started/model", "started/model"]
    # The next spawn does get the new one -- the snapshot is per task, not a freeze.
    assert manager.provider.name == "switched-to"
    assert manager.model == NEW_MODEL


@pytest.mark.asyncio
async def test_spawn_snapshots_before_the_task_queues(tmp_path) -> None:
    """The snapshot must be taken in ``spawn``, not where the task starts
    running: a spawn waits on the concurrency gate and a sandbox boot first,
    and a switch landing in that window would hand the task an endpoint the
    user chose after asking for it.

    Driven through the real ``_run_subagent`` so the window is genuinely
    open -- stubbing it would prove only that ``spawn`` passes *a* pair, which
    a snapshot taken later would also satisfy.
    """
    import raven.agent.subagent.manager as manager_mod

    served: list[str] = []

    class _RecordingProvider(_Provider):
        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            served.append(self.name)
            return LLMResponse(content="done", finish_reason="stop")

    manager = SubagentManager(
        provider=_RecordingProvider("started-with"),
        workspace=tmp_path,
        model="started/model",
    )
    manager._submit = _noop_submit
    # Hold every spawn in exactly the window the snapshot exists for.
    manager._gate = asyncio.Semaphore(0)

    original_build = manager_mod.build_executor
    manager_mod.build_executor = lambda *a, **k: _StubExecutor()
    try:
        await manager.spawn("do the thing", task_summary="thing", session_key="s")
        manager.set_provider(_RecordingProvider("switched-to"), NEW_MODEL)
        manager._gate.release()
        for _ in range(50):
            await asyncio.sleep(0)
            if served:
                break
    finally:
        manager_mod.build_executor = original_build

    assert served == ["started-with"], "the spawn ran on the provider it was asked for"
