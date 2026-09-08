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
    state = SimpleNamespace(record=record, composer_dirty=True)
    host = SimpleNamespace(show=lambda _: record, state=lambda _: state, write=AsyncMock(), input=AsyncMock())
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
    service.sessions.get_or_create("tui:task")
    service.bind_session(record.handle, "tui:task")
    service.bind_session(record.handle, "cli-launch-rsi")
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
    assert state.composer_dirty is not matched
    if matched:
        assert future.result()["ack_for"] == "a2a-123456789abc"
    host.write.assert_not_awaited()
    host.input.assert_not_awaited()
    session = service.sessions.get_or_create("tui:task")
    assert session.messages[-1]["notice"]["kind"] == "terminal_reply"
    assert "UNTRUSTED" in session.messages[-1]["content"]
    assert identity.show("raven").binding.handle is None
    assert service.sessions.peek("cli-launch-rsi") is None


async def test_native_reply_from_the_receiving_terminal_acks_without_quoting_the_nonce(tmp_path):
    record = TerminalRecord(worktree_id=f"repo::{tmp_path}", worktree_path=str(tmp_path))
    state = SimpleNamespace(record=record, composer_dirty=True)
    host = SimpleNamespace(show=lambda _: record, state=lambda _: state, write=AsyncMock(), input=AsyncMock())
    identity = IdentityRegistry(
        tmp_path / "identities.json", config_rows=lambda: [{"name": "Raven", "kind": "builtin"}]
    )
    delivery = DeliveryService(host)
    future = asyncio.get_running_loop().create_future()
    envelope = Envelope(sender="raven", recipient="worker", scope="local/development", body="Read this task")
    delivery.pending[envelope.nonce] = PendingSend(envelope, record.handle, future, time.monotonic())
    service = TerminalServices(
        SubscriptionEmitter(AsyncMock()), AsyncMock(), host=host, delivery=delivery, identities=identity
    )
    service.sessions = SessionManager(tmp_path)
    service.sessions.get_or_create("tui:task")
    service.bind_session(record.handle, "tui:task")
    result = await service.receive_host("started on it", record.handle)
    assert result["contentAck"] is True
    assert future.result()["ack_for"] == envelope.nonce
    assert not state.composer_dirty


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


@pytest.mark.parametrize("fallback", ["valid", "stale", "missing", "ambiguous"])
async def test_host_reply_uses_identity_session_without_creating_sessions(tmp_path, fallback):
    from raven.contracts.terminal import TerminalError

    record = TerminalRecord(worktree_id=f"repo::{tmp_path}", worktree_path=str(tmp_path))
    binding = SimpleNamespace(handle=record.handle, incarnation_id=record.incarnation_id)
    if fallback == "stale":
        binding.incarnation_id = "old"
    identities = [SimpleNamespace(binding=binding, session_key="tui:creator")]
    if fallback == "ambiguous":
        identities.append(SimpleNamespace(binding=binding, session_key="tui:other"))
    registry = SimpleNamespace(ensure_host=lambda: None, reconcile=lambda: None, list=lambda: identities)
    host = SimpleNamespace(show=lambda _: record)
    delivery = SimpleNamespace(receive_host=AsyncMock(return_value=None))
    emitter = SimpleNamespace(emit=AsyncMock())
    service = TerminalServices(emitter, AsyncMock(), host=host, delivery=delivery, identities=registry)
    service.sessions = SessionManager(tmp_path)
    if fallback != "missing":
        service.sessions.get_or_create("tui:creator")
    service.conversations["unrelated-terminal"] = "cli-launch-rsi"
    if fallback == "valid":
        assert (await service.receive_host("reply", record.handle))["state"] == "delivered_to_host"
        assert service.sessions.peek("tui:creator").messages[-1]["notice"]["kind"] == "terminal_reply"
        assert emitter.emit.await_args.args[0] == "tui:creator"
    else:
        with pytest.raises(TerminalError) as error:
            await service.receive_host("reply", record.handle)
        assert error.value.code == "host_conversation_not_found"
        emitter.emit.assert_not_awaited()
    assert service.sessions.peek("cli-launch-rsi") is None
    if fallback == "missing":
        assert service.sessions.peek("tui:creator") is None


async def test_host_reply_prefers_sender_over_ack_matched_terminal(tmp_path):
    host = SimpleNamespace(show=lambda handle: None)
    registry = SimpleNamespace(ensure_host=lambda: None, reconcile=lambda: None)
    delivery = SimpleNamespace(receive_host=AsyncMock(return_value={"handle": "other"}))
    service = TerminalServices(
        SimpleNamespace(emit=AsyncMock()), AsyncMock(), host=host, delivery=delivery, identities=registry
    )
    service.sessions = SessionManager(tmp_path)
    service.sessions.get_or_create("tui:sender")
    service.sessions.get_or_create("tui:other")
    service.conversations.update(sender="tui:sender", other="tui:other")
    await service.receive_host("reply", "sender")
    assert service.sessions.peek("tui:sender").messages
    assert not service.sessions.peek("tui:other").messages
