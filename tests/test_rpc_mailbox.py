"""Authenticated receiver RPC rejects identity and method escapes."""

from uuid import uuid4

import pytest
from aiohttp import ClientSession, web

from raven.rpc import connection
from raven.rpc.dispatcher import Dispatcher
from tests.test_mailbox_handoff import handoff as handoff
from tests.test_mailbox_receiver import grant
from tests.test_mailbox_receiver import receivers as receivers
from tests.test_mailbox_store import NOW, wire
from tests.test_mailbox_store import mailbox as mailbox


async def test_receiver_cannot_escape_dispatcher_method_allowlist(receivers):
    service, *_ = receivers
    binding = service.authenticate(grant(receivers)["credential"])
    dispatcher = Dispatcher()

    async def unsafe(params):
        return {"changed": True}

    dispatcher.register("agents.register", unsafe)
    token = connection.bind_connection()
    try:
        connection.current_state()["mailbox_binding_id"] = binding.binding_id
        response = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": "agents.register"})
        assert response.get("error", {}).get("message") == "receiver_method_forbidden"
    finally:
        connection.unbind_connection(token)


@pytest.fixture
def rpc(receivers, monkeypatch):
    from raven.rpc.methods.mailbox import register_mailbox_methods

    monkeypatch.setattr("raven.mailbox.delivery.time.time", lambda: NOW)
    service, refs, *_ = receivers
    dispatcher = Dispatcher()
    methods = register_mailbox_methods(dispatcher, receivers=service)
    binding = service.authenticate(grant(receivers)["credential"])
    return dispatcher, service, refs, binding, methods


async def call(dispatcher, method, params=None, *, binding=None, admin=False):
    token = connection.bind_connection()
    try:
        if binding:
            connection.current_state()["mailbox_binding_id"] = binding.binding_id
        if admin:
            connection.current_state()["mailbox_admin"] = True
        return await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    finally:
        connection.unbind_connection(token)


async def test_unbound_and_declared_surfaces_cannot_issue_credentials(rpc):
    dispatcher, _, refs, _, _ = rpc
    params = {
        "agent_name": "worker",
        "ref": refs[1].model_dump(),
        "scope": {"task_id": "task", "workspace_id": "workspace"},
        "request_id": str(uuid4()),
    }
    response = await call(dispatcher, "mailbox.enroll", params)
    assert response["error"]["data"]["code"] == "receiver_unauthorized"


async def test_schema_one_admin_reads_do_not_upgrade_but_task_creation_does(mailbox, tmp_path):
    from raven.agent.registry.identity import IdentityRegistry
    from raven.mailbox.receiver import ReceiverService
    from raven.rpc.methods.mailbox import register_mailbox_methods
    from tests.test_mailbox_store import SCOPE

    store, sender, _ = mailbox
    registry = IdentityRegistry(tmp_path / "registry.json", config_rows=lambda: [])
    dispatcher = Dispatcher()
    register_mailbox_methods(dispatcher, receivers=ReceiverService(store, registry, upgrade=False))
    for method, params in [("mailbox.bindings", {}), ("mailbox.overview", SCOPE)]:
        response = await call(dispatcher, method, params, admin=True)
        assert response["error"]["data"]["code"] == "receiver_capability_unavailable"
        with store.db.connection() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    response = await call(
        dispatcher,
        "mailbox.handoff.create",
        {**SCOPE, "owner_agent_id": sender.agent_id, "request_id": str(uuid4())},
        admin=True,
    )
    assert response["result"]["data"]["owner_agent_id"] == sender.agent_id
    with store.db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


@pytest.mark.parametrize(
    "forged",
    [{"agent_id": str(uuid4())}, {"task_id": "other"}, {"instance_id": str(uuid4())}, {"binding_id": str(uuid4())}],
)
async def test_receiver_parameters_cannot_override_binding(rpc, forged):
    dispatcher, service, refs, binding, _ = rpc
    sent = service.store.send(wire(*refs), refs[0], now=NOW)
    response = await call(dispatcher, "mailbox.poll", {"request_id": str(uuid4()), **forged}, binding=binding)
    assert "error" in response
    assert service.store.status(refs[1].agent_id, sent["message_id"])["phase"] == "pending"


async def test_poll_ack_and_receipt_reuse_r1_kernel(rpc):
    dispatcher, service, refs, binding, _ = rpc
    sent = service.store.send(wire(*refs), refs[0], now=NOW)
    response = await call(dispatcher, "mailbox.poll", {"request_id": str(uuid4())}, binding=binding)
    claim = response["result"]["data"]["claims"][0]
    response = await call(
        dispatcher,
        "mailbox.ack",
        {"claim": claim, "result": {"outcome": "succeeded", "summary": "Reviewed", "evidence": []}},
        binding=binding,
    )
    assert response["result"]["data"]["phase"] == "completed"
    assert service.store.status(refs[1].agent_id, sent["message_id"])["outcome"] == "succeeded"


async def test_revoked_connected_receiver_cannot_poll(rpc):
    dispatcher, service, _, binding, _ = rpc
    service.revoke(binding.binding_id)
    response = await call(dispatcher, "mailbox.poll", {"request_id": str(uuid4())}, binding=binding)
    assert response["error"]["data"]["code"] == "receiver_revoked"


async def test_status_checks_persisted_message_scope(rpc):
    dispatcher, service, refs, binding, _ = rpc
    sent = service.store.send(wire(*refs, scope={"task_id": "other", "workspace_id": "workspace"}), refs[0], now=NOW)
    response = await call(dispatcher, "mailbox.status", {"message_id": sent["message_id"]}, binding=binding)
    assert response["error"]["data"]["code"] == "scope_denied"


async def test_receiver_websocket_auth_and_broadcast_isolation(rpc):
    import asyncio

    from raven.rpc.transports.ws import WsGateway

    dispatcher, service, refs, _, _ = rpc
    issued = service.grant("worker", refs[1], {"task_id": "task", "workspace_id": "workspace"}, request_id=str(uuid4()))
    gateway = WsGateway()
    gateway.dispatcher = dispatcher
    app = web.Application()
    app.router.add_get("/rpc", gateway.handle_ws)
    app.router.add_post("/auth/nonce", gateway.handle_mint_nonce)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    try:
        async with ClientSession() as client:
            headers = {"X-Raven-Receiver": issued["credential"]}
            async with client.post(url + "/auth/nonce", headers=headers) as denied:
                assert denied.status == 401
            async with client.ws_connect(url + "/rpc", headers=headers) as ws:
                await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "mailbox.poll", "params": {"peek": True}})
                assert (await ws.receive_json())["result"]["data"] == {"messages": []}
                await gateway.broadcast(
                    {"method": "clarify.request", "params": {"conversation_id": "another-task", "secret": "private"}}
                )
                with pytest.raises(asyncio.TimeoutError):
                    await ws.receive(timeout=0.05)
                service.revoke(issued["binding"]["binding_id"])
                await ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "mailbox.poll", "params": {"peek": True}})
                assert (await ws.receive_json())["error"]["data"]["code"] == "receiver_revoked"
    finally:
        await runner.cleanup()


async def test_receiver_socket_auth_and_notification_isolation(rpc):
    import asyncio

    from raven.rpc.server import RpcServer
    from tests.test_rpc_server_socket import _read_one_frame, _send, _wire_paired_socket

    dispatcher, service, refs, _, _ = rpc
    issued = service.grant("worker", refs[1], {"task_id": "task", "workspace_id": "workspace"}, request_id=str(uuid4()))
    listener, client, accepted, temporary = await _wire_paired_socket()
    server = RpcServer(dispatcher, sock=accepted, auth_token="only-the-human-transport")
    task = asyncio.create_task(server.serve_forever())
    try:
        await server.started.wait()
        await asyncio.get_running_loop().sock_sendall(client, (issued["credential"] + "\n").encode())
        await _send(client, "mailbox.poll", {"peek": True}, 1)
        assert b'"messages": []' in await _read_one_frame(client)
        await server.send_frame({"jsonrpc": "2.0", "method": "clarify.request", "params": {"secret": "other-task"}})
        assert await _read_one_frame(client, timeout=0.05) == b""
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        client.close()
        listener.close()
        (temporary / "sock").unlink()
        temporary.rmdir()


async def test_overview_shows_outgoing_accept_only_after_verified_read(handoff, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from raven.agent.registry.identity import IdentityRegistry
    from raven.contracts.terminal import TerminalRecord
    from raven.mailbox.receiver import ReceiverService
    from raven.rpc.methods.mailbox import register_mailbox_methods
    from tests.test_mailbox_handoff import acceptance
    from tests.test_mailbox_store import SCOPE

    monkeypatch.setattr("raven.mailbox.handoff.time.time", lambda: NOW)
    core, store, sender, receiver, offer, sha, _ = handoff
    record = TerminalRecord(worktree_id="workspace", worktree_path=str(tmp_path), liveness="live")
    registry = IdentityRegistry(
        tmp_path / "registry.json", config_rows=lambda: [{"name": "worker", "kind": "cli", "command": "codex"}]
    )
    registry.register("worker", kind_ref="worker", binding=record, task_ref="task")
    service = ReceiverService(store, registry, SimpleNamespace(show=lambda handle: record))
    service.grant("worker", receiver, SCOPE, request_id=str(uuid4()))
    accept_id = acceptance(store, sender, receiver, offer, sha)
    dispatcher = Dispatcher()
    register_mailbox_methods(dispatcher, receivers=service)
    first = await call(dispatcher, "mailbox.overview", SCOPE, admin=True)
    messages = first["result"]["data"]["messages"]
    assert accept_id in [row["message_id"] for row in messages]
    outgoing = next(row for row in messages if row["message_id"] == accept_id)
    assert outgoing["handoff"]["status"] == "accept_received"
    core.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    second = await call(dispatcher, "mailbox.overview", SCOPE, admin=True)
    outgoing = next(row for row in second["result"]["data"]["messages"] if row["message_id"] == accept_id)
    assert outgoing["handoff"]["status"] == "PROPOSED"
    assert outgoing["handoff"]["unresolved_items"] == ["Review output"]


@pytest.mark.parametrize("mode", ["large", "lost"])
async def test_receiver_cli_handles_large_response_and_lost_commit(mode):
    from raven.cli.mailbox_commands import _receiver_rpc
    from raven.contracts.mailbox import MailboxError

    async def receive(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        frame = await ws.receive_json()
        if mode == "large":
            await ws.send_json(
                {"jsonrpc": "2.0", "id": frame["id"], "result": {"data": {"content_base64": "A" * (6 * 1024 * 1024)}}}
            )
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/rpc", receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        credential = {"url": f"http://127.0.0.1:{port}/rpc", "token": "s" * 43}
        if mode == "large":
            result = await _receiver_rpc(credential, "mailbox.artifact.read", {})
            assert len(result["content_base64"]) == 6 * 1024 * 1024
        else:
            with pytest.raises(MailboxError, match="commit_unknown"):
                await _receiver_rpc(credential, "mailbox.poll", {"request_id": str(uuid4())})
    finally:
        await runner.cleanup()


async def test_actual_receiver_cli_uses_only_scoped_credential(rpc, tmp_path):
    import asyncio
    import json
    import sys

    from raven.rpc.transports.ws import WsGateway

    dispatcher, service, refs, _, _ = rpc
    issued = service.grant("worker", refs[1], {"task_id": "task", "workspace_id": "workspace"}, request_id=str(uuid4()))
    gateway = WsGateway()
    gateway.dispatcher = dispatcher
    app = web.Application()
    app.router.add_get("/rpc", gateway.handle_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    credential = tmp_path / "receiver.json"
    credential.write_text(json.dumps({"url": f"http://127.0.0.1:{port}/rpc", "token": issued["credential"]}))
    credential.chmod(0o600)
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"peek": True}))
    params.chmod(0o600)
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "raven",
            "mailbox",
            "rpc",
            "--credential-file",
            str(credential),
            "--params-file",
            str(params),
            "--method",
            "mailbox.poll",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, error.decode()
        assert json.loads(output)["result"] == {"messages": []}
        assert issued["credential"].encode() not in output + error
    finally:
        await runner.cleanup()
