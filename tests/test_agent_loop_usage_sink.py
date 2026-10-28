"""Tests for the usage_sink populated by AgentLoop and surfaced to the TUI.

Pins the wire shape that ``turn.send`` relays as ``message.complete.payload.usage``:
per-turn token counts plus the live context-window gauge (used / max / percent)
and the provider-reported cost. Before this, only the token counts were populated, so the
TUI context bar stayed frozen at 0% and never showed cost.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import raven.agent.loop.turn_path as agent_loop_main
import raven.rpc.methods.session as session_methods
from raven.agent.harness.action import DefaultAction
from raven.agent.hook import AgentHook, HookDecision
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, HostWiring, ToolWiring, TurnPolicy
from raven.agent.loop.recovery import RecoveryLimits
from raven.config.loader import load_config
from raven.config.raven import ContextConfig
from raven.contracts.llm_provider import ToolCallRequest
from raven.contracts.token_strategy import UsageSnapshot
from raven.contracts.tool import Tool
from raven.providers import rates, usage_record
from raven.providers.base import ErrorClassification, LLMProvider, LLMResponse
from raven.providers.binding import ModelBinding, use_binding
from raven.session.manager import SessionManager
from raven.spine.message import ChatType, Source
from raven.spine.turn import AnswerlessTurnError, Origin, TurnRequest
from raven.token_wise import usage_context
from raven.token_wise.registry import StrategyRegistry
from raven.token_wise.usage_tracker import UsageTracker

# The real fetch, captured before conftest's autouse guard stubs it to {}.
_REAL_FETCH = rates._fetch_openrouter_models


class UsageProvider(LLMProvider):
    """Returns a fixed reply with a known usage snapshot. No tool calls."""

    def __init__(self, model: str, prompt_tokens: int, completion_tokens: int, extra: dict | None = None):
        super().__init__(api_key="test")
        self._model = model
        self._usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            **(extra or {}),
        }

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
        return LLMResponse(content="ok", finish_reason="stop", usage=self._usage)

    def get_default_model(self) -> str:
        return self._model


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture(autouse=True)
def _reset_openrouter_cache():
    rates.reset_openrouter_cache()
    yield
    rates.reset_openrouter_cache()


def _make_agent(
    workspace: Path,
    provider: LLMProvider,
    model: str,
    window: int | None,
    *,
    strategies: StrategyRegistry | None = None,
    context_config: ContextConfig | None = None,
    hooks: list[AgentHook] | None = None,
) -> AgentLoop:
    kwargs: dict = {}
    if window is not None or strategies is not None or context_config is not None:
        kwargs["engine"] = EngineWiring(
            context_window_tokens=window,
            strategies=strategies,
            context_config=context_config,
        )
    if hooks is not None:
        kwargs["host"] = HostWiring(hooks=hooks)
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model=model,
        **kwargs,
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )


@pytest.mark.asyncio
async def test_usage_sink_carries_context_gauge_and_cost(workspace):
    """A non-openrouter model fills used/percent against the configured window."""
    provider = UsageProvider("stub", prompt_tokens=6000, completion_tokens=2000)
    agent = _make_agent(workspace, provider, model="stub", window=40000)
    sink: dict = {}

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
        usage_sink=sink,
    )

    assert sink["context_max"] == 40000
    assert sink["context_used"] == 8000
    assert sink["context_percent"] == 20
    assert sink["cost_usd"] is None
    assert sink["cost_missing_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "used", "percent"),
    [
        ({"cache_read_input_tokens": 30000, "total_tokens": 38000, "prompt_tokens_include_cache": False}, 38000, 95),
        ({"cache_read_input_tokens": 30000}, 8000, 20),
    ],
    ids=["cache_reported_apart_from_prompt_tokens", "cache_already_inside_prompt_tokens"],
)
async def test_the_gauge_measures_the_window_not_the_bill(workspace, extra, used, percent):
    """A warm cache bills a fraction of the prompt and occupies all of it.

    The gauge divides by the context window, so it must count the cached prompt
    the provider reports apart from ``prompt_tokens`` -- and must not count it
    twice where ``prompt_tokens`` already contains it, which is why the
    add-back reads the provider's own ``prompt_tokens_include_cache``. The
    billed counts on the wire are untouched either way.
    """
    provider = UsageProvider("stub", prompt_tokens=6000, completion_tokens=2000, extra=extra)
    agent = _make_agent(workspace, provider, model="stub", window=40000)
    sink: dict = {}

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
        usage_sink=sink,
    )

    assert sink["context_used"] == used
    assert sink["context_percent"] == percent
    assert sink["prompt_tokens"] == 6000
    assert sink["completion_tokens"] == 2000


def _patch_live_openrouter_window(monkeypatch, window: int) -> None:
    """Route the OpenRouter models fetch to report ``window`` for deepseek-v4-pro."""
    models = [
        {
            "id": "deepseek/deepseek-v4-pro",
            "context_length": window,
            "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
        }
    ]

    def handler(_req):
        return httpx.Response(200, content=json.dumps({"data": models}))

    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(rates, "_fetch_openrouter_models", _REAL_FETCH)
    monkeypatch.setattr(rates.httpx, "Client", client_factory)
    monkeypatch.setattr(rates, "_OPENROUTER_CACHE_TIME", 0.0)


@pytest.mark.asyncio
async def test_usage_sink_context_max_from_live_openrouter(workspace, monkeypatch):
    """Without an explicit window, an OpenRouter model LiteLLM lags on gets its real window from /models."""
    _patch_live_openrouter_window(monkeypatch, 163840)

    provider = UsageProvider("openrouter/deepseek/deepseek-v4-pro", 1000, 500)
    agent = _make_agent(
        workspace,
        provider,
        model="openrouter/deepseek/deepseek-v4-pro",
        window=None,
    )
    sink: dict = {}

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
        usage_sink=sink,
    )

    assert sink["context_max"] == 163840
    assert sink["context_used"] == 1500


@pytest.mark.asyncio
async def test_usage_sink_context_max_stays_explicit_over_live_openrouter(workspace, monkeypatch):
    """An explicitly configured window wins even when the model's live window disagrees."""
    _patch_live_openrouter_window(monkeypatch, 163840)

    provider = UsageProvider("openrouter/deepseek/deepseek-v4-pro", 1000, 500)
    agent = _make_agent(
        workspace,
        provider,
        model="openrouter/deepseek/deepseek-v4-pro",
        window=8192,
    )
    sink: dict = {}

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
        usage_sink=sink,
    )

    assert sink["context_max"] == 8192
    assert sink["context_used"] == 1500


@pytest.mark.asyncio
async def test_usage_sink_context_max_is_resolved_off_the_event_loop_thread(workspace, monkeypatch):
    """SF10: this per-call tier defaults to ``allow_fetch=True``, so a cold
    OpenRouter model with both caches expired can reach for a synchronous
    ~10s HTTP call. ``_run_agent_loop`` runs on the event loop, so that call
    must run on a worker thread, not inline."""
    seen: dict[str, threading.Thread] = {}

    def fake_resolve(model: str) -> int:
        seen["thread"] = threading.current_thread()
        return 99_999

    monkeypatch.setattr(agent_loop_main, "resolve_context_window", fake_resolve)

    provider = UsageProvider("stub", 1000, 500)
    agent = _make_agent(workspace, provider, model="stub", window=None)
    sink: dict = {}

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
        usage_sink=sink,
    )

    assert sink["context_max"] == 99_999
    assert seen["thread"] is not threading.current_thread()


# --------------------------------------------------------------------------- #
# construction-time ladder: _context_window_explicit + refresh_context_window   #
# --------------------------------------------------------------------------- #


def test_no_configured_window_resolves_via_the_ladder_and_is_not_explicit(workspace):
    """An unresolvable model falls back to the documented default, not explicit."""
    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=None)

    assert agent.default_binding.configured_window is None
    assert agent.context_window_tokens == rates.DEFAULT_CONTEXT_WINDOW_TOKENS


def test_a_configured_window_is_explicit_at_construction(workspace):
    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=8192)

    assert agent.default_binding.configured_window == 8192
    assert agent.context_window_tokens == 8192


def test_a_pinned_window_outranks_the_model_a_session_switched_to(workspace, monkeypatch):
    """A pin is a deliberate override; the model a session runs on cannot discard it."""
    _patch_live_openrouter_window(monkeypatch, 163840)

    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=8192)

    switched = ModelBinding(provider, "openrouter/deepseek/deepseek-v4-pro", agent.default_binding.configured_window)
    with use_binding(switched):
        assert agent.context_window_tokens == 8192


def test_the_window_follows_the_model_the_turn_is_bound_to(workspace, monkeypatch):
    """Without an explicit pin, the window is whatever the turn's own model holds.

    The switch runs inside the running event loop, so the ladder is walked with
    ``allow_fetch=False`` -- an in-process cache entry of any age answers rather
    than a live fetch. Populated directly rather than through a mocked network
    call, which ``allow_fetch=False`` never reaches.
    """
    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=None)
    assert agent.context_window_tokens == rates.DEFAULT_CONTEXT_WINDOW_TOKENS

    # conftest's autouse guard stubs the fetch to a zero-argument lambda; restore
    # the real one so allow_fetch=False's in-process-cache branch actually runs.
    monkeypatch.setattr(rates, "_fetch_openrouter_models", _REAL_FETCH)
    rates._OPENROUTER_CACHE["deepseek/deepseek-v4-pro"] = {
        "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
        "context_length": 163840,
    }
    monkeypatch.setattr(rates, "_OPENROUTER_CACHE_TIME", time.time() - rates._OPENROUTER_CACHE_TTL - 1000)
    switched = ModelBinding(provider, "openrouter/deepseek/deepseek-v4-pro")
    with use_binding(switched):
        assert agent.context_window_tokens == 163840


# --------------------------------------------------------------------------- #
# allow_fetch=False: construction and refresh never touch the network        #
# --------------------------------------------------------------------------- #


def _forbid_network_client(monkeypatch):
    """Restore the real fetch, then count real ``httpx.Client`` builds.

    Not a raise: ``_fetch_openrouter_models`` wraps the fetch in
    ``except Exception`` to degrade on a network failure, so a raise from here
    would be swallowed as "the network failed" and the test would pass for the
    wrong reason. A counter the caller asserts is 0 actually distinguishes
    "never touched the network" from "touched it and degraded".
    """
    counter = {"calls": 0}
    real_client = rates.httpx.Client

    def _counting_client(*args, **kwargs):
        counter["calls"] += 1
        return real_client(*args, **kwargs)

    monkeypatch.setattr(rates, "_fetch_openrouter_models", _REAL_FETCH)
    monkeypatch.setattr(rates.httpx, "Client", _counting_client)
    return counter


def test_construction_on_an_openrouter_model_never_touches_the_network(workspace, monkeypatch, tmp_path):
    """The regression this fixes: constructing on an unmapped OpenRouter model
    used to fetch synchronously, blocking startup for up to 10s."""
    from raven.providers import model_catalog_cache

    monkeypatch.setattr(model_catalog_cache, "_CACHE_PATH", tmp_path / "model-catalog.json", raising=False)
    counter = _forbid_network_client(monkeypatch)

    provider = UsageProvider("openrouter/deepseek/deepseek-v4-pro", 0, 0)
    agent = _make_agent(workspace, provider, model="openrouter/deepseek/deepseek-v4-pro", window=None)

    assert agent.context_window_tokens == rates.DEFAULT_CONTEXT_WINDOW_TOKENS
    assert counter["calls"] == 0


def test_reading_the_window_for_an_openrouter_model_never_touches_the_network(workspace, monkeypatch, tmp_path):
    """The window is read inside a running turn, so resolving it must not freeze
    the event loop on a synchronous fetch."""
    from raven.providers import model_catalog_cache

    monkeypatch.setattr(model_catalog_cache, "_CACHE_PATH", tmp_path / "model-catalog.json", raising=False)

    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=None)

    counter = _forbid_network_client(monkeypatch)
    switched = ModelBinding(provider, "openrouter/deepseek/deepseek-v4-pro")
    with use_binding(switched):
        assert agent.context_window_tokens == rates.DEFAULT_CONTEXT_WINDOW_TOKENS
    assert counter["calls"] == 0


class EffortProvider(UsageProvider):
    """Records the effort each call was sent at."""

    def __init__(self, model: str):
        super().__init__(model, 10, 5)
        self.efforts: list = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):  # noqa: E501
        self.efforts.append(reasoning_effort)
        return await super().chat(messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice)


def _point_config(tmp_path: Path, monkeypatch, payload: dict) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)


@pytest.mark.asyncio
async def test_a_call_runs_at_the_configured_effort(workspace, monkeypatch):
    """`agents.defaults.reasoningEffort` reaches the wire.

    The provider holds a configured default of its own, but it is frozen when
    the provider is constructed -- deliberately, so a credentials refresh
    cannot import a live `agents` section -- so an edit reached it only at a
    restart. The loop sends it explicitly instead.
    """
    _point_config(workspace, monkeypatch, {"agents": {"defaults": {"reasoningEffort": "high"}}})
    provider = EffortProvider("stub")
    agent = _make_agent(workspace, provider, model="stub", window=None)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert provider.efforts == ["high"]


@pytest.mark.asyncio
async def test_no_configured_effort_sends_none(workspace, monkeypatch):
    """Nothing passed, so the provider's own default stands. Sending an
    explicit None would switch that default off instead."""
    _point_config(workspace, monkeypatch, {})
    provider = EffortProvider("stub")
    agent = _make_agent(workspace, provider, model="stub", window=None)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert provider.efforts == [None]


# --------------------------------------------------------------------------- #
# the cost is the turn's: every iteration, and every call it delegated          #
# --------------------------------------------------------------------------- #


def _usage(cost: float | None) -> dict:
    usage: dict = {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}
    if cost is not None:
        usage["cost_usd"] = cost
    return usage


def _says(text: str, cost: float | None) -> LLMResponse:
    return LLMResponse(content=text, finish_reason="stop", usage=_usage(cost))


def _asks_for(tool: str, cost: float | None, **arguments) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="c1", name=tool, arguments=arguments)],
        usage=_usage(cost),
    )


class ScriptedProvider(LLMProvider):
    """One scripted reply per call; the last entry answers any call after it."""

    def __init__(self, model: str, script: list[LLMResponse]):
        super().__init__(api_key="test")
        self._model = model
        self._script = script
        self.calls = 0

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
        reply = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return reply

    def get_default_model(self) -> str:
        return self._model


class DelegatingTool(Tool):
    """Spends the way a dispatched sub-agent does: in a session of its own,
    naming the delegating turn's session as its root (``usage_owner``, which
    is what the ACP prompt carries over and the sub-agent's turn binds)."""

    def __init__(self, tracker: UsageTracker, root: str, cost: float | None):
        self._tracker = tracker
        self._root = root
        self._cost = cost

    @property
    def name(self) -> str:
        return "delegate"

    @property
    def description(self) -> str:
        return "Hand the task to a sub-agent."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **_kwargs) -> str:
        with usage_context.bind("acp:child", {"root_session_key": self._root}):
            await self._tracker.after_llm_call({}, UsageSnapshot(model="child/model", cost_usd=self._cost))
        return "the sub-agent answered"


def _costing_agent(
    workspace: Path,
    script: list[LLMResponse],
    *,
    strategies: StrategyRegistry | None = None,
    hooks: list[AgentHook] | None = None,
) -> AgentLoop:
    """An agent whose every billed call is the loop's own.

    The Curator is dropped because its Slow Path is a bounded agent loop of its
    own on the same provider, and a scripted provider cannot tell its calls from
    the turn's. Its calls, like the watch-work judgement's, reach the usage
    recorder only through the provider seam (``raven.providers.usage_record``),
    which a test here installs only when the seam is what it pins.
    """
    return _make_agent(
        workspace,
        ScriptedProvider("stub", script),
        model="stub",
        window=40000,
        strategies=strategies,
        context_config=ContextConfig(drop_segments=["curator"]),
        hooks=hooks,
    )


async def _turn(agent: AgentLoop, sink: dict, session_key: str = "s1") -> None:
    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key=session_key,
        usage_sink=sink,
    )


@pytest.mark.asyncio
async def test_a_turn_costs_every_iteration_it_ran(workspace):
    """Two model calls, two prices, one turn: the bar shows their sum.

    The sink used to carry the final iteration's cost, so a turn that called a
    tool and then answered reported only the answer.
    """
    agent = _costing_agent(workspace, [_asks_for("list_dir", 0.002, path="."), _says("done", 0.003)])
    sink: dict = {}

    await _turn(agent, sink)

    assert sink["cost_usd"] == pytest.approx(0.005), "the tool-calling iteration, plus the one that answered"
    assert sink["cost_missing_calls"] == 0


@pytest.mark.asyncio
async def test_a_delegating_turn_costs_what_it_delegated(workspace):
    """The sub-agent's spend is the turn's spend.

    A sub-agent bills in its own session, so nothing on the delegating turn's
    own calls can see it; it names this turn's session as its root, and the
    recorder every billed call passes bills it here as well.
    """
    tracker = UsageTracker(persist=False)
    agent = _costing_agent(
        workspace,
        [_asks_for("delegate", 0.002), _says("done", 0.003)],
        strategies=StrategyRegistry([tracker]),
    )
    agent.tools.register(DelegatingTool(tracker, root="s1", cost=0.02))
    sink: dict = {}

    await _turn(agent, sink)

    assert sink["cost_usd"] == pytest.approx(0.025), "0.002 + 0.003 of its own, plus the 0.02 it delegated"
    assert sink["cost_missing_calls"] == 0


@pytest.mark.asyncio
async def test_a_turn_is_billed_once_while_the_provider_seam_listens_too(workspace):
    """Production installs the loop's own registry at the provider seam as well
    as binding it for the turn, so the seam and the loop hear the same call. The
    seam's row is the one kept, under the turn's session, and the loop adds
    none."""
    tracker = UsageTracker(persist=False)
    registry = StrategyRegistry([tracker])
    usage_record.install(registry.after_llm_call)
    agent = _costing_agent(
        workspace,
        [_asks_for("list_dir", 0.002, path="."), _says("done", 0.003)],
        strategies=registry,
    )
    sink: dict = {}

    await _turn(agent, sink)

    assert agent.provider.calls == 2
    billed = tracker.snapshot("s1")
    assert billed.calls == 2, "one row per model call, not one from the loop and one from the seam"
    assert billed.cost_usd == pytest.approx(0.005)
    assert tracker.total.calls == 2
    assert sink["cost_usd"] == pytest.approx(0.005)


@pytest.mark.asyncio
async def test_a_serving_generation_is_billed_alone_after_a_candidate_installs(workspace):
    """A candidate generation's assembly installs its registry process-wide
    before it is known to be usable, and the generation it would replace goes
    on serving. That generation's turn is billed to its own registry only: the
    seam used to record it into the candidate's, and the loop, no longer
    matching the installed sink, recorded it again into its own."""
    serving_tracker = UsageTracker(persist=False)
    serving = StrategyRegistry([serving_tracker])
    candidate_tracker = UsageTracker(persist=False)
    usage_record.install(StrategyRegistry([candidate_tracker]).after_llm_call)
    agent = _costing_agent(
        workspace,
        [_asks_for("list_dir", 0.002, path="."), _says("done", 0.003)],
        strategies=serving,
    )
    sink: dict = {}

    await _turn(agent, sink)

    assert agent.provider.calls == 2
    assert serving_tracker.total.calls == 2, "each call once, to the generation that made it"
    assert candidate_tracker.total.calls == 0, "the candidate billed nothing it did not make"


class _BestOfTwoAction(DefaultAction):
    """An Action that asks twice and keeps the second answer -- the shape
    ``ActionRequest`` names (best-of-n, a critic pass): one decision, two
    requests, one response handed back to the loop."""

    async def decide(self, request):
        await super().decide(request)
        return await super().decide(request)


@pytest.mark.asyncio
async def test_every_call_a_multi_call_action_makes_is_billed(workspace):
    """The loop records the one response ``decide`` returns. A claim over the
    whole decision would bill that one and lose the other request; the seam
    records both, under the turn's session, and the loop adds nothing."""
    tracker = UsageTracker(persist=False)
    registry = StrategyRegistry([tracker])
    usage_record.install(registry.after_llm_call)
    agent = _costing_agent(workspace, [_says("draft", 0.002), _says("final", 0.003)], strategies=registry)
    agent.harness = replace(agent.harness, action=_BestOfTwoAction())
    sink: dict = {}

    await _turn(agent, sink)

    assert agent.provider.calls == 2
    billed = tracker.snapshot("s1")
    assert billed.calls == 2, "both requests of the one decision, each once"
    assert billed.cost_usd == pytest.approx(0.005)
    assert tracker.total.calls == 2


@pytest.mark.asyncio
async def test_a_turn_that_ends_unpriced_still_reports_what_it_knows(workspace):
    """An unpriced call is unknown cost, not free, and it does not erase the
    price of the calls beside it -- theirs is a sum, its own is a count."""
    agent = _costing_agent(workspace, [_asks_for("list_dir", 0.002, path="."), _says("done", None)])
    sink: dict = {}

    await _turn(agent, sink)

    assert sink["cost_usd"] == pytest.approx(0.002)
    assert sink["cost_missing_calls"] == 1


@pytest.mark.asyncio
async def test_a_delegation_that_reports_no_price_is_counted_not_dropped(workspace):
    """The sub-agent ran on a model with no published price: the turn says how
    many calls it cannot price rather than billing them as zero."""
    tracker = UsageTracker(persist=False)
    agent = _costing_agent(
        workspace,
        [_asks_for("delegate", 0.002), _says("done", 0.003)],
        strategies=StrategyRegistry([tracker]),
    )
    agent.tools.register(DelegatingTool(tracker, root="s1", cost=None))
    sink: dict = {}

    await _turn(agent, sink)

    assert sink["cost_usd"] == pytest.approx(0.005)
    assert sink["cost_missing_calls"] == 1


@pytest.mark.asyncio
async def test_a_turn_is_not_billed_for_a_delegation_of_another_session(workspace):
    """The scope is found by root session key, so a sub-agent working for a
    turn in another conversation bills that one and not this one."""
    tracker = UsageTracker(persist=False)
    agent = _costing_agent(
        workspace,
        [_asks_for("delegate", 0.002), _says("done", 0.003)],
        strategies=StrategyRegistry([tracker]),
    )
    agent.tools.register(DelegatingTool(tracker, root="another-session", cost=0.02))
    sink: dict = {}

    await _turn(agent, sink)

    assert sink["cost_usd"] == pytest.approx(0.005)


# --------------------------------------------------------------------------- #
# the reading outlives the turn: stored on the reply, read back on a resume     #
# --------------------------------------------------------------------------- #


def _measured(prompt_tokens: int, completion_tokens: int, **extra) -> dict:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        **extra,
    }


async def _resumed_usage(workspace: Path, monkeypatch, session_key: str) -> dict:
    cfg = load_config()
    cfg.agents.defaults.workspace = str(workspace)
    cfg.agents.defaults.context_window_tokens = 40000
    monkeypatch.setattr(session_methods, "load_config", lambda: cfg)
    result = await session_methods.session_resume({"session_id": session_key})
    return result["info"]["usage"]


def _stored_replies(workspace: Path, session_key: str) -> list[dict]:
    session = SessionManager(workspace).peek(session_key)
    assert session is not None, "the turn was not persisted"
    return [m for m in session.messages if m.get("role") == "assistant"]


@pytest.mark.asyncio
async def test_each_reply_is_stored_with_the_window_its_call_measured(workspace):
    """The gauge's reading is kept on the reply its call produced.

    Live, the reading reaches the meter through the stream caller's sink and is
    gone once ``message.complete`` is sent. A reload has nothing to redraw it
    from but the stored log, which is not what the model was sent: the context
    engine compacts on its own. So every reply carries the reading of the call
    that wrote it -- the tool-calling one as well as the answer -- with no sink
    asked for, since a conversation held on a channel is opened in the browser
    too.
    """
    agent = _costing_agent(
        workspace,
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
                usage=_measured(6000, 200),
            ),
            LLMResponse(content="done", finish_reason="stop", usage=_measured(6500, 300)),
        ],
    )

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="tui:c1",
    )

    replies = _stored_replies(workspace, "tui:c1")
    assert [r.get("context_used") for r in replies] == [6200, 6800]
    assert all("_context_used" not in r for r in replies), f"the in-flight private key was stored: {replies}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "used"),
    [
        ({"cache_read_input_tokens": 30000, "prompt_tokens_include_cache": False}, 38000),
        ({"cache_read_input_tokens": 30000}, 8000),
    ],
    ids=["cache_reported_apart_from_prompt_tokens", "cache_already_inside_prompt_tokens"],
)
async def test_a_resumed_session_shows_the_reading_the_live_turn_showed(workspace, monkeypatch, extra, used):
    """The meter must not move when the page reloads or the reader switches back."""
    agent = _costing_agent(
        workspace, [LLMResponse(content="ok", finish_reason="stop", usage=_measured(6000, 2000, **extra))]
    )
    sink: dict = {}
    await _turn(agent, sink, session_key="tui:c1")

    usage = await _resumed_usage(workspace, monkeypatch, "tui:c1")

    assert sink["context_used"] == used
    assert (usage["context_used"], usage["context_percent"]) == (sink["context_used"], sink["context_percent"])
    assert usage["context_estimated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [{}, _measured(0, 0)], ids=["no_usage_reported", "zero_tokens_reported"])
async def test_a_reply_whose_call_reported_no_usage_carries_no_reading(workspace, usage):
    """Absent means unknown, so a resume reads past it to an earlier reading
    rather than drawing an empty window."""
    agent = _costing_agent(workspace, [LLMResponse(content="ok", finish_reason="stop", usage=usage)])

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="tui:c1",
    )

    (reply,) = _stored_replies(workspace, "tui:c1")
    assert "context_used" not in reply and "_context_used" not in reply, reply


class _ReviseTheAnswer(AgentHook):
    async def after_iteration(self, ctx):
        if ctx.response is not None and not ctx.response.has_tool_calls:
            return HookDecision(short_circuit_result="reviewed answer")
        return HookDecision()


class _RefuseTheTools(AgentHook):
    async def before_execute_tools(self, ctx):
        return HookDecision(short_circuit_result="tools refused")


class _EndAfterTheTools(AgentHook):
    async def after_iteration(self, ctx):
        if ctx.response is not None and ctx.response.has_tool_calls:
            return HookDecision(short_circuit_result="stopped after the tools")
        return HookDecision()


_CALLS_A_TOOL = LLMResponse(
    content="",
    tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
    usage=_measured(6000, 2000),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hook", "reply", "put_in_place"),
    [
        (
            _ReviseTheAnswer(),
            LLMResponse(content="first draft", finish_reason="stop", usage=_measured(6000, 2000)),
            "reviewed answer",
        ),
        (_RefuseTheTools(), _CALLS_A_TOOL, "tools refused"),
        (_EndAfterTheTools(), _CALLS_A_TOOL, "stopped after the tools"),
    ],
    ids=[
        "after_iteration_replaces_the_answer",
        "before_execute_tools_replaces_a_tool_call",
        "after_iteration_ends_after_the_tools",
    ],
)
async def test_a_reply_a_hook_put_in_place_of_the_calls_keeps_that_calls_reading(
    workspace, monkeypatch, hook, reply, put_in_place
):
    """A hook's text stands in for what the model said, not for the call: the
    live meter already showed that call's window, so a resume must show it too.
    After a tool call, the call's own row carries the reading, so the text a
    hook ends the turn with there needs none."""
    agent = _costing_agent(workspace, [reply], hooks=[hook])
    sink: dict = {}
    await _turn(agent, sink, session_key="tui:c1")

    usage = await _resumed_usage(workspace, monkeypatch, "tui:c1")

    assert put_in_place in [r.get("content") for r in _stored_replies(workspace, "tui:c1")], "the hook's text was filed"
    assert sink["context_used"] == 8000
    assert (usage["context_used"], usage["context_percent"]) == (sink["context_used"], sink["context_percent"])
    assert usage["context_estimated"] is False


_ANSWERS_THROUGH_THE_MESSAGE_TOOL = LLMResponse(
    content="",
    tool_calls=[ToolCallRequest(id="c1", name="message", arguments={"content": "here it is"})],
    usage=_measured(6000, 200),
)


def _says_nothing(usage: dict) -> LLMResponse:
    return LLMResponse(content="", finish_reason="stop", usage=usage)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limits", "empty_replies", "live"),
    [
        (
            RecoveryLimits(llm_error_retry_delays=()),
            [_says_nothing(_measured(7000, 0)), _says_nothing(_measured(7400, 0))],
            7400,
        ),
        (RecoveryLimits(enabled=False), [_says_nothing(_measured(7000, 0))], 7000),
        (RecoveryLimits(llm_error_retry_delays=()), [_says_nothing(_measured(7000, 0)), _says_nothing({})], 7000),
    ],
    ids=["recovery_spent", "recovery_off", "last_empty_reply_unreported"],
)
async def test_a_turn_that_ends_on_an_empty_reply_keeps_that_calls_reading(
    workspace, monkeypatch, limits, empty_replies, live
):
    """The model answered through the message tool and then said nothing more,
    so the turn completes on a call whose reply is not filed. The live meter
    showed the last window reported, which ends at the newest row the turn
    kept, so a resume must show it too: recovery's scaffolding is dropped, and
    a call that reported nothing leaves the reading before it standing."""
    agent = AgentLoop(
        provider=ScriptedProvider("stub", [_ANSWERS_THROUGH_THE_MESSAGE_TOOL, *empty_replies]),
        workspace=workspace,
        model="stub",
        engine=EngineWiring(context_window_tokens=40000, context_config=ContextConfig(drop_segments=["curator"])),
        policy=TurnPolicy(max_iterations=6, empty_recovery=limits),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    async def _delivered(_content: str, _media: list[str]) -> None:
        return None

    agent.tools.get("message").set_send_callback(_delivered)
    sink: dict = {}
    await _turn(agent, sink, session_key="tui:c1")

    usage = await _resumed_usage(workspace, monkeypatch, "tui:c1")

    assert sink["context_used"] == live
    assert (usage["context_used"], usage["context_percent"]) == (sink["context_used"], sink["context_percent"])
    assert usage["context_estimated"] is False


def _fails_to_answer(usage: dict) -> LLMResponse:
    return LLMResponse(
        content="Error calling LLM (network@stub): connection reset",
        finish_reason="error",
        usage=usage,
        error_classification=ErrorClassification("network", retryable=False),
    )


@pytest.mark.asyncio
async def test_an_answered_turn_that_ends_on_a_provider_error_keeps_that_calls_reading(workspace, monkeypatch):
    """The answer went out through the message tool, so the provider error
    that follows ends the turn without failing it, and its reply is not filed
    either. The live meter showed that call's window, which ends at the newest
    row the turn kept."""
    agent = AgentLoop(
        provider=ScriptedProvider("stub", [_ANSWERS_THROUGH_THE_MESSAGE_TOOL, _fails_to_answer(_measured(7100, 0))]),
        workspace=workspace,
        model="stub",
        engine=EngineWiring(context_window_tokens=40000, context_config=ContextConfig(drop_segments=["curator"])),
        policy=TurnPolicy(max_iterations=6, empty_recovery=RecoveryLimits(llm_error_retry_delays=())),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    async def _delivered(_content: str, _media: list[str]) -> None:
        return None

    agent.tools.get("message").set_send_callback(_delivered)
    sink: dict = {}
    await _turn(agent, sink, session_key="tui:c1")

    usage = await _resumed_usage(workspace, monkeypatch, "tui:c1")

    assert sink["context_used"] == 7100
    assert (usage["context_used"], usage["context_percent"]) == (sink["context_used"], sink["context_percent"])
    assert usage["context_estimated"] is False


def _failing_agent(workspace: Path, limits: RecoveryLimits, script: list[LLMResponse]) -> AgentLoop:
    return AgentLoop(
        provider=ScriptedProvider("stub", script),
        workspace=workspace,
        model="stub",
        engine=EngineWiring(context_window_tokens=40000, context_config=ContextConfig(drop_segments=["curator"])),
        policy=TurnPolicy(max_iterations=6, empty_recovery=limits),
        tools=ToolWiring(restrict_to_workspace=True),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limits", "script", "live"),
    [
        (RecoveryLimits(enabled=False), [_says_nothing(_measured(7000, 0))], 7000),
        (
            RecoveryLimits(llm_error_retry_delays=()),
            [_says_nothing(_measured(7000, 0)), _says_nothing(_measured(7400, 0))],
            7400,
        ),
        (RecoveryLimits(llm_error_retry_delays=()), [_says_nothing(_measured(7000, 0)), _says_nothing({})], 7000),
        (RecoveryLimits(llm_error_retry_delays=()), [_fails_to_answer(_measured(7100, 0))], 7100),
    ],
    ids=["first_reply_empty", "every_retry_empty", "last_retry_unreported", "provider_error"],
)
async def test_a_failed_turn_that_filed_no_reply_keeps_its_reading_on_the_failure(
    workspace, monkeypatch, limits, script, live
):
    """No call of the turn left a row behind: each reply was empty or an error,
    and the question was filed before the first call. The failure the turn
    files is then the only row it adds, so that is where the live meter's last
    reading has to go for a resume to show it."""
    agent = _failing_agent(workspace, limits, script)
    sink: dict = {}

    with pytest.raises(AnswerlessTurnError):
        await _turn(agent, sink, session_key="tui:c1")

    failure = _stored_replies(workspace, "tui:c1")[-1]
    usage = await _resumed_usage(workspace, monkeypatch, "tui:c1")

    assert sink["context_used"] == live
    assert failure["turn_ended"]["status"] == "failed"
    assert failure["context_used"] == live
    assert (usage["context_used"], usage["context_percent"]) == (sink["context_used"], sink["context_percent"])
    assert usage["context_estimated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cut", "status"),
    [(asyncio.CancelledError, "cancelled"), (RuntimeError, "failed")],
    ids=["cancelled", "crashed"],
)
async def test_a_turn_cut_short_after_a_call_keeps_that_calls_reading_on_its_marker(
    workspace, monkeypatch, cut, status
):
    """A stop or a crash files the same marker an answerless turn does, and it
    can come right after a call whose empty reply left no row behind."""
    agent = _failing_agent(workspace, RecoveryLimits(llm_error_retry_delays=()), [_says_nothing(_measured(7000, 0))])
    strip = agent._strip_think
    seen: list[str | None] = []

    def _cut_on_the_retry(text):
        seen.append(text)
        if len(seen) > 1:
            raise cut("the turn was cut short")
        return strip(text)

    monkeypatch.setattr(agent, "_strip_think", _cut_on_the_retry)

    with pytest.raises(cut):
        await _turn(agent, {}, session_key="tui:c1")

    marker = _stored_replies(workspace, "tui:c1")[-1]
    usage = await _resumed_usage(workspace, monkeypatch, "tui:c1")

    assert marker["turn_ended"]["status"] == status
    assert marker["context_used"] == 7000
    assert (usage["context_used"], usage["context_estimated"]) == (7000, False)


@pytest.mark.asyncio
async def test_a_failed_turn_that_measured_nothing_files_no_reading(workspace):
    """Nothing measured means nothing to file, so a resume falls back to its estimate."""
    agent = _failing_agent(workspace, RecoveryLimits(llm_error_retry_delays=()), [_fails_to_answer({})])

    with pytest.raises(AnswerlessTurnError):
        await _turn(agent, {}, session_key="tui:c1")

    assert "context_used" not in _stored_replies(workspace, "tui:c1")[-1]


def _fails_in_transport(usage: dict) -> LLMResponse:
    return LLMResponse(
        content="The upstream reported a failed call instead of an answer.",
        finish_reason="error",
        usage=usage,
        error_classification=ErrorClassification("upstream_transport_failure", retryable=False),
    )


_LISTS_THE_WORKSPACE = LLMResponse(
    content="",
    tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
    usage=_measured(6000, 200),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [False, True], ids=["failed", "answered"])
async def test_a_call_that_failed_in_transport_reads_no_window(workspace, monkeypatch, answered):
    """The upstream's account of a call it failed is no reading of the window:
    it may be the very accounting that flagged the call, billed at a size the
    request cannot have been. Neither the live meter nor the stored record takes
    it, so both stay on the call before."""
    first = _ANSWERS_THROUGH_THE_MESSAGE_TOOL if answered else _LISTS_THE_WORKSPACE
    agent = _failing_agent(
        workspace,
        RecoveryLimits(llm_error_retry_delays=()),
        [first, _fails_in_transport(_measured(5, 0, cost_usd=0.002))],
    )

    async def _delivered(_content: str, _media: list[str]) -> None:
        return None

    agent.tools.get("message").set_send_callback(_delivered)
    sink: dict = {}
    if answered:
        await _turn(agent, sink, session_key="tui:c1")
    else:
        with pytest.raises(AnswerlessTurnError):
            await _turn(agent, sink, session_key="tui:c1")

    usage = await _resumed_usage(workspace, monkeypatch, "tui:c1")

    assert sink["context_used"] == 6200
    # The cost is the turn's, a flow, and the failed call was still billed.
    assert sink["cost_usd"] == pytest.approx(0.002)
    assert (usage["context_used"], usage["context_estimated"]) == (6200, False)
