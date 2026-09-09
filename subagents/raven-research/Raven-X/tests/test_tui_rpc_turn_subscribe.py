"""Tests for ``turn.subscribe`` + ``turn.unsubscribe`` real handlers
(turn-streaming).

Relevant models live in ``raven/tui_rpc/models.py``
(``TurnSubscribe*`` / ``TurnUnsubscribe*``); the handlers live in
``raven.tui_rpc.methods.turn`` + ``raven.tui_rpc.subscriptions``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods.turn import (
    register_turn_methods,
    turn_subscribe,
    turn_unsubscribe,
)
from raven.tui_rpc.subscriptions import SubscriptionEmitter


@pytest.fixture
def emitter() -> SubscriptionEmitter:
    """Fresh emitter with a no-op send_frame (we test handler return shapes only,
    not notification dispatch in this file — see test_subscription_emitter.py)."""
    return SubscriptionEmitter(send_frame=AsyncMock(return_value=None))


@pytest.fixture
def dispatcher(emitter: SubscriptionEmitter) -> Dispatcher:
    d = Dispatcher()
    register_turn_methods(d, emitter=emitter)
    return d


# ---------------------------------------------------------------------------
# turn.subscribe
# ---------------------------------------------------------------------------


async def test_turn_subscribe_returns_subscription_id(
    emitter: SubscriptionEmitter,
) -> None:
    """``turn.subscribe`` with valid session_key returns ``{subscription_id}``."""
    result = await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)

    assert set(result) == {"subscription_id"}
    assert isinstance(result["subscription_id"], str)
    assert len(result["subscription_id"]) >= 16  # uuid hex


async def test_turn_subscribe_multiple_subscribers_same_session(
    emitter: SubscriptionEmitter,
) -> None:
    """Same session_key can have multiple concurrent subscribers (different sub_ids)."""
    r1 = await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    r2 = await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    assert r1["subscription_id"] != r2["subscription_id"]


async def test_turn_subscribe_rejects_missing_session_key(
    emitter: SubscriptionEmitter,
) -> None:
    """Missing required ``session_key`` → validation error."""
    with pytest.raises(Exception):  # noqa: BLE001
        await turn_subscribe({}, emitter=emitter)


# ---------------------------------------------------------------------------
# turn.unsubscribe
# ---------------------------------------------------------------------------


async def test_turn_unsubscribe_existing_returns_true(
    emitter: SubscriptionEmitter,
) -> None:
    """Unsubscribe an active subscription → ``{unsubscribed: True}``."""
    sub = await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    sub_id = sub["subscription_id"]

    result = await turn_unsubscribe({"subscription_id": sub_id}, emitter=emitter)
    assert result == {"unsubscribed": True}


async def test_turn_unsubscribe_unknown_returns_false_idempotent(
    emitter: SubscriptionEmitter,
) -> None:
    """Unsubscribe with unknown subscription_id → ``{unsubscribed: False}`` —
    NOT an error (unsubscribe is idempotent).
    """
    result = await turn_unsubscribe({"subscription_id": "nonexistent-sub-id-12345"}, emitter=emitter)
    assert result == {"unsubscribed": False}


async def test_turn_unsubscribe_twice_second_call_returns_false(
    emitter: SubscriptionEmitter,
) -> None:
    """Calling unsubscribe twice on same sub_id: 1st True, 2nd False."""
    sub = await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    sub_id = sub["subscription_id"]

    r1 = await turn_unsubscribe({"subscription_id": sub_id}, emitter=emitter)
    r2 = await turn_unsubscribe({"subscription_id": sub_id}, emitter=emitter)

    assert r1["unsubscribed"] is True
    assert r2["unsubscribed"] is False


# ---------------------------------------------------------------------------
# End-to-end via Dispatcher
# ---------------------------------------------------------------------------


async def test_turn_subscribe_dispatches_via_dispatcher(
    dispatcher: Dispatcher,
) -> None:
    resp = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn.subscribe",
            "params": {"session_key": "tui:default"},
        }
    )
    assert "error" not in resp
    assert set(resp["result"]) == {"subscription_id"}


async def test_turn_unsubscribe_dispatches_via_dispatcher(
    dispatcher: Dispatcher,
) -> None:
    sub_resp = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn.subscribe",
            "params": {"session_key": "tui:default"},
        }
    )
    sub_id = sub_resp["result"]["subscription_id"]

    resp = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "turn.unsubscribe",
            "params": {"subscription_id": sub_id},
        }
    )
    assert "error" not in resp
    assert resp["result"] == {"unsubscribed": True}
