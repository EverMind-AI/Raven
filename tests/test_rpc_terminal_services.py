"""Native-host reply notes and terminal service lifecycle integration."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from raven.agent.registry.identity import IdentityRegistry
from raven.contracts.terminal import Envelope, TerminalRecord
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.subscriptions import SubscriptionEmitter
from raven.rpc.terminal_services import TerminalServices
from raven.session.manager import SessionManager
from raven.terminal.deliver import DeliveryService, PendingSend


@pytest.mark.parametrize("matched", [True, False])
async def test_native_reply_is_a_persisted_note_without_pty_input(tmp_path, matched):
    record = TerminalRecord(worktree_id=f"repo::{tmp_path}", worktree_path=str(tmp_path))
    host = SimpleNamespace(show=lambda _: record, write=AsyncMock(), input=AsyncMock())
    identity = IdentityRegistry(
        tmp_path / "identities.json", config_rows=lambda: [{"name": "Raven", "kind": "builtin"}]
    )
    delivery = DeliveryService(host)
    future = asyncio.get_running_loop().create_future()
    if matched:
        envelope = Envelope(sender="raven", recipient="worker", scope="local/development", body="Read this task")
        envelope.nonce = "a2a-123456789abc"
        delivery.pending[envelope.nonce] = PendingSend(envelope, record.handle, future, time.monotonic())
    emitter = SubscriptionEmitter(AsyncMock())
    service = TerminalServices(emitter, AsyncMock(), host=host, delivery=delivery, identities=identity)
    service.sessions = SessionManager(tmp_path)
    service.bind_session(record.handle, "tui:task")
    dispatcher = Dispatcher()
    service.register(dispatcher)
    response = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "reply",
            "method": "terminal.send",
            "params": {
                "to": "raven",
                "source_handle": record.handle,
                "text": "ack_for=a2a-123456789abc received",
            },
        }
    )
    from raven.rpc.terminal_models import TerminalSendResult

    TerminalSendResult.model_validate(response["result"])
    result = response["result"]["send"]
    assert result["state"] == "delivered_to_host"
    assert result["contentAck"] is matched
    assert future.done() is matched
    if matched:
        assert future.result()["ack_for"] == "a2a-123456789abc"
    host.write.assert_not_awaited()
    host.input.assert_not_awaited()
    session = service.sessions.get_or_create("tui:task")
    assert session.messages[-1]["notice"]["kind"] == "terminal_reply"
    assert "UNTRUSTED" in session.messages[-1]["content"]
    assert identity.show("raven").binding.handle is None


async def test_ambiguous_terminal_target_is_rejected_before_delivery():
    from raven.rpc.methods.terminal import register_terminal_methods

    dispatcher = Dispatcher()
    receiver = AsyncMock()
    register_terminal_methods(dispatcher, host=object(), receive_host=receiver)
    response = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "reply",
            "method": "terminal.send",
            "params": {
                "to": "raven",
                "handle": "term_test",
                "text": "hello",
            },
        }
    )
    assert response["error"]["code"] == -32602
    receiver.assert_not_awaited()
