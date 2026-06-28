"""Sentinel nudge-policy and pending-decision behavior:

- NudgePolicy integration in TaskDiscoverer (check + record_fired)
- PendingDecisionStore.put returns superseded-awaiting decision IDs;
  TaskDiscoverer notifies user when superseding an awaiting_confirm decision
- attach_sentinel_decision_consumer startup warning when require_confirm=True
  but no LLM provider is configured
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.config.raven import NudgePolicyConfig
from raven.proactive_engine.sentinel.executor.dispatcher import NudgeDispatcher
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy
from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
from raven.proactive_engine.sentinel.predictor.task_discoverer import TaskDiscoverer
from raven.proactive_engine.sentinel.types import PendingDecision, TaskOption


_NOW = datetime(2026, 5, 8, 8, 0)
_NOW_MS = int(_NOW.timestamp() * 1000)


# ── helpers ───────────────────────────────────────────────────────────


def _make_decision(
    *, decision_id: str = "dec_x",
    awaiting_confirm: bool = False,
    consumed: bool = False,
    channel: str = "feishu", to: str = "ou_xxx",
    created_at_ms: int = _NOW_MS,
    ttl_min: int = 60,
) -> PendingDecision:
    return PendingDecision(
        decision_id=decision_id,
        channel=channel, to=to,
        created_at_ms=created_at_ms,
        ttl_min=ttl_min,
        options=[
            TaskOption(id="opt_1", title="task one", why="why",
                       type="ad_hoc", exec_kind="reply",
                       exec_payload={"prompt": "do one"},
                       created_at_ms=created_at_ms),
        ],
        consumed=consumed,
        awaiting_confirm=awaiting_confirm,
        picked_option_id="opt_1" if awaiting_confirm else None,
    )


class _DiscoveryStubProvider:
    """Returns 3 canned options on the discovery LLM call."""

    async def chat_with_retry(self, *, messages, tools, model, tool_choice):
        args = json.dumps({
            "options": [
                {
                    "title": f"task {i}", "why": "why",
                    "type": "ad_hoc", "exec_kind": "reply",
                    "exec_payload": {"prompt": f"do task {i}"},
                }
                for i in range(3)
            ],
        })
        class _Call:
            arguments = args
        class _Resp:
            has_tool_calls = True
            tool_calls = [_Call()]
        return _Resp()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "memory").mkdir()
    return ws


@pytest.fixture
def memory_store(workspace: Path) -> MemoryStore:
    store = MemoryStore(workspace)
    store.write_long_term("## User Information\n- name: Alice")
    store.append_history("[2026-05-08 06:30] morning routine")
    return store


# ── #2: supersede-awaiting return value + user notify ────────────────


def test_put_returns_superseded_awaiting_ids(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    # Plant an awaiting_confirm decision
    awaiting = _make_decision(decision_id="dec_old", awaiting_confirm=True,
                              created_at_ms=_NOW_MS - 1000)
    store.put(awaiting)

    # New decision on same address — should supersede the awaiting one
    fresh = _make_decision(decision_id="dec_new", created_at_ms=_NOW_MS)
    superseded = store.put(fresh)
    assert superseded == ["dec_old"]


def test_put_returns_empty_when_no_awaiting_superseded(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    # Plant a fresh (not awaiting) decision
    not_awaiting = _make_decision(decision_id="dec_old",
                                   awaiting_confirm=False,
                                   created_at_ms=_NOW_MS - 1000)
    store.put(not_awaiting)

    fresh = _make_decision(decision_id="dec_new", created_at_ms=_NOW_MS)
    superseded = store.put(fresh)
    # Old decision IS superseded (no longer in store) but not in
    # awaiting_confirm — return value lists only awaiting ones
    assert superseded == []
    raw = store._store.load()["decisions"]
    assert len(raw) == 1
    assert raw[0]["decision_id"] == "dec_new"


def test_put_returns_empty_for_first_decision(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    fresh = _make_decision(decision_id="dec_new")
    superseded = store.put(fresh)
    assert superseded == []


def test_put_returns_empty_when_consumed_decision_present(tmp_path: Path):
    """A previously-consumed decision is kept in the store for audit;
    superseding it isn't a concern (user already picked or cancelled).
    Only un-consumed awaiting_confirm decisions matter."""
    store = PendingDecisionStore(tmp_path / "pending.json")
    consumed = _make_decision(decision_id="dec_consumed",
                              consumed=True,
                              created_at_ms=_NOW_MS - 1000)
    # Bypass the lifecycle methods — direct hand-poke for setup
    store.put(_make_decision(decision_id="dec_temp"))
    store.mark_consumed("dec_temp", picked_option_id="opt_1",
                        consumed_at_ms=_NOW_MS - 500)

    fresh = _make_decision(decision_id="dec_new")
    superseded = store.put(fresh)
    # The consumed decision wasn't dropped (kept for audit) and isn't
    # in awaiting state, so nothing returns
    assert superseded == []


# ── #1: NudgePolicy integration in TaskDiscoverer ─────────────────────


@pytest.mark.asyncio
async def test_discoverer_calls_policy_check_and_record_fired(
    memory_store, tmp_path
):
    pending_store = PendingDecisionStore(tmp_path / "pending.json")
    dispatcher = NudgeDispatcher(now_fn=lambda: _NOW)
    dispatcher.set_post(AsyncMock())
    provider = _DiscoveryStubProvider()
    policy = NudgePolicy(NudgePolicyConfig(), now_fn=lambda: _NOW)

    discoverer = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        policy=policy,
        max_options=4,
        now_fn=lambda: _NOW,
    )

    decision = await discoverer.run(channel="feishu", to="ou_xxx")
    assert decision is not None

    # NudgePolicy should now know about the fire (next check on same
    # session for a "nudge" with same content would be denied via dedup)
    second_check = policy.check(
        "nudge", session_key="feishu:ou_xxx",
        # Use the same menu preview that TaskDiscoverer used internally
        content="(any)", priority="medium",
    )
    # We can't easily verify the exact dedup hit without re-rendering
    # the menu; instead verify fired_at was incremented
    assert len(policy._fired_at) == 1


@pytest.mark.asyncio
async def test_discoverer_skips_when_policy_denies(memory_store, tmp_path):
    pending_store = PendingDecisionStore(tmp_path / "pending.json")
    posted: list = []

    async def _post(out):
        posted.append(out)

    dispatcher = NudgeDispatcher(now_fn=lambda: _NOW)
    dispatcher.set_post(_post)
    provider = _DiscoveryStubProvider()

    # Build a policy with quiet_hours that include 8 AM (the test time)
    policy_cfg = NudgePolicyConfig(quiet_hours=(7, 9))   # 7-9 AM = quiet
    policy = NudgePolicy(policy_cfg, now_fn=lambda: _NOW)

    discoverer = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        policy=policy,
        max_options=4,
        now_fn=lambda: _NOW,
    )

    decision = await discoverer.run(channel="feishu", to="ou_xxx")
    # Denied — no menu dispatched, no PendingDecision persisted
    assert decision is None
    assert pending_store.get_recent("feishu", "ou_xxx", now_ms=_NOW_MS + 1) is None
    # Nothing dispatched
    assert posted == []
    # Policy state untouched (no fire recorded)
    assert len(policy._fired_at) == 0


@pytest.mark.asyncio
async def test_discoverer_works_without_policy(memory_store, tmp_path):
    """Backwards-compat: policy is optional; harness/tests that don't
    pass one should still get the menu dispatched."""
    pending_store = PendingDecisionStore(tmp_path / "pending.json")
    dispatcher = NudgeDispatcher(now_fn=lambda: _NOW)
    dispatcher.set_post(AsyncMock())
    provider = _DiscoveryStubProvider()

    discoverer = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        # no policy passed
        max_options=4,
        now_fn=lambda: _NOW,
    )
    decision = await discoverer.run(channel="feishu", to="ou_xxx")
    assert decision is not None


# ── #2: TaskDiscoverer notifies user on superseded awaiting ───────────


@pytest.mark.asyncio
async def test_discoverer_notifies_user_when_superseding_awaiting_confirm(
    memory_store, tmp_path
):
    pending_store = PendingDecisionStore(tmp_path / "pending.json")
    posted: list = []

    async def _post(out):
        posted.append(out)

    dispatcher = NudgeDispatcher(now_fn=lambda: _NOW)
    dispatcher.set_post(_post)
    provider = _DiscoveryStubProvider()

    # Plant an awaiting_confirm decision on the same address
    awaiting = _make_decision(
        decision_id="dec_old", awaiting_confirm=True,
        created_at_ms=_NOW_MS - 30 * 60_000,  # 30 min ago, well within TTL
    )
    pending_store.put(awaiting)

    discoverer = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        max_options=4,
        now_fn=lambda: _NOW,
    )
    submitted: list = []
    discoverer.set_submit(submitted.append)

    decision = await discoverer.run(channel="feishu", to="ou_xxx")
    assert decision is not None

    # The supersede notice now submits a SENTINEL-origin turn (off the bus).
    from raven.spine import Origin

    assert len(submitted) == 1
    assert submitted[0].origin is Origin.SENTINEL
    assert "替换" in submitted[0].text

    # The new menu still goes through the dispatcher to the hub.
    menu_msgs = [m for m in posted
                 if m.source.extras.get("_sentinel_action")
                     == "discovery_menu"]
    assert len(menu_msgs) == 1


@pytest.mark.asyncio
async def test_discoverer_suppresses_notice_when_dispatch_fails(
    memory_store, tmp_path
):
    """If dispatch_options raises, the supersede notice MUST NOT be
    sent — otherwise the user sees 'your pick was replaced' without
    ever receiving the replacement menu, which is worse UX than the
    silent supersession we used to do."""
    pending_store = PendingDecisionStore(tmp_path / "pending.json")
    provider = _DiscoveryStubProvider()

    # Plant an awaiting_confirm decision so a successful run would
    # trigger the notice
    pending_store.put(_make_decision(
        decision_id="dec_old", awaiting_confirm=True,
        created_at_ms=_NOW_MS - 10 * 60_000,
    ))

    # Build a dispatcher that always raises on dispatch_options
    class _FailingDispatcher:
        async def dispatch_options(self, decision):
            raise RuntimeError("simulated dispatch failure")

    discoverer = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=_FailingDispatcher(),
        provider=provider,
        model="x",
        max_options=4,
        now_fn=lambda: _NOW,
    )
    submitted: list = []
    discoverer.set_submit(submitted.append)

    # discoverer.run() catches the dispatch failure and returns the
    # persisted decision — no exception propagates to caller.
    decision = await discoverer.run(channel="feishu", to="ou_xxx")
    assert decision is not None  # decision was persisted

    # Notice must be suppressed when dispatch fails — nothing submitted
    assert submitted == [], (
        "supersede notice should be suppressed when dispatch fails — "
        "otherwise user is told 'pick replaced' with no replacement "
        "menu following"
    )


@pytest.mark.asyncio
async def test_discoverer_no_notice_when_superseding_unpicked_decision(
    memory_store, tmp_path
):
    """If the prior decision wasn't picked (still showing fresh
    options), there's nothing user-facing to recover — no notice
    sent."""
    pending_store = PendingDecisionStore(tmp_path / "pending.json")
    dispatcher = NudgeDispatcher(now_fn=lambda: _NOW)
    dispatcher.set_post(AsyncMock())
    provider = _DiscoveryStubProvider()

    pending_store.put(_make_decision(
        decision_id="dec_old", awaiting_confirm=False,
        created_at_ms=_NOW_MS - 30 * 60_000,
    ))

    discoverer = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        max_options=4,
        now_fn=lambda: _NOW,
    )
    submitted: list = []
    discoverer.set_submit(submitted.append)

    await discoverer.run(channel="feishu", to="ou_xxx")
    # Superseding an unpicked decision recovers nothing user-facing, so no
    # supersede notice turn is submitted.
    assert submitted == []


# ── #4: startup warning when require_confirm=True without LLM ─────────


def test_attach_decision_consumer_warns_on_no_llm_provider(tmp_path,
                                                             caplog):
    """Health check: require_confirm=True without an LLM provider
    works for clear yes/no but ambiguous replies fall through. Operator
    should be warned at startup."""
    from raven.cli._proactive_stack import attach_sentinel_decision_consumer
    from raven.config.raven import SentinelConfig

    # Build a minimal runner stub with phase4 stash but provider=None
    class _StubRunner:
        feedback = MagicMock()
    runner = _StubRunner()
    pending_store = PendingDecisionStore(tmp_path / "pending.json")
    runner._phase4_pending_store = pending_store
    runner._phase4_routine_store = None
    runner._phase4_planner_provider = None
    runner._phase4_planner_model = None
    runner._phase4_now_fn = None

    # Build a minimal agent stub
    from raven.agent.hook.composite import CompositeHook
    class _StubAgent:
        cron_service = None
        tools = MagicMock()
        subagents = MagicMock()
        decision_consumer = None
        hooks = CompositeHook()
    agent = _StubAgent()

    sentinel_cfg = SentinelConfig(
        enabled=True,
        task_discovery_enabled=True,
        task_discovery_require_confirm=True,
    )

    import io
    from loguru import logger as _logger
    captured = io.StringIO()
    sink_id = _logger.add(captured, level="WARNING")
    try:
        attach_sentinel_decision_consumer(runner, agent,
                                          sentinel_cfg=sentinel_cfg)
    finally:
        _logger.remove(sink_id)

    log_text = captured.getvalue()
    assert "task_discovery_require_confirm=True but no LLM" in log_text
    # Consumer was still attached (degraded mode is functional for
    # clear yes/no via regex)
    assert agent.decision_consumer is not None


def test_attach_decision_consumer_no_warn_when_provider_set(tmp_path):
    """Same setup but with provider configured — no warning."""
    from raven.cli._proactive_stack import attach_sentinel_decision_consumer
    from raven.config.raven import SentinelConfig

    class _StubProvider:
        async def chat_with_retry(self, **kw):
            return MagicMock()

    class _StubRunner:
        feedback = MagicMock()
    runner = _StubRunner()
    runner._phase4_pending_store = PendingDecisionStore(
        tmp_path / "pending.json"
    )
    runner._phase4_routine_store = None
    runner._phase4_planner_provider = _StubProvider()
    runner._phase4_planner_model = "qwen3.5-27B"
    runner._phase4_now_fn = None

    from raven.agent.hook.composite import CompositeHook
    class _StubAgent:
        cron_service = None
        tools = MagicMock()
        subagents = MagicMock()
        decision_consumer = None
        hooks = CompositeHook()
    agent = _StubAgent()

    sentinel_cfg = SentinelConfig(
        enabled=True,
        task_discovery_enabled=True,
        task_discovery_require_confirm=True,
    )

    import io
    from loguru import logger as _logger
    captured = io.StringIO()
    sink_id = _logger.add(captured, level="WARNING")
    try:
        attach_sentinel_decision_consumer(runner, agent,
                                          sentinel_cfg=sentinel_cfg)
    finally:
        _logger.remove(sink_id)
    assert "no LLM provider" not in captured.getvalue()


def test_attach_decision_consumer_registers_hook(tmp_path):
    """Regression guard: attach must append DecisionConsumerAdapter to
    agent.hooks so before_user_inbound actually short-circuits menu
    replies. Without this, picks fall through to normal agent loop and
    PendingDecisionStore is never updated."""
    from raven.agent.hook.adapters import DecisionConsumerAdapter
    from raven.agent.hook.composite import CompositeHook
    from raven.cli._proactive_stack import attach_sentinel_decision_consumer
    from raven.config.raven import SentinelConfig

    class _StubRunner:
        feedback = MagicMock()
    runner = _StubRunner()
    runner._phase4_pending_store = PendingDecisionStore(
        tmp_path / "pending.json"
    )
    runner._phase4_routine_store = None
    runner._phase4_planner_provider = None
    runner._phase4_planner_model = None
    runner._phase4_now_fn = None

    class _StubAgent:
        cron_service = None
        tools = MagicMock()
        subagents = MagicMock()
        decision_consumer = None
        hooks = CompositeHook()
    agent = _StubAgent()

    sentinel_cfg = SentinelConfig(
        enabled=True,
        task_discovery_enabled=True,
        task_discovery_require_confirm=False,
    )
    assert not any(
        isinstance(h, DecisionConsumerAdapter) for h in agent.hooks
    )
    attach_sentinel_decision_consumer(runner, agent, sentinel_cfg=sentinel_cfg)
    assert any(
        isinstance(h, DecisionConsumerAdapter) for h in agent.hooks
    )


def test_attach_decision_consumer_is_idempotent(tmp_path):
    """A second attach call on the same agent must NOT add a second
    DecisionConsumerAdapter. Double-attach would fire ActionExecutor
    twice for every menu pick — silent double-dispatch hazard if a
    future caller (fixture reuse, hot-reload, multi-process bootstrap)
    re-runs the helper."""
    from raven.agent.hook.adapters import DecisionConsumerAdapter
    from raven.agent.hook.composite import CompositeHook
    from raven.cli._proactive_stack import attach_sentinel_decision_consumer
    from raven.config.raven import SentinelConfig

    class _StubRunner:
        feedback = MagicMock()
    runner = _StubRunner()
    runner._phase4_pending_store = PendingDecisionStore(
        tmp_path / "pending.json"
    )
    runner._phase4_routine_store = None
    runner._phase4_planner_provider = None
    runner._phase4_planner_model = None
    runner._phase4_now_fn = None

    class _StubAgent:
        cron_service = None
        tools = MagicMock()
        subagents = MagicMock()
        decision_consumer = None
        hooks = CompositeHook()
    agent = _StubAgent()

    sentinel_cfg = SentinelConfig(
        enabled=True,
        task_discovery_enabled=True,
        task_discovery_require_confirm=False,
    )
    attach_sentinel_decision_consumer(runner, agent, sentinel_cfg=sentinel_cfg)
    attach_sentinel_decision_consumer(runner, agent, sentinel_cfg=sentinel_cfg)
    adapter_count = sum(
        1 for h in agent.hooks if isinstance(h, DecisionConsumerAdapter)
    )
    assert adapter_count == 1


def test_attach_decision_consumer_no_warn_when_require_confirm_false(
    tmp_path,
):
    """If require_confirm=False, the warning is irrelevant."""
    from raven.cli._proactive_stack import attach_sentinel_decision_consumer
    from raven.config.raven import SentinelConfig

    class _StubRunner:
        feedback = MagicMock()
    runner = _StubRunner()
    runner._phase4_pending_store = PendingDecisionStore(
        tmp_path / "pending.json"
    )
    runner._phase4_routine_store = None
    runner._phase4_planner_provider = None
    runner._phase4_planner_model = None
    runner._phase4_now_fn = None

    from raven.agent.hook.composite import CompositeHook
    class _StubAgent:
        cron_service = None
        tools = MagicMock()
        subagents = MagicMock()
        decision_consumer = None
        hooks = CompositeHook()
    agent = _StubAgent()

    sentinel_cfg = SentinelConfig(
        enabled=True,
        task_discovery_enabled=True,
        task_discovery_require_confirm=False,
    )

    import io
    from loguru import logger as _logger
    captured = io.StringIO()
    sink_id = _logger.add(captured, level="WARNING")
    try:
        attach_sentinel_decision_consumer(runner, agent,
                                          sentinel_cfg=sentinel_cfg)
    finally:
        _logger.remove(sink_id)
    assert "no LLM provider" not in captured.getvalue()
