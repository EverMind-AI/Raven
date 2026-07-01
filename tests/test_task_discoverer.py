"""Unit tests for TaskDiscoverer + dispatch_options + render_menu_markdown (MS2)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.proactive_engine.sentinel.executor.dispatcher import (
    NudgeDispatcher,
    render_menu_markdown,
)
from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
from raven.proactive_engine.sentinel.predictor.task_discoverer import (
    TaskDiscoverer,
    _extract_sentinel_observations_block,
)
from raven.proactive_engine.sentinel.types import PendingDecision, TaskOption

_NOW = datetime(2026, 5, 8, 8, 0)
_NOW_MS = int(_NOW.timestamp() * 1000)


def _wire_dispatcher(now_fn):
    posted: list = []
    d = NudgeDispatcher(now_fn=now_fn)

    async def _post(out):
        posted.append(out)

    d.set_post(_post)
    return d, posted


# ── helpers ───────────────────────────────────────────────────────────


class _StubResponse:
    """Mimics LLMProvider.chat_with_retry's response object enough for
    the parts TaskDiscoverer touches (has_tool_calls + tool_calls list
    of objects with `.arguments`)."""

    def __init__(self, options: list[dict] | None, *, raw_args=None):
        if raw_args is not None:
            self._args = raw_args
        elif options is None:
            self._args = None
        else:
            self._args = json.dumps({"options": options})
        self.has_tool_calls = self._args is not None
        if self.has_tool_calls:

            class _Call:
                arguments = self._args

            _Call.arguments = self._args
            self.tool_calls = [_Call()]
        else:
            self.tool_calls = []


class _StubProvider:
    """Async LLM provider stub with configurable canned response."""

    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    async def chat_with_retry(self, *, messages, tools, model, tool_choice):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "model": model,
                "tool_choice": tool_choice,
            }
        )
        return self._response


def _option_dict(**overrides):
    base = {
        "title": "草拟回复 X",
        "why": "X 在昨天发了你尚未回复",
        "type": "ad_hoc",
        "exec_kind": "reply",
        "exec_payload": {"prompt": "请帮我草拟回复 X"},
        "source": "history",
        "priority": "medium",
    }
    base.update(overrides)
    return base


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "memory").mkdir()
    return ws


@pytest.fixture
def memory_store(workspace: Path) -> MemoryStore:
    store = MemoryStore(workspace)
    # Plant some content so discovery prompt has substance to work with
    store.write_long_term(
        "## User Information\n"
        "- name: Alice\n"
        "\n"
        "## Sentinel Observations (auto)\n"
        "<!-- sentinel:auto last_updated=2026-05-07T08:00 -->\n"
        "\n"
        "### Signal counts (last 7 days)\n"
        "- dispatched: 47, accepted: 36 (76%)\n"
        "\n"
        "<!-- /sentinel:auto -->\n"
    )
    store.append_history("[2026-05-08 06:30] User started morning routine")
    return store


@pytest.fixture
def pending_store(tmp_path: Path) -> PendingDecisionStore:
    return PendingDecisionStore(tmp_path / "pending.json")


# ── render_menu_markdown ──────────────────────────────────────────────


def test_render_menu_markdown_format():
    decision = PendingDecision(
        decision_id="dec_x",
        channel="feishu",
        to="ou_xxx",
        created_at_ms=_NOW_MS,
        ttl_min=60,
        options=[
            TaskOption(
                id="opt_a",
                title="草拟回复 X",
                why="X 在昨天发了未回复",
                type="ad_hoc",
                exec_kind="reply",
                exec_payload={},
            ),
            TaskOption(
                id="opt_b",
                title="周二 PR review",
                why="最近 3 周这么做过",
                type="routine_confirm",
                exec_kind="routine_confirm",
                exec_payload={"routine_id": "dow1-h09-pr"},
            ),
        ],
    )
    text = render_menu_markdown(decision)
    assert text.startswith("📋 [今日建议]")
    assert "1. (新任务) 草拟回复 X" in text
    assert "   — X 在昨天发了未回复" in text
    assert "2. (持续模式 ✓) 周二 PR review" in text
    assert "回复数字选择" in text


def test_render_menu_markdown_omits_why_when_empty():
    decision = PendingDecision(
        decision_id="dec_x",
        channel="feishu",
        to="ou_xxx",
        created_at_ms=_NOW_MS,
        options=[
            TaskOption(id="opt_a", title="bare option", why="", type="ad_hoc", exec_kind="reply", exec_payload={}),
        ],
    )
    text = render_menu_markdown(decision)
    assert "1. (新任务) bare option" in text
    # No "—" line for empty why
    assert "   —" not in text.split("回复数字")[0]


# ── _extract_sentinel_observations_block ──────────────────────────────


def test_extract_observations_block_pulls_section_body():
    md = (
        "## User Information\n"
        "- foo\n"
        "\n"
        "## Sentinel Observations (auto)\n"
        "<!-- sentinel:auto last_updated=2026-05-07T08:00 -->\n"
        "BODY HERE\n"
        "<!-- /sentinel:auto -->\n"
        "\n"
        "## Other\n"
    )
    body = _extract_sentinel_observations_block(md)
    assert body == "BODY HERE"


def test_extract_observations_block_returns_empty_when_missing():
    assert _extract_sentinel_observations_block("") == ""
    assert _extract_sentinel_observations_block("## Foo\nbar") == ""


# ── TaskDiscoverer.run happy path ─────────────────────────────────────


@pytest.mark.asyncio
async def test_supersede_notice_submits_sentinel_origin_when_wired(memory_store, pending_store):
    # Spine path: with submit wired, the supersede notice runs as a
    # SENTINEL-origin turn (a system notice — after_send is skipped) instead of
    # publishing to the bus.
    from raven.spine import Origin

    dispatcher, _posted = _wire_dispatcher(lambda: _NOW)
    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=_StubProvider(_StubResponse([])),
        model="x",
        now_fn=lambda: _NOW,
    )

    captured = {}

    class _Handle:
        async def result(self):
            return None

    disco.set_submit(lambda req: (captured.__setitem__("req", req), _Handle())[1])

    await disco._notify_superseded_awaiting(
        channel="feishu",
        to="ou_xxx",
        superseded_ids=["dec_old"],
    )

    req = captured["req"]
    assert req.origin is Origin.SENTINEL
    assert req.source.sender_id == "sentinel"
    assert req.source.channel == "feishu" and req.source.chat_id == "ou_xxx"
    assert "替换" in req.text
    assert req.sentinel is None  # not a menu-pick → no action_origin


@pytest.mark.asyncio
async def test_discoverer_happy_path_creates_decision_and_dispatches(memory_store, pending_store):
    dispatcher, posted = _wire_dispatcher(lambda: _NOW)
    response = _StubResponse(
        [
            _option_dict(title="task A"),
            _option_dict(title="task B", priority="high"),
            _option_dict(title="task C", source="memory"),
        ]
    )
    provider = _StubProvider(response)

    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="qwen3.5-27B",
        max_options=4,
        decision_ttl_min=60,
        now_fn=lambda: _NOW,
    )

    decision = await disco.run(channel="feishu", to="ou_xxx")
    assert decision is not None
    assert len(decision.options) == 3
    titles = [o.title for o in decision.options]
    assert titles == ["task A", "task B", "task C"]

    # Persisted
    fetched = pending_store.get_recent("feishu", "ou_xxx", now_ms=_NOW_MS + 1)
    assert fetched is not None
    assert fetched.decision_id == decision.decision_id

    outbound = posted.pop(0)
    assert outbound.source.channel == "feishu"
    assert outbound.source.chat_id == "ou_xxx"
    assert outbound.source.extras["_sentinel_action"] == "discovery_menu"
    assert "task A" in outbound.content
    assert "task B" in outbound.content


@pytest.mark.asyncio
async def test_discoverer_returns_none_on_no_tool_call(memory_store, pending_store):
    dispatcher, _posted = _wire_dispatcher(lambda: _NOW)
    provider = _StubProvider(_StubResponse(options=None))

    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        now_fn=lambda: _NOW,
    )

    result = await disco.run(channel="feishu", to="ou_xxx")
    assert result is None
    # Nothing persisted
    assert pending_store.get_recent("feishu", "ou_xxx", now_ms=_NOW_MS + 1) is None


@pytest.mark.asyncio
async def test_discoverer_drops_malformed_options_keeps_valid(memory_store, pending_store):
    dispatcher, _posted = _wire_dispatcher(lambda: _NOW)
    response = _StubResponse(
        [
            _option_dict(title="valid one"),
            {"title": "missing fields"},  # missing exec_kind+payload → invalid
            _option_dict(title="", exec_kind="reply"),  # empty title → dropped
            _option_dict(title="bad kind", exec_kind="weird_kind"),  # unknown kind
            _option_dict(title="another valid", priority="high"),
        ]
    )
    provider = _StubProvider(response)

    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        now_fn=lambda: _NOW,
    )

    decision = await disco.run(channel="feishu", to="ou_xxx")
    assert decision is not None
    titles = [o.title for o in decision.options]
    assert titles == ["valid one", "another valid"]


@pytest.mark.asyncio
async def test_discoverer_rejects_routine_confirm_without_routine_id(memory_store, pending_store):
    dispatcher, _posted = _wire_dispatcher(lambda: _NOW)
    response = _StubResponse(
        [
            _option_dict(
                title="bad routine",
                type="routine_confirm",
                exec_kind="routine_confirm",
                exec_payload={},  # missing routine_id
            ),
            _option_dict(
                title="good routine",
                type="routine_confirm",
                exec_kind="routine_confirm",
                exec_payload={"routine_id": "dow1-h09-meeting", "make_cron": True},
            ),
        ]
    )
    provider = _StubProvider(response)

    # pad with 2 ad_hoc options so we get the minimum 3
    response = _StubResponse(
        [
            _option_dict(
                title="bad routine",
                type="routine_confirm",
                exec_kind="routine_confirm",
                exec_payload={},
            ),
            _option_dict(
                title="good routine",
                type="routine_confirm",
                exec_kind="routine_confirm",
                exec_payload={"routine_id": "dow1-h09-meeting"},
            ),
            _option_dict(title="ad hoc 1"),
            _option_dict(title="ad hoc 2"),
        ]
    )
    provider = _StubProvider(response)

    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        now_fn=lambda: _NOW,
    )

    decision = await disco.run(channel="feishu", to="ou_xxx")
    assert decision is not None
    titles = [o.title for o in decision.options]
    assert "bad routine" not in titles
    assert "good routine" in titles


@pytest.mark.asyncio
async def test_discoverer_truncates_to_max_options(memory_store, pending_store):
    dispatcher, _posted = _wire_dispatcher(lambda: _NOW)
    response = _StubResponse([_option_dict(title=f"option {i}") for i in range(8)])
    provider = _StubProvider(response)

    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        max_options=3,  # keep only the first 3
        now_fn=lambda: _NOW,
    )

    decision = await disco.run(channel="feishu", to="ou_xxx")
    assert decision is not None
    assert len(decision.options) == 3
    titles = [o.title for o in decision.options]
    assert titles == ["option 0", "option 1", "option 2"]


@pytest.mark.asyncio
async def test_discoverer_overdue_survives_truncation(memory_store, pending_store):
    """Regression: an overdue option emitted past max_options must float to
    the front BEFORE truncation, not get dropped (annotate-then-truncate)."""
    dispatcher, _posted = _wire_dispatcher(lambda: _NOW)
    opts = [_option_dict(title=f"option {i}") for i in range(5)]
    # Overdue task emitted LAST (index 5), deadline before _NOW (2026-05-08).
    opts.append(_option_dict(title="交月报", deadline="2026-05-01"))
    provider = _StubProvider(_StubResponse(opts))

    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        max_options=3,
        now_fn=lambda: _NOW,
    )

    decision = await disco.run(channel="feishu", to="ou_xxx")
    assert decision is not None
    assert len(decision.options) == 3
    # Floated to the front with the overdue marker; survived truncation.
    assert decision.options[0].title == "⚠️ 逾期 5/1 交月报"


@pytest.mark.asyncio
async def test_discoverer_handles_malformed_json_args_gracefully(memory_store, pending_store):
    dispatcher, _posted = _wire_dispatcher(lambda: _NOW)
    # Hand-craft response with non-JSON arg string
    response = _StubResponse(options=None, raw_args="this is not json")
    response.has_tool_calls = True

    class _C:
        arguments = "this is not json"

    response.tool_calls = [_C()]
    provider = _StubProvider(response)

    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        now_fn=lambda: _NOW,
    )

    result = await disco.run(channel="feishu", to="ou_xxx")
    assert result is None


@pytest.mark.asyncio
async def test_discoverer_supersedes_prior_decision_on_same_address(memory_store, pending_store):
    dispatcher, _posted = _wire_dispatcher(lambda: _NOW)

    # Plant a pre-existing decision
    pre_existing = PendingDecision(
        decision_id="dec_old",
        channel="feishu",
        to="ou_xxx",
        created_at_ms=_NOW_MS - 1000,
        options=[TaskOption(id="opt_old", title="old", why="", type="ad_hoc", exec_kind="reply", exec_payload={})],
    )
    pending_store.put(pre_existing)

    response = _StubResponse(
        [
            _option_dict(title="new task A"),
            _option_dict(title="new task B"),
            _option_dict(title="new task C"),
        ]
    )
    provider = _StubProvider(response)
    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        now_fn=lambda: _NOW,
    )

    new_decision = await disco.run(channel="feishu", to="ou_xxx")
    assert new_decision is not None

    fetched = pending_store.get_recent("feishu", "ou_xxx", now_ms=_NOW_MS + 1)
    assert fetched is not None
    assert fetched.decision_id == new_decision.decision_id  # not "dec_old"


@pytest.mark.asyncio
async def test_dispatcher_dispatch_options_publishes_outbound(memory_store, pending_store):
    """Discovery menus go to OUTBOUND so the user sees render_menu_markdown
    verbatim (no agent paraphrase). DecisionConsumerAdapter handles the
    pick path independently — see dispatcher.dispatch_options docstring."""
    dispatcher, posted = _wire_dispatcher(lambda: _NOW)
    decision = PendingDecision(
        decision_id="dec_x",
        channel="feishu",
        to="ou_xxx",
        created_at_ms=_NOW_MS,
        options=[
            TaskOption(
                id="opt_a", title="alpha", why="why-a", type="ad_hoc", exec_kind="reply", exec_payload={"prompt": "go"}
            )
        ],
    )

    result = await dispatcher.dispatch_options(decision)
    assert result.delivered is True
    assert result.details["decision_id"] == "dec_x"

    outbound = posted.pop(0)
    assert outbound.source.channel == "feishu"
    assert outbound.source.chat_id == "ou_xxx"
    assert outbound.source.extras["_sentinel_origin"] is True
    assert outbound.source.extras["_sentinel_decision_id"] == "dec_x"
    assert "alpha" in outbound.content


@pytest.mark.asyncio
async def test_dispatcher_dispatch_options_rejects_empty():
    dispatcher, _posted = _wire_dispatcher(lambda: _NOW)
    decision = PendingDecision(
        decision_id="dec_x",
        channel="feishu",
        to="ou_xxx",
        created_at_ms=_NOW_MS,
        options=[],
    )
    result = await dispatcher.dispatch_options(decision)
    assert result.delivered is False
    assert result.reason == "empty_options"
