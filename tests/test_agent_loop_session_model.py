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

from raven.agent.loop.bundles import EngineWiring, TurnPolicy
from raven.agent.loop.main import AgentLoop
from raven.config.raven import ContextConfig, SkillForgeConfig
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.context_engine.segments.skills import SkillsSegmentBuilder
from raven.providers.base import LLMResponse
from raven.providers.binding import ModelBinding, active_binding
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _Provider:
    def __init__(self, name: str, provider_name: str = "vendor") -> None:
        self.name = name
        self.provider_name = provider_name

    def get_default_model(self) -> str:
        return f"{self.name}/default"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok", finish_reason="stop")


class _Resolver:
    """A multi-vendor dispatcher the refresh path must never collapse: like the
    gateway's ResolvingProvider it has no ``provider_name``."""

    def get_default_model(self) -> str:
        return "resolver/default"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok", finish_reason="stop")


def _loop(tmp_path) -> AgentLoop:
    return AgentLoop(
        provider=_Provider("boot"),
        workspace=tmp_path,
        model="boot/model",
        # Push discovery, because that is what builds the skills segment and the
        # gate these tests reach for. Upstream had no such switch and got the
        # segment unconditionally; here the default is pull, which drops it.
        engine=EngineWiring(context_config=ContextConfig(), skill_forge_config=SkillForgeConfig(discovery="push")),
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
        await loop.subagents.spawn("do it", task_summary="it", session_key="tui:a")
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
        provider_pool=ProviderPool(cfg),
        engine=EngineWiring(context_config=context_config, skill_forge_config=SkillForgeConfig()),
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
        provider_pool=ProviderPool(cfg),
        engine=EngineWiring(context_config=context_config, skill_forge_config=SkillForgeConfig()),
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
        provider_pool=ProviderPool(cfg),
        engine=EngineWiring(
            context_config=ContextConfig(),
            skill_forge_config=SkillForgeConfig(
                discovery="push",
                llm_gate_model="claude-haiku-4-5",
                llm_gate_provider="openrouter",
            ),
        ),
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
        provider_pool=ProviderPool(cfg),
        engine=EngineWiring(context_config=ContextConfig(), skill_forge_config=SkillForgeConfig()),
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
        provider_pool=ProviderPool(cfg),
        engine=EngineWiring(context_config=ContextConfig(), skill_forge_config=SkillForgeConfig()),
    )

    loop.restore_session_model("tui:a", "gemini-2.5-flash")

    assert loop.session_model("tui:a") == "boot/model"
    assert not loop.has_session_binding("tui:a")


def _restorable_loop(tmp_path, stored: dict[str, dict[str, str]]) -> AgentLoop:
    """A loop whose session store already holds someone's earlier choice."""
    from raven.config.schema import Config
    from raven.providers.pool import ProviderPool

    cfg = Config()
    cfg.agents.defaults.model = "claude-opus-4-5"
    cfg.providers.anthropic.api_key = "sk-ant"
    loop = AgentLoop(
        provider=_Provider("boot"),
        workspace=tmp_path,
        model="boot/model",
        provider_pool=ProviderPool(cfg),
        engine=EngineWiring(context_config=ContextConfig(), skill_forge_config=SkillForgeConfig()),
    )
    for key, metadata in stored.items():
        record = loop.sessions.get_or_create(key)
        record.metadata.update(metadata)
        loop.sessions.save(record)
    return loop


async def test_a_channel_turn_restores_the_model_that_session_chose(tmp_path) -> None:
    """The reason the read moved onto the loop. It used to hang off
    ``session.resume``, so only the TUI got it: a conversation arriving from a
    channel came back on the default after a restart, with its own choice
    sitting unread in its own record. No resume call anywhere in this test.
    """
    loop = _restorable_loop(tmp_path, {"whatsapp:alice": {"model": "claude-sonnet-4-5", "provider": "anthropic"}})

    seen: list[str] = []

    async def body(*a, **k):
        seen.append(loop.model)
        return None

    await _run(loop, "whatsapp:alice", body)

    assert seen == ["claude-sonnet-4-5"]


def test_the_stored_model_is_read_once_per_session(tmp_path) -> None:
    """Most sessions never switched, and the miss has to be remembered too --
    otherwise every turn of every unswitched conversation re-reads a record to
    learn the same nothing.
    """
    loop = _restorable_loop(tmp_path, {"tui:a": {"model": "claude-sonnet-4-5", "provider": "anthropic"}})
    reads: list[str] = []
    real_peek = loop.sessions.peek

    def counting_peek(key: str):
        reads.append(key)
        return real_peek(key)

    loop.sessions.peek = counting_peek

    for _ in range(3):
        loop.session_model("tui:a")
        loop.session_model("tui:never-switched")

    assert reads == ["tui:a", "tui:never-switched"]


def test_a_restored_session_stays_on_the_default_once_cleared(tmp_path) -> None:
    """What makes clearing stick is that the read happens once per key.

    End to end, not a guard on one line: the key is marked both where the record
    is read and where a caller supplies the pair, so removing either alone still
    leaves this green. ``test_the_stored_model_is_read_once_per_session`` pins
    the marking in ``_restore_once``; the redundant one in
    ``restore_session_model`` is unpinned, and deleting it passes the suite.

    Named for what it does show, and not for a guarantee
    ``clear_session_binding`` does not give: it deliberately does not mark, so a
    session cleared without ever having been read would be read afterwards. No
    caller creates one -- ``session.delete`` unlinks the record first -- which is
    why the marking lives in the read rather than in the clear.
    """
    loop = _restorable_loop(tmp_path, {"tui:a": {"model": "claude-sonnet-4-5", "provider": "anthropic"}})

    assert loop.session_model("tui:a") == "claude-sonnet-4-5"
    loop.clear_session_binding("tui:a")

    assert loop.session_model("tui:a") == "boot/model"
    assert not loop.has_session_binding("tui:a")


def test_has_session_binding_sees_a_choice_that_only_exists_on_disk(tmp_path) -> None:
    """``model.options`` asks this to decide whether to report the session's own
    model or the configured default. Answered without the restore, a session
    that switched before a restart is reported as having inherited the default,
    and the picker stars the wrong row.
    """
    loop = _restorable_loop(tmp_path, {"tui:a": {"model": "claude-sonnet-4-5", "provider": "anthropic"}})

    assert loop.has_session_binding("tui:a")
    assert not loop.has_session_binding("tui:b")


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
        provider_pool=ProviderPool(cfg),
        engine=EngineWiring(
            context_config=ContextConfig(),
            skill_forge_config=SkillForgeConfig(discovery="push", llm_gate_model="openai/gpt-5-mini"),
        ),
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
    manager_mod.build_executor = lambda *args, **kwargs: _StubExecutor()
    try:

        async def _body(*args, **kwargs):
            await loop.subagents.spawn("do it", task_summary="it", session_key="tui:a")
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


@pytest.mark.asyncio
async def test_a_switched_turns_request_says_its_marks_are_already_placed(tmp_path) -> None:
    """CacheOptimizer's budget must bind the provider of every turn it runs on.

    The construction site can only configure the provider it was handed. A
    session switched mid-conversation runs on a pool-built provider that site
    never saw, and if nothing tells that provider the breakpoints are already
    there, the request carries the strategy's plus the provider's own --
    reproduced on a live wire as 2 sent under ``maxCacheBreakpoints=1``.

    Asserted on the request rather than on the sender. This used to be a switch
    set on the provider object, which bound the right turn but never came back
    off, so every later consumer of that same object -- the Curator, a subagent,
    Sentinel, the session titler -- kept sending with no breakpoints at all. The
    stamp travels with the request, so it binds this turn and nothing else.
    """
    from raven.providers import prompt_cache
    from raven.providers.base import LLMProvider
    from raven.providers.binding import use_binding
    from raven.token_wise.cache_optimizer import CacheOptimizer
    from raven.token_wise.registry import StrategyRegistry

    class _SendingProvider(LLMProvider):
        """Records what the request said on its way out."""

        def __init__(self, model: str) -> None:
            super().__init__(api_key="test")
            self._model = model
            self.saw_marks_placed: bool | None = None

        async def chat(self, messages, tools=None, model=None, **kwargs):
            self.saw_marks_placed = prompt_cache.marks_already_placed(messages)
            return LLMResponse(content="ok", finish_reason="stop")

        def get_default_model(self) -> str:
            return self._model

    def _agent(provider: LLMProvider, registry: StrategyRegistry) -> AgentLoop:
        return AgentLoop(
            provider=provider,
            workspace=tmp_path,
            model=provider.get_default_model(),
            policy=TurnPolicy(max_iterations=2),
            engine=EngineWiring(strategies=registry),
        )

    with_optimizer = StrategyRegistry([CacheOptimizer(supports_caching=lambda _model: True)])

    built_with = _SendingProvider("boot/model")
    switched_to = _SendingProvider("switched/model")
    loop = _agent(built_with, with_optimizer)
    with use_binding(ModelBinding(switched_to, "switched/model")):
        await loop._process_message(_req("tui:a"), session_key="tui:a")
    assert switched_to.saw_marks_placed is True, "the sender this turn ran on was told"

    # Without the optimizer nothing else places breakpoints, so the provider's
    # own marking is all the caching there is -- the request must not claim
    # otherwise.
    plain = _SendingProvider("switched/model")
    loop2 = _agent(_SendingProvider("boot/model"), StrategyRegistry([]))
    with use_binding(ModelBinding(plain, "switched/model")):
        await loop2._process_message(_req("tui:b"), session_key="tui:b")
    assert plain.saw_marks_placed is False, "no optimizer, no claim, provider still marks"


# ---------------------------------------------------------------------------
# Credentials are re-asked from the pool on every ask
# ---------------------------------------------------------------------------


class _Pool:
    """Answers ``bind`` from a mapping, the way the real pool answers from its
    fingerprint-keyed cache."""

    def __init__(self) -> None:
        self.bindings: dict[str, ModelBinding] = {}
        self.raise_for: set[str] = set()

    def bind(self, model: str, provider_name: str | None = None) -> ModelBinding:
        if model in self.raise_for:
            raise RuntimeError("credential removed")
        return self.bindings[model]

    def bind_pin(self, model: str | None, provider_name: str | None = None) -> ModelBinding | None:
        return None


def _pooled_loop(tmp_path) -> tuple[AgentLoop, _Pool]:
    pool = _Pool()
    loop = AgentLoop(
        provider=_Provider("boot"),
        workspace=tmp_path,
        model="boot/model",
        provider_pool=pool,
        engine=EngineWiring(context_config=ContextConfig(), skill_forge_config=SkillForgeConfig(discovery="push")),
    )
    pool.bindings["boot/model"] = loop.default_binding
    return loop, pool


def test_an_edited_credential_serves_the_next_ask(tmp_path) -> None:
    """The pool drops its cache when the credentials fingerprint changes; this
    is the caller-side half -- without it the session keeps the stale provider
    until a restart."""
    loop, pool = _pooled_loop(tmp_path)
    old = _binding("prov-old", "vendor-a/model")
    loop.set_session_binding("tui:a", old)
    pool.bindings["vendor-a/model"] = old
    assert loop.binding_for_session("tui:a") is old

    fresh = _binding("prov-rotated", "vendor-a/model")
    pool.bindings["vendor-a/model"] = fresh

    assert loop.binding_for_session("tui:a") is fresh
    assert loop.binding_for_session("tui:a") is fresh, "the stored override follows the rebind"


def test_a_removed_credential_keeps_the_current_binding(tmp_path) -> None:
    """A pair that can no longer be built keeps the binding it has -- a turn on
    the provider that answered a second ago beats no turn at all."""
    loop, pool = _pooled_loop(tmp_path)
    old = _binding("prov-old", "vendor-a/model")
    loop.set_session_binding("tui:a", old)
    pool.raise_for.add("vendor-a/model")

    assert loop.binding_for_session("tui:a") is old


def test_the_default_lane_rebinds_too(tmp_path) -> None:
    loop, pool = _pooled_loop(tmp_path)
    fresh = _binding("prov-rotated", "boot/model")
    pool.bindings["boot/model"] = fresh

    assert loop.binding_for_session("tui:new") is fresh


def test_a_rebind_that_moves_the_provider_forgets_transport_verdicts(tmp_path) -> None:
    """The verdict caches are keyed by model but computed from the provider, so
    a provider rebuilt under the same model id must not keep serving the old
    transport's answer."""
    loop, pool = _pooled_loop(tmp_path)
    old = _binding("prov-old", "vendor-a/model")
    loop.set_session_binding("tui:a", old)
    pool.bindings["vendor-a/model"] = old
    loop._vision_ok["vendor-a/model"] = False
    assert loop.binding_for_session("tui:a") is old
    assert loop._vision_ok, "an identity rebind keeps the verdicts"

    pool.bindings["vendor-a/model"] = _binding("prov-rotated", "vendor-a/model")
    loop.binding_for_session("tui:a")

    assert not loop._vision_ok


def test_repeated_default_asks_keep_the_transport_verdicts(tmp_path) -> None:
    """The default lane adopts the pool's binding object once; compared against
    a default that never moved, every later ask would read as a provider change
    and thrash the verdict caches."""
    loop, pool = _pooled_loop(tmp_path)
    adopted = _binding("prov-pool", "boot/model")
    pool.bindings["boot/model"] = adopted

    loop.binding_for_session("tui:x")
    assert loop.default_binding is adopted, "the pool's object becomes the default, through the setter"
    loop._vision_ok["boot/model"] = True

    loop.binding_for_session("tui:y")

    assert loop._vision_ok, "an unchanged pool answer must not clear the verdict caches"


def test_a_provider_that_cannot_name_its_vendor_is_never_re_paired(tmp_path) -> None:
    """The gateway default is a ResolvingProvider -- a multi-vendor dispatcher
    with no provider_name. Re-asking the pool for it would derive a vendor from
    the model id and replace the resolver with one direct provider, moving the
    bill to whichever vendor the prefix happens to name."""

    class _DerivingPool(_Pool):
        """Answers every bind with a direct single-vendor binding, the way the
        real pool derives a vendor from the model prefix when handed None."""

        def __init__(self) -> None:
            super().__init__()
            self.asked: list[str] = []

        def bind(self, model: str, provider_name: str | None = None):
            self.asked.append(model)
            return _binding("prov-direct", model)

    pool = _DerivingPool()
    loop = AgentLoop(
        provider=_Resolver(),
        workspace=tmp_path,
        model="anthropic/some-model",
        provider_pool=pool,
        engine=EngineWiring(context_config=ContextConfig(), skill_forge_config=SkillForgeConfig(discovery="push")),
    )

    binding = loop.binding_for_session("tui:a")

    assert binding is loop.default_binding
    assert binding.provider.__class__ is _Resolver, "the resolver must not collapse to one vendor"
    assert pool.asked == [], "an unnamed provider is not the pool's to re-pair"
