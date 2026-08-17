"""Unit tests for DecisionConsumer + AgentLoop hook."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from raven.proactive_engine.sentinel.executor.action_executor import ActionExecutor
from raven.proactive_engine.sentinel.executor.decision_consumer import DecisionConsumer, MenuReply
from raven.proactive_engine.sentinel.executor.decision_router import DecisionRouter
from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
from raven.proactive_engine.sentinel.feedback.tracker import NudgeFeedbackTracker
from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore
from raven.proactive_engine.sentinel.types import PendingDecision, Routine, TaskOption
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, SentinelExtras, TurnRequest

_NOW = datetime(2026, 5, 8, 9, 0)
_NOW_MS = int(_NOW.timestamp() * 1000)


# ── helpers ───────────────────────────────────────────────────────────


def _option(
    *,
    oid: str = "opt_1",
    title: str = "草拟回复 X",
    exec_kind: str = "reply",
    exec_payload: dict | None = None,
    type: str = "ad_hoc",
) -> TaskOption:
    if exec_payload is None:
        exec_payload = {"prompt": "请帮我整理本周项目笔记"} if exec_kind == "reply" else {}
    return TaskOption(
        id=oid, title=title, why="why", type=type, exec_kind=exec_kind, exec_payload=exec_payload, created_at_ms=_NOW_MS
    )


def _decision(
    *, decision_id: str = "dec_x", channel: str = "feishu", to: str = "ou_xxx", options: list[TaskOption] | None = None
) -> PendingDecision:
    return PendingDecision(
        decision_id=decision_id,
        channel=channel,
        to=to,
        created_at_ms=_NOW_MS,
        ttl_min=60,
        options=options if options is not None else [_option()],
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def pending_store(tmp_path: Path) -> PendingDecisionStore:
    return PendingDecisionStore(tmp_path / "pending.json")


@pytest.fixture
def feedback(workspace: Path) -> NudgeFeedbackTracker:
    return NudgeFeedbackTracker(workspace / "sentinel_feedback.jsonl")


def _make_consumer(
    *,
    pending_store: PendingDecisionStore,
    router_provider=None,
    routine_store: RoutineStore | None = None,
    feedback: NudgeFeedbackTracker | None = None,
    require_confirm: bool = False,
) -> DecisionConsumer:
    """Default require_confirm=False so the legacy tests still
    exercise immediate-execute. Confirm-flow tests pass
    require_confirm=True explicitly."""
    router = DecisionRouter(
        pending_store=pending_store,
        provider=router_provider,
        model="x" if router_provider else None,
        now_fn=lambda: _NOW,
    )
    executor = ActionExecutor(routine_store=routine_store, now_fn=lambda: _NOW)
    submitted: list = []
    executor.set_submit(submitted.append)
    consumer = DecisionConsumer(
        router=router,
        executor=executor,
        pending_store=pending_store,
        feedback=feedback,
        require_confirm=require_confirm,
        now_fn=lambda: _NOW,
    )
    consumer._submitted = submitted  # captured exec-reply TurnRequests
    return consumer


def _msg(content: str, *, channel: str = "feishu", to: str = "ou_xxx", action_origin: bool = False) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel=channel, chat_id=to, sender_id="user", chat_type=ChatType.DM),
        text=content,
        sentinel=SentinelExtras(action_origin=True) if action_origin else None,
    )


# ── consumer fall-through ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_pending_decision_returns_none(pending_store):
    consumer = _make_consumer(pending_store=pending_store)
    result = await consumer(_msg("hello"))
    assert result is None


@pytest.mark.asyncio
async def test_action_origin_typed_field_falls_through(pending_store):
    """Spine path: the anti-recursion guard reads the typed
    ``sentinel_action_origin`` (a menu pick is origin=USER, so this hook IS
    reached) — the injected exec-prompt must not be re-consumed as a pick."""
    pending_store.put(_decision())
    consumer = _make_consumer(pending_store=pending_store)

    msg = _msg("/pick 1", action_origin=True)
    assert await consumer(msg) is None


# ── pick path → execute → reply ───────────────────────────────────────


@pytest.mark.asyncio
async def test_pick_via_regex_executes_reply_and_marks_consumed(pending_store, feedback):
    pending_store.put(
        _decision(
            options=[
                _option(oid="opt_1", title="任务 1", exec_payload={"prompt": "去做任务 1"}),
                _option(oid="opt_2", title="任务 2", exec_payload={"prompt": "去做任务 2"}),
            ]
        )
    )

    consumer = _make_consumer(pending_store=pending_store, feedback=feedback)

    out = await consumer(_msg("/pick 2"))
    assert isinstance(out, MenuReply)
    assert "任务 2" in out.content

    # Executor submitted the injected user prompt (USER origin, action_origin)
    assert len(consumer._submitted) == 1
    req = consumer._submitted[0]
    assert req.text == "去做任务 2"
    assert req.sentinel is not None and req.sentinel.action_origin is True

    # Decision marked consumed
    raw = pending_store._store.load()["decisions"][0]
    assert raw["consumed"] is True
    assert raw["picked_option_id"] == "opt_2"

    # Feedback recorded as accepted
    counts = feedback.counts(since_days=7)
    assert counts.get("accepted", 0) == 1


@pytest.mark.asyncio
async def test_skip_path_marks_dismissed(pending_store, feedback):
    pending_store.put(_decision())

    class _SkipProvider:
        async def chat_with_retry(self, *, messages, tools, model, tool_choice):
            class _Call:
                arguments = json.dumps({"intent": "skip", "confidence": 0.95})

            class _Resp:
                has_tool_calls = True
                tool_calls = [_Call()]

            return _Resp()

    consumer = _make_consumer(pending_store=pending_store, router_provider=_SkipProvider(), feedback=feedback)

    out = await consumer(_msg("跳过"))
    assert isinstance(out, MenuReply)
    assert "跳过" in out.content or "好的" in out.content

    # Decision still consumed=True but with picked_option_id=None
    raw = pending_store._store.load()["decisions"][0]
    assert raw["consumed"] is True
    assert raw["picked_option_id"] is None

    # Feedback recorded as dismissed (not accepted)
    counts = feedback.counts(since_days=7)
    assert counts.get("dismissed", 0) == 1
    assert counts.get("accepted", 0) == 0


@pytest.mark.asyncio
async def test_low_confidence_llm_falls_through(pending_store, feedback):
    pending_store.put(_decision())

    class _AmbiguousProvider:
        async def chat_with_retry(self, *, messages, tools, model, tool_choice):
            class _Call:
                arguments = json.dumps({"intent": "pick", "option_index": 1, "confidence": 0.3})

            class _Resp:
                has_tool_calls = True
                tool_calls = [_Call()]

            return _Resp()

    consumer = _make_consumer(pending_store=pending_store, router_provider=_AmbiguousProvider(), feedback=feedback)

    # User says something ambiguous; LLM replies with low confidence
    out = await consumer(_msg("都行你随便"))
    # Consumer falls through (no consume) — AgentLoop processes normally
    assert out is None

    # Decision still live (NOT marked consumed)
    raw = pending_store._store.load()["decisions"][0]
    assert raw["consumed"] is False


# ── routine_confirm path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_routine_confirm_pick_upgrades_routine(pending_store, feedback, tmp_path):
    routine_store = RoutineStore(tmp_path / "routines.json")
    routine_store.merge(
        [
            Routine(id="dow1-h09-meeting", pattern="x", occurrence_count=4),
        ],
        now_ms=_NOW_MS - 1000,
    )

    pending_store.put(
        _decision(
            options=[
                _option(
                    oid="opt_routine",
                    title="周二早会",
                    type="routine_confirm",
                    exec_kind="routine_confirm",
                    exec_payload={"routine_id": "dow1-h09-meeting"},
                ),
            ]
        )
    )

    consumer = _make_consumer(pending_store=pending_store, routine_store=routine_store, feedback=feedback)

    out = await consumer(_msg("/pick 1"))
    assert isinstance(out, MenuReply)
    assert "周二早会" in out.content
    assert "upgraded routine" in out.content or "已确认" in out.content

    # Routine actually upgraded
    r = routine_store.get("dow1-h09-meeting")
    assert r is not None
    assert r.status == "active"


# ── error rendering ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_status_renders_user_facing_apology(pending_store):
    # Pick a routine_confirm option without a routine_store wired → error
    pending_store.put(
        _decision(
            options=[
                _option(
                    oid="opt_x",
                    title="oops",
                    type="routine_confirm",
                    exec_kind="routine_confirm",
                    exec_payload={"routine_id": "nope"},
                ),
            ]
        )
    )
    consumer = _make_consumer(pending_store=pending_store)
    out = await consumer(_msg("/pick 1"))
    assert isinstance(out, MenuReply)
    assert "无法执行" in out.content
    assert "oops" in out.content


# ── AgentLoop hook integration (lightweight) ──────────────────────────


@pytest.mark.asyncio
async def test_decision_consumer_short_circuits_agent_loop(pending_store):
    """Smoke test the decision_consumer hook on AgentLoop. We mock the
    consumer to check only that it's wired and short-circuits the
    process_message path without running the full LLM pipeline."""
    from raven.agent.loop import AgentLoop

    class _FakeProvider:
        def get_default_model(self) -> str:
            return "fake-model"

        async def chat_with_retry(self, **kw):
            # Should NEVER be called when consumer short-circuits
            raise AssertionError("LLM should not be invoked")

    async def _consumer_hook(req):
        return MenuReply(
            channel=req.source.channel, chat_id=req.source.chat_id, content="✓ consumed by decision_consumer"
        )

    workspace = Path("/tmp") / f"ec-test-{_NOW_MS}"
    workspace.mkdir(parents=True, exist_ok=True)

    loop = AgentLoop(
        provider=_FakeProvider(),  # type: ignore[arg-type]
        workspace=workspace,
        decision_consumer=_consumer_hook,
    )

    msg = _msg("/pick 1")
    out = await loop._process_message(msg)
    assert out is not None
    content, _media = out
    assert "consumed by decision_consumer" in content


@pytest.mark.asyncio
async def test_decision_consumer_falls_through_on_none(pending_store):
    """If consumer returns None, AgentLoop continues with normal flow.
    We can't easily test the full normal flow here without a full LLM
    setup — settle for verifying the hook is called and the result is
    used."""
    from raven.agent.loop import AgentLoop

    calls = {"n": 0}

    async def _no_consume_hook(msg):
        calls["n"] += 1
        return None

    workspace = Path("/tmp") / f"ec-test-noconsume-{_NOW_MS}"
    workspace.mkdir(parents=True, exist_ok=True)

    class _FakeProvider:
        def get_default_model(self) -> str:
            return "fake-model"

        async def chat_with_retry(self, **kw):
            # Dummy short response — we just want to verify the hook
            # was called before this LLM call would have happened.
            class _R:
                content = "fake reply"
                has_tool_calls = False
                tool_calls = []
                role = "assistant"

            return _R()

    loop = AgentLoop(
        provider=_FakeProvider(),  # type: ignore[arg-type]
        workspace=workspace,
        decision_consumer=_no_consume_hook,
    )

    # Use a real-ish session message — the loop will go through the
    # normal path. We just verify the consumer hook fired.
    msg = _msg("hello")
    try:
        await loop._process_message(msg)
    except Exception:
        # The fake provider may throw mid-flow; that's fine — we only
        # care that the consumer was called once before the failure.
        pass
    assert calls["n"] == 1
