"""Unit tests for NudgeDispatcher.

Verifies the delivery contract:
- Plain nudge posts a spine Text per resolved target via the hub's ``post``
  (never back through a turn, so the agent loop can't "act on" the reminder)
- Anti-cascade flag ``source.extras._sentinel_origin=True`` is set
- Non-nudge actions / empty messages / empty targets are rejected gracefully
- No ``post`` wired (set_post never called) is skipped, not crashed
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from raven.proactive_engine.sentinel.executor.dispatcher import NudgeDispatcher, split_session_key
from raven.proactive_engine.sentinel.types import PlannerDecision
from raven.spine import Text


def _decision(**overrides) -> PlannerDecision:
    defaults = dict(
        action="nudge",
        reason="test",
        priority="low",
        proactivity_score=0.7,
        target_session="cli:direct",
        nudge_message="hello from sentinel",
    )
    defaults.update(overrides)
    return PlannerDecision(**defaults)


def _wire(now_fn=None) -> tuple[NudgeDispatcher, AsyncMock]:
    post = AsyncMock()
    d = NudgeDispatcher(now_fn=now_fn) if now_fn else NudgeDispatcher()
    d.set_post(post)
    return d, post


@pytest.mark.asyncio
async def test_dispatch_plain_nudge_posts_text():
    d, post = _wire()

    result = await d.dispatch(_decision(), [("cli", "direct")])

    assert result.delivered is True
    assert result.reason == "ok"
    assert result.delivery_time is not None
    assert post.await_count == 1

    out = post.await_args.args[0]
    assert isinstance(out, Text)
    assert out.content == "hello from sentinel"
    assert out.source.channel == "cli"
    assert out.source.chat_id == "direct"
    assert out.source.sender_id == "sentinel"
    assert out.source.extras["_sentinel_origin"] is True
    assert out.source.extras["_sentinel_action"] == "nudge"
    assert out.source.extras["_sentinel_priority"] == "low"
    assert out.source.extras["_sentinel_proactivity_score"] == 0.7


@pytest.mark.asyncio
async def test_dispatch_fans_out_to_multiple_targets():
    d, post = _wire()

    result = await d.dispatch(_decision(), [("cli", "direct"), ("feishu", "ou_x")])

    assert result.delivered is True
    assert post.await_count == 2
    channels = {call.args[0].source.channel for call in post.await_args_list}
    assert channels == {"cli", "feishu"}


@pytest.mark.asyncio
async def test_dispatch_rejects_wrong_action():
    d, post = _wire()

    for bad in ("skip", "nudge_inject", "nudge_defer", "spawn_agent"):
        result = await d.dispatch(_decision(action=bad, nudge_message="x"), [("cli", "direct")])
        assert result.delivered is False
        assert bad in result.reason
    assert post.await_count == 0


@pytest.mark.asyncio
async def test_dispatch_rejects_empty_message():
    d, post = _wire()
    result = await d.dispatch(_decision(nudge_message=None), [("cli", "direct")])
    assert result.delivered is False
    assert result.reason == "empty_message"
    assert post.await_count == 0


@pytest.mark.asyncio
async def test_dispatch_rejects_no_targets():
    d, post = _wire()
    result = await d.dispatch(_decision(), [])
    assert result.delivered is False
    assert result.reason == "no_targets"
    assert post.await_count == 0


@pytest.mark.asyncio
async def test_dispatch_without_post_skips():
    d = NudgeDispatcher()
    result = await d.dispatch(_decision(), [("cli", "direct")])
    assert result.delivered is False
    assert result.reason == "no_post"


@pytest.mark.asyncio
async def test_dispatch_injects_now_fn():
    fixed = datetime(2026, 4, 21, 14, 0, 0)
    d, _ = _wire(now_fn=lambda: fixed)
    result = await d.dispatch(_decision(), [("cli", "direct")])
    assert result.delivery_time == fixed


def test_split_session_key_well_formed():
    assert split_session_key("telegram:home") == ("telegram", "home")
    assert split_session_key("cli:direct") == ("cli", "direct")
    # Channel names rarely have colons but chat_ids might include them.
    assert split_session_key("channel:chat:with:colons") == ("channel", "chat:with:colons")


def test_split_session_key_malformed():
    assert split_session_key("") == ("sentinel", "direct")
    assert split_session_key("bare_name") == ("sentinel", "bare_name")
