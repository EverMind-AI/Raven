"""A live model switch must reach everything holding the old provider.

``config.set key="model"`` builds a fresh provider and hands it to
``AgentLoop.set_provider``. The loop is not the only holder: the subagent
manager, the context engine's LLM-backed segments and the consolidator
each captured the provider they were built with. When the switch stopped
at ``loop.provider``, those three kept calling the abandoned endpoint --
subagent spawns and the skill rewriter/gate failed to authenticate while
the main loop worked fine.

Work already in flight is the other half: every call site reads the
provider from ``self`` at call time, so an unconditional swap would relay
one conversation across two vendors. The loop parks a switch until no
turn is running -- a count, not a flag, because a user turn and a
proactive turn hold slots in separate pools and overlap. A detached
subagent outlives that window entirely, so ``spawn`` snapshots the pair
it was asked for.
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
from raven.memory_engine.skill_forge.gate import LLMGateFilter
from raven.memory_engine.skill_forge.rewriter import QueryRewriter
from raven.providers.base import LLMResponse, ToolCallRequest

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
    loop.provider = "old-provider"
    loop.model = "old-model"
    loop.subagents = _Recorder()
    loop.context_engine = _Recorder()
    loop.memory_consolidator = _Recorder()
    loop._turns_in_flight = 0
    loop._pending_provider = None
    loop._image_tool_result_ok = {"old-model": True}
    refreshed: list[bool] = []
    loop.refresh_context_window = lambda: refreshed.append(True)

    new_provider = SimpleNamespace(name="new-provider")
    loop.set_provider(new_provider, NEW_MODEL)

    assert loop.provider is new_provider
    assert loop.model == NEW_MODEL
    for holder in (loop.subagents, loop.context_engine, loop.memory_consolidator):
        assert holder.provider is new_provider
        assert holder.model == NEW_MODEL
    # Cached per model id but computed from the provider: a swap keeping the
    # model id must not keep serving the old transport's verdict.
    assert loop._image_tool_result_ok == {}
    # The window follows the adopted pair -- re-resolved here rather than at
    # the RPC call site, which a parked switch outlives.
    assert refreshed == [True]


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
    assert skills._gate._provider is new_provider
    assert skills._rewriter._provider is new_provider
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
        assert holder is not None, f"AgentLoop.{attr} is gone; _adopt_provider still calls it"
        assert callable(getattr(holder, "set_provider", None)), f"AgentLoop.{attr} lost set_provider"


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


def test_rewriter_and_gate_adopt_the_new_provider() -> None:
    new_provider = SimpleNamespace(name="new-provider")

    rewriter = QueryRewriter("old-provider")
    rewriter.set_provider(new_provider, NEW_MODEL)
    assert rewriter._provider is new_provider

    gate = LLMGateFilter("old-provider")
    gate.set_provider(new_provider, NEW_MODEL)
    assert gate._provider is new_provider
    # Unset means the gate follows the provider's default model, so the
    # switch must not pin it to the agent's model behind the user's back.
    assert gate._model is None


def test_pinned_gate_model_survives_the_switch() -> None:
    gate = LLMGateFilter("old-provider", model="openai/gpt-5-mini")
    gate.set_provider(SimpleNamespace(name="new-provider"), NEW_MODEL)
    assert gate._model == "openai/gpt-5-mini"


def test_the_default_curator_model_is_a_pin(tmp_path) -> None:
    """``ContextConfig.curator_model`` has a non-empty default, so in practice
    it does not follow the agent model -- at construction or across a switch.
    """
    loop = _loop(tmp_path)
    curator = next(b for b in loop.context_engine._builders if isinstance(b, CuratorSegmentBuilder))
    pinned = curator.curator_model
    assert pinned == ContextConfig().curator_model

    loop.set_provider(_Provider("new"), NEW_MODEL)
    assert curator.curator_model == pinned


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


def test_a_switch_during_a_turn_is_parked(tmp_path) -> None:
    """Every call site reads ``self.provider`` at call time, so adopting
    mid-turn would send the rest of one turn to a different vendor.
    """
    loop = _loop(tmp_path)
    started = _Provider("started-with")
    loop.set_provider(started, "started/model")

    loop._turns_in_flight = 1
    switched = _Provider("switched-to")
    loop.set_provider(switched, NEW_MODEL)

    assert loop.provider is started, "the turn in flight must keep what it started with"
    assert loop.subagents.provider is started
    assert loop._pending_provider == (switched, NEW_MODEL)

    loop._turns_in_flight = 0
    loop._adopt_pending_provider()

    assert loop.provider is switched
    assert loop.subagents.provider is switched
    assert loop._pending_provider is None


def test_a_switch_between_turns_applies_immediately(tmp_path) -> None:
    loop = _loop(tmp_path)
    switched = _Provider("switched-to")
    loop.set_provider(switched, NEW_MODEL)

    assert loop.provider is switched
    assert loop._pending_provider is None


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
async def test_run_turn_adopts_on_entry_and_releases_on_exit(tmp_path) -> None:
    """The wrapper is the whole mechanism, so drive the real one. Asserting on
    ``_turns_in_flight`` alone would pass with the wrapper deleted.
    """
    loop = _loop(tmp_path)
    started = _Provider("started-with")
    loop.set_provider(started, "started/model")

    switched = _Provider("switched-to")
    loop._pending_provider = (switched, NEW_MODEL)

    seen: dict[str, object] = {}

    async def _fake_run_turn(*args, **kwargs):
        seen["provider"] = loop.provider
        seen["depth"] = loop._turns_in_flight
        return "outcome"

    loop._run_turn = _fake_run_turn
    assert await loop.run_turn(None, None, None) == "outcome"

    assert seen["provider"] is switched, "a parked switch must land before the turn reads it"
    assert seen["depth"] == 1
    assert loop._turns_in_flight == 0


@pytest.mark.asyncio
async def test_run_turn_releases_its_slot_when_the_turn_raises(tmp_path) -> None:
    """A turn that fails must not leave the loop looking busy forever -- every
    later switch would be parked and never adopted.
    """
    loop = _loop(tmp_path)

    async def _boom(*args, **kwargs):
        raise RuntimeError("turn failed")

    loop._run_turn = _boom
    with pytest.raises(RuntimeError):
        await loop.run_turn(None, None, None)

    assert loop._turns_in_flight == 0

    switched = _Provider("switched-to")
    loop.set_provider(switched, NEW_MODEL)
    assert loop.provider is switched


@pytest.mark.asyncio
async def test_a_second_turn_does_not_unpark_a_switch_under_the_first(tmp_path) -> None:
    """OriginPools gates USER and system origins on independent semaphores with
    no global cap, so a user turn and a cron turn overlap on one loop. A count
    is what makes the park survive the shorter of the two.
    """
    loop = _loop(tmp_path)
    started = _Provider("started-with")
    loop.set_provider(started, "started/model")

    long_turn_running = asyncio.Event()
    release_long = asyncio.Event()
    switched = _Provider("switched-to")
    during_short: dict[str, object] = {}

    async def _long(*args, **kwargs):
        long_turn_running.set()
        await release_long.wait()
        during_short["provider_at_end_of_long"] = loop.provider
        return "long"

    async def _short(*args, **kwargs):
        during_short["provider_during_short"] = loop.provider
        return "short"

    loop._run_turn = _long
    long_task = asyncio.create_task(loop.run_turn(None, None, None))
    await long_turn_running.wait()

    # The switch lands while only the long turn is running.
    loop.set_provider(switched, NEW_MODEL)
    assert loop.provider is started
    assert loop._pending_provider is not None

    # A second, shorter turn starts and finishes underneath it.
    loop._run_turn = _short
    await loop.run_turn(None, None, None)

    assert during_short["provider_during_short"] is started, "the short turn must not adopt the park"
    assert loop.provider is started, "the long turn is still running"
    assert loop._turns_in_flight == 1

    release_long.set()
    await long_task

    assert loop.provider is switched, "the last turn out adopts it"
    assert loop._pending_provider is None


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
    manager_mod.build_executor = lambda cfg, workspace, owned_ids=None: _StubExecutor()
    try:
        await manager.spawn("do the thing", label="thing", session_key="s")
        manager.set_provider(_RecordingProvider("switched-to"), NEW_MODEL)
        manager._gate.release()
        for _ in range(50):
            await asyncio.sleep(0)
            if served:
                break
    finally:
        manager_mod.build_executor = original_build

    assert served == ["started-with"], "the spawn ran on the provider it was asked for"
