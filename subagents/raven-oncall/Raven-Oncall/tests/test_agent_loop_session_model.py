"""The model is per conversation, and one conversation's switch is its own.

Five rules, in the order a user would state them:

1. different sessions can be on different models;
2. switching one session does not move another;
3. a new session starts on the configured default, not on whatever the last
   session switched to;
4. a subsystem with a model *and credentials* of its own uses them; without
   both it follows the model of the conversation it is running under;
5. a switch that arrives while a turn is running takes effect on the next
   turn, not in the middle of this one.

Rule 5 is not a mechanism here, it is a consequence: ``run_turn`` resolves the
session's binding once and holds it in a context var for the whole turn tree,
so a switch landing mid-turn is simply not visible to that turn. The same
context copy is what makes a detached subagent finish on the model it was
spawned under.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.agent.loop.main import AgentLoop
from raven.config.raven import ContextConfig, SkillForgeConfig
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.context_engine.segments.skills import SkillsSegmentBuilder
from raven.providers.base import LLMResponse
from raven.providers.binding import ModelBinding, active_binding
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name

    def get_default_model(self) -> str:
        return f"{self.name}/default"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok", finish_reason="stop")


def _loop(tmp_path) -> AgentLoop:
    return AgentLoop(
        provider=_Provider("boot"),
        workspace=tmp_path,
        model="boot/model",
        context_config=ContextConfig(),
        skill_forge_config=SkillForgeConfig(),
    )


def _req(session_key: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
        text="hi",
        conversation=session_key,
    )


def _binding(name: str, model: str) -> ModelBinding:
    return ModelBinding(_Provider(name), model)


async def _run(loop: AgentLoop, session_key: str, body) -> object:
    loop._run_turn = body
    return await loop.run_turn(_req(session_key), None, None)


# ---------------------------------------------------------------------------
# 1 + 2 + 3: scope
# ---------------------------------------------------------------------------


def test_a_session_without_a_switch_is_on_the_default(tmp_path) -> None:
    loop = _loop(tmp_path)
    assert loop.session_model("tui:a") == "boot/model"
    assert loop.binding_for_session("tui:a") is loop.default_binding


def test_two_sessions_can_be_on_two_models(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))
    loop.set_session_binding("tui:b", _binding("prov-b", "vendor-b/model"))

    assert loop.session_model("tui:a") == "vendor-a/model"
    assert loop.session_model("tui:b") == "vendor-b/model"


def test_switching_one_session_leaves_the_others_alone(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))

    assert loop.session_model("tui:b") == "boot/model", "an untouched session stays on the default"
    assert loop.default_binding.model == "boot/model", "a session switch is not a default change"


def test_a_new_session_starts_on_the_default_not_the_last_switch(tmp_path) -> None:
    """Rule 3. A session-scoped switch is deliberately not sticky: the next
    session created reads the configured default again.
    """
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))

    assert loop.session_model("tui:fresh") == "boot/model"


def test_changing_the_default_moves_only_sessions_that_never_switched(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:pinned", _binding("prov-a", "vendor-a/model"))

    loop.set_default_binding(_binding("prov-new", "vendor-new/model"))

    assert loop.session_model("tui:pinned") == "vendor-a/model"
    assert loop.session_model("tui:drifting") == "vendor-new/model"


def test_dropping_a_session_override_returns_it_to_the_default(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))
    loop.clear_session_binding("tui:a")

    assert loop.session_model("tui:a") == "boot/model"


# ---------------------------------------------------------------------------
# The turn boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_runs_on_its_own_session_model(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))

    seen: dict[str, object] = {}

    async def _body(*args, **kwargs):
        # Everything under the turn reads the same pair, including the holders
        # that used to keep a reference of their own.
        seen["loop"] = (loop.provider.name, loop.model)
        seen["subagents"] = (loop.subagents.provider.name, loop.subagents.model)
        seen["consolidator"] = loop.memory_consolidator.provider.name
        return "done"

    assert await _run(loop, "tui:a", _body) == "done"
    assert seen["loop"] == ("prov-a", "vendor-a/model")
    assert seen["subagents"] == ("prov-a", "vendor-a/model")
    assert seen["consolidator"] == "prov-a"


@pytest.mark.asyncio
async def test_outside_a_turn_the_loop_reports_the_default(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))

    assert active_binding() is None
    assert loop.model == "boot/model"


@pytest.mark.asyncio
async def test_two_concurrent_turns_each_keep_their_own_model(tmp_path) -> None:
    """Rules 1 and 2 have to hold while both turns are in flight, which is the
    case a shared provider could never express: a user turn and a cron turn run
    at the same time on this loop.
    """
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))
    loop.set_session_binding("cron:job", _binding("prov-cron", "vendor-cron/model"))

    a_started = asyncio.Event()
    release_a = asyncio.Event()
    seen: dict[str, str] = {}

    async def _body(req, *args, **kwargs):
        key = req.conversation
        if key == "tui:a":
            a_started.set()
            await release_a.wait()
        seen[key] = loop.model
        return key

    loop._run_turn = _body
    a = asyncio.create_task(loop.run_turn(_req("tui:a"), None, None))
    await a_started.wait()
    await loop.run_turn(_req("cron:job"), None, None)
    release_a.set()
    await a

    assert seen["cron:job"] == "vendor-cron/model"
    assert seen["tui:a"] == "vendor-a/model", "the cron turn must not have moved the user turn"


@pytest.mark.asyncio
async def test_a_switch_mid_turn_lands_on_the_next_turn(tmp_path) -> None:
    """Rule 5, with no parking involved: the running turn holds the binding it
    entered on, so the switch is invisible to it and current for the next one.
    """
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-old", "vendor-old/model"))

    started = asyncio.Event()
    release = asyncio.Event()
    during: list[str] = []

    async def _body(req, *args, **kwargs):
        started.set()
        await release.wait()
        during.append(loop.model)
        return "done"

    loop._run_turn = _body
    running = asyncio.create_task(loop.run_turn(_req("tui:a"), None, None))
    await started.wait()

    loop.set_session_binding("tui:a", _binding("prov-new", "vendor-new/model"))
    release.set()
    await running

    assert during == ["vendor-old/model"], "the turn in flight must not move"

    after: list[str] = []

    async def _next(req, *args, **kwargs):
        after.append(loop.model)
        return "done"

    await _run(loop, "tui:a", _next)
    assert after == ["vendor-new/model"]


@pytest.mark.asyncio
async def test_the_turn_binding_is_released_when_the_turn_raises(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))

    async def _boom(*args, **kwargs):
        raise RuntimeError("turn failed")

    loop._run_turn = _boom
    with pytest.raises(RuntimeError):
        await loop.run_turn(_req("tui:a"), None, None)

    assert active_binding() is None
    assert loop.model == "boot/model"


# ---------------------------------------------------------------------------
# Rule 4: subsystems
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_spawned_subagent_keeps_its_conversations_model(tmp_path) -> None:
    """A subagent has no model of its own, so it follows the conversation that
    spawned it -- and keeps doing so after that conversation switches, because
    it is a detached task that outlives the turn.
    """
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))

    captured: dict[str, object] = {}

    async def _capture(task_id, task, label, origin, provider, model):
        captured["pair"] = (provider.name, model)

    loop.subagents._run_subagent = _capture
    loop.subagents._gate = asyncio.Semaphore(0)

    async def _body(*args, **kwargs):
        await loop.subagents.spawn("do it", label="it", session_key="tui:a")
        return "done"

    await _run(loop, "tui:a", _body)
    loop.set_session_binding("tui:a", _binding("prov-new", "vendor-new/model"))
    await asyncio.sleep(0)

    assert captured["pair"] == ("prov-a", "vendor-a/model")


@pytest.mark.asyncio
async def test_an_unconfigured_subsystem_follows_the_conversation(tmp_path) -> None:
    """The gate and the curator are unpinned here (no credentials for the
    default ``curator_model``), so both read the turn's model.
    """
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))
    skills = next(b for b in loop.context_engine._builders if isinstance(b, SkillsSegmentBuilder))
    curator = next(b for b in loop.context_engine._builders if isinstance(b, CuratorSegmentBuilder))

    seen: dict[str, object] = {}

    async def _body(*args, **kwargs):
        seen["gate"] = skills._gate._binding()[1]
        seen["curator"] = curator.curator_model
        seen["rewriter"] = skills._rewriter._call_provider().name
        return "done"

    await _run(loop, "tui:a", _body)
    assert seen["gate"] == "vendor-a/model"
    assert seen["curator"] == "vendor-a/model"
    assert seen["rewriter"] == "prov-a"


@pytest.mark.asyncio
async def test_a_configured_subsystem_uses_its_own_pair(tmp_path) -> None:
    """With a model *and* a credential of its own, a subsystem stops following
    the conversation -- that is the whole point of configuring one.
    """
    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))
    skills = next(b for b in loop.context_engine._builders if isinstance(b, SkillsSegmentBuilder))
    curator = next(b for b in loop.context_engine._builders if isinstance(b, CuratorSegmentBuilder))

    pin = _binding("prov-pin", "vendor-pin/small")
    skills._gate._pin = pin
    curator._pin = pin

    seen: dict[str, object] = {}

    async def _body(*args, **kwargs):
        seen["gate"] = skills._gate._binding()[1]
        seen["curator"] = curator.curator_model
        return "done"

    await _run(loop, "tui:a", _body)
    assert seen["gate"] == "vendor-pin/small"
    assert seen["curator"] == "vendor-pin/small"


def test_the_gate_sends_no_model_outside_a_turn(tmp_path) -> None:
    """An unpaired pin must never go out on the fallback provider's key. With
    no turn bound there is nothing to follow, so the gate asks the provider for
    its own default rather than posting a model id it has no credential for.
    """
    loop = _loop(tmp_path)
    skills = next(b for b in loop.context_engine._builders if isinstance(b, SkillsSegmentBuilder))
    skills._gate._model = "openai/gpt-5-mini"
    skills._gate._pin = None

    assert active_binding() is None
    assert skills._gate._binding()[1] is None


@pytest.mark.asyncio
async def test_a_request_without_a_conversation_falls_back_to_its_channel_key(tmp_path) -> None:
    """Non-TUI channels and cron arrive with no ``conversation``; the
    ``channel:chat_id`` key is the only one they get, so a switch stored under
    it has to be the one their turn runs on.
    """
    loop = _loop(tmp_path)
    loop.set_session_binding("whatsapp:12345", _binding("prov-wa", "vendor-wa/model"))

    seen: list[str] = []

    async def _body(*args, **kwargs):
        seen.append(loop.model)
        return "done"

    loop._run_turn = _body
    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="whatsapp", chat_id="12345", sender_id="u", chat_type=ChatType.DM),
        text="hi",
    )
    await loop.run_turn(req, None, None)

    assert seen == ["vendor-wa/model"]


def test_a_configured_pin_survives_a_real_factory_build(tmp_path) -> None:
    """The pin only becomes a pair when a pool is wired, and every entry point
    must wire one -- without it a correctly credentialed ``curator_model`` is
    silently ignored and the user is told it has no credentials.
    """
    from raven.config.schema import Config
    from raven.providers.pool import ProviderPool

    cfg = Config()
    cfg.agents.defaults.model = "claude-opus-4-5"
    cfg.agents.defaults.provider = "auto"
    cfg.providers.anthropic.api_key = "sk-ant"
    cfg.providers.gemini.api_key = "AIza"

    context_config = ContextConfig(curator_model="gemini-2.5-flash")
    loop = AgentLoop(
        provider=_Provider("boot"),
        workspace=tmp_path,
        model="boot/model",
        context_config=context_config,
        skill_forge_config=SkillForgeConfig(),
        provider_pool=ProviderPool(cfg),
    )
    curator = next(b for b in loop.context_engine._builders if isinstance(b, CuratorSegmentBuilder))

    assert curator._pin is not None, "a credentialed curator_model must become a pair"
    assert curator.curator_model == "gemini-2.5-flash"
    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))
    assert curator.curator_model == "gemini-2.5-flash", "a configured subsystem does not follow the turn"


def test_the_factory_hands_the_pool_the_pin_the_user_configured(tmp_path) -> None:
    """Both halves of the configured pair have to reach the pool.

    A gateway serving another vendor's model is exactly the case the id cannot
    express: derived from ``claude-haiku-4-5`` the vendor is Anthropic, and the
    curator would run on the Anthropic key while the user asked for the
    gateway. Dropping the provider argument at the factory leaves the pin
    looking configured and pointed at the wrong bill.
    """
    from raven.config.schema import Config
    from raven.providers.pool import ProviderPool

    cfg = Config()
    cfg.agents.defaults.model = "claude-opus-4-5"
    cfg.agents.defaults.provider = "auto"
    cfg.providers.anthropic.api_key = "sk-ant"
    cfg.providers.openrouter.api_key = "sk-or"

    context_config = ContextConfig(curator_model="claude-haiku-4-5", curator_provider="openrouter")
    loop = AgentLoop(
        provider=_Provider("boot"),
        workspace=tmp_path,
        model="boot/model",
        context_config=context_config,
        skill_forge_config=SkillForgeConfig(),
        provider_pool=ProviderPool(cfg),
    )
    curator = next(b for b in loop.context_engine._builders if isinstance(b, CuratorSegmentBuilder))

    assert curator._pin is not None
    assert curator._pin.provider.api_key == "sk-or", "the configured provider serves the pin"


def test_the_factory_hands_the_pool_the_gate_pin_the_user_configured(tmp_path) -> None:
    """Same wiring, the other pin."""
    from raven.config.schema import Config
    from raven.context_engine.segments.skills import SkillsSegmentBuilder
    from raven.providers.pool import ProviderPool

    cfg = Config()
    cfg.agents.defaults.model = "claude-opus-4-5"
    cfg.agents.defaults.provider = "auto"
    cfg.providers.anthropic.api_key = "sk-ant"
    cfg.providers.openrouter.api_key = "sk-or"

    loop = AgentLoop(
        provider=_Provider("boot"),
        workspace=tmp_path,
        model="boot/model",
        context_config=ContextConfig(),
        skill_forge_config=SkillForgeConfig(
            llm_gate_model="claude-haiku-4-5",
            llm_gate_provider="openrouter",
        ),
        provider_pool=ProviderPool(cfg),
    )
    skills = next(b for b in loop.context_engine._builders if isinstance(b, SkillsSegmentBuilder))

    assert skills._gate is not None
    assert skills._gate._pin is not None
    assert skills._gate._pin.provider.api_key == "sk-or"


def test_a_stored_model_is_restored_onto_a_resumed_session(tmp_path) -> None:
    """The write half is useless without this read half. A switch has to
    survive a restart, or the user's choice lasts exactly as long as the
    process -- and the persistence looks like it works while doing nothing.
    """
    from raven.config.schema import Config
    from raven.providers.pool import ProviderPool

    cfg = Config()
    cfg.agents.defaults.model = "claude-opus-4-5"
    cfg.agents.defaults.provider = "auto"
    cfg.providers.anthropic.api_key = "sk-ant"
    loop = AgentLoop(
        provider=_Provider("boot"),
        workspace=tmp_path,
        model="boot/model",
        context_config=ContextConfig(),
        skill_forge_config=SkillForgeConfig(),
        provider_pool=ProviderPool(cfg),
    )

    assert loop.session_model("tui:a") == "boot/model", "a fresh process has no override"

    loop.restore_session_model("tui:a", "claude-sonnet-4-5")

    assert loop.session_model("tui:a") == "claude-sonnet-4-5"
    assert loop.has_session_binding("tui:a")


def test_a_stored_model_that_cannot_be_built_leaves_the_default(tmp_path) -> None:
    """A credential removed since the switch must not fail the resume."""
    from raven.config.schema import Config
    from raven.providers.pool import ProviderPool

    cfg = Config()
    cfg.agents.defaults.model = "claude-opus-4-5"
    loop = AgentLoop(
        provider=_Provider("boot"),
        workspace=tmp_path,
        model="boot/model",
        context_config=ContextConfig(),
        skill_forge_config=SkillForgeConfig(),
        provider_pool=ProviderPool(cfg),
    )

    loop.restore_session_model("tui:a", "gemini-2.5-flash")

    assert loop.session_model("tui:a") == "boot/model"
    assert not loop.has_session_binding("tui:a")


def test_has_session_binding_distinguishes_chosen_from_inherited(tmp_path) -> None:
    """``session_model`` falls back to the default, so it cannot answer this --
    and callers that override a forced provider need the difference.
    """
    loop = _loop(tmp_path)
    assert not loop.has_session_binding("tui:a")

    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))

    assert loop.has_session_binding("tui:a")
    assert not loop.has_session_binding("tui:b")


def test_a_configured_gate_pin_survives_a_real_factory_build(tmp_path) -> None:
    """The gate's pin is built in the same factory as the curator's but was
    uncovered -- and a gate failure is swallowed by its top-N fallback, so
    losing the pin degrades silently by design.
    """
    from raven.config.schema import Config
    from raven.providers.pool import ProviderPool

    cfg = Config()
    cfg.agents.defaults.model = "claude-opus-4-5"
    cfg.agents.defaults.provider = "auto"
    cfg.providers.anthropic.api_key = "sk-ant"
    cfg.providers.openai.api_key = "sk-openai"

    loop = AgentLoop(
        provider=_Provider("boot"),
        workspace=tmp_path,
        model="boot/model",
        context_config=ContextConfig(),
        skill_forge_config=SkillForgeConfig(llm_gate_model="openai/gpt-5-mini"),
        provider_pool=ProviderPool(cfg),
    )
    skills = next(b for b in loop.context_engine._builders if isinstance(b, SkillsSegmentBuilder))

    assert skills._gate is not None
    assert skills._gate._pin is not None, "a credentialed llm_gate_model must become a pair"
    assert skills._gate._binding()[1] == "openai/gpt-5-mini"

    loop.set_session_binding("tui:a", _binding("prov-a", "vendor-a/model"))
    assert skills._gate._binding()[1] == "openai/gpt-5-mini", "a configured subsystem does not follow the turn"


@pytest.mark.asyncio
async def test_a_spawn_holds_its_binding_through_the_gate_and_the_sandbox_boot(tmp_path) -> None:
    """The binding is taken in ``spawn``, not where the task starts running: a
    spawn waits on the concurrency gate and a sandbox boot first, and a switch
    landing in that window would hand it an endpoint chosen after it was asked
    for. Driven through the real ``_run_subagent`` so the window is genuinely
    open -- stubbing it would prove only that ``spawn`` passes a pair.
    """
    import raven.agent.subagent.manager as manager_mod
    from raven.providers.base import LLMResponse as _Resp

    served: list[str] = []

    class _Recording(_Provider):
        async def chat_with_retry(self, **kwargs) -> _Resp:
            served.append(self.name)
            return _Resp(content="done", finish_reason="stop")

    class _StubExecutor:
        @property
        def is_sandboxed(self) -> bool:
            return False

        async def exec(self, command: str, **kwargs):  # pragma: no cover - unused
            raise NotImplementedError

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    loop = _loop(tmp_path)
    loop.set_session_binding("tui:a", ModelBinding(_Recording("started-with"), "started/model"))
    loop.subagents._submit = lambda *a, **k: None
    loop.subagents._gate = asyncio.Semaphore(0)

    original_build = manager_mod.build_executor
    manager_mod.build_executor = lambda cfg, workspace, owned_ids=None: _StubExecutor()
    try:

        async def _body(*args, **kwargs):
            await loop.subagents.spawn("do it", label="it", session_key="tui:a")
            return "done"

        await _run(loop, "tui:a", _body)
        loop.set_session_binding("tui:a", ModelBinding(_Recording("switched-to"), "switched/model"))
        loop.subagents._gate.release()
        for _ in range(50):
            await asyncio.sleep(0)
            if served:
                break
    finally:
        manager_mod.build_executor = original_build

    assert served == ["started-with"], "the spawn ran on the model its conversation had when it asked"
