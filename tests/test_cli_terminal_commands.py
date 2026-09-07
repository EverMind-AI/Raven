"""CLI argv compatibility and authenticated terminal RPC transport."""

import json
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app


@pytest.mark.parametrize(
    "argv,method,params",
    [
        (
            ["terminal", "list", "--worktree", "id:repo::/tmp/task", "--limit", "1000", "--include-visual-layouts"],
            "terminal.list",
            {"worktree_id": "repo::/tmp/task", "limit": 1000, "include_visual_layouts": True},
        ),
        (["terminal", "show", "--terminal", "term_test"], "terminal.show", {"handle": "term_test"}),
        (
            ["terminal", "send", "--terminal", "term_test", "--text", "hello", "--enter"],
            "terminal.send",
            {"handle": "term_test", "text": "hello", "enter": True, "require_ack": False},
        ),
        (
            ["terminal", "wait", "--terminal", "term_test", "--for", "tui-idle", "--timeout-ms", "2000"],
            "terminal.wait",
            {"handle": "term_test", "for": "tui-idle", "timeout_ms": 2000},
        ),
        (["terminal", "close", "--terminal", "term_test"], "terminal.close", {"handle": "term_test"}),
        (
            ["terminal", "rename", "--terminal", "term_test", "--title", "worker"],
            "terminal.rename",
            {"handle": "term_test", "title": "worker"},
        ),
    ],
)
def test_terminal_argv_uses_rpc_and_preserves_envelope(monkeypatch, argv, method, params):
    from raven.cli import _terminal_rpc

    envelope = {
        "id": "test",
        "ok": True,
        "result": {"terminal": {"handle": "term_test"}},
        "_meta": {"runtimeId": "remote"},
    }
    request = AsyncMock(return_value=envelope)
    monkeypatch.setattr(_terminal_rpc, "request", request)
    result = CliRunner().invoke(app, [*argv, "--environment", "development", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == envelope
    assert request.await_args.args == (method, params)
    assert request.await_args.kwargs["environment"] == "development"


def test_terminal_error_is_json_with_nonzero_exit(monkeypatch):
    from raven.cli import _terminal_rpc

    envelope = {
        "id": "test",
        "ok": False,
        "error": {"code": "agent_prompt_blocked", "message": "permission"},
        "_meta": {"runtimeId": "remote"},
    }
    monkeypatch.setattr(_terminal_rpc, "request", AsyncMock(return_value=envelope))
    result = CliRunner().invoke(
        app, ["terminal", "send", "--terminal", "term_test", "--text", "hello", "--enter", "--json"]
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout) == envelope


def test_native_host_reply_never_requires_a_terminal_handle(monkeypatch):
    from raven.cli import _terminal_rpc

    request = AsyncMock(return_value={"ok": True, "result": {"send": {"state": "delivered_to_host"}}})
    monkeypatch.setattr(_terminal_rpc, "request", request)
    monkeypatch.setenv("RAVEN_TERMINAL_HANDLE", "term_sender")
    result = CliRunner().invoke(
        app, ["terminal", "send", "--to", "raven", "--text", "ack_for=a2a-123456789abc received", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert request.await_args.args[1]["to"] == "raven"
    assert request.await_args.args[1]["source_handle"] == "term_sender"
    assert "handle" not in request.await_args.args[1]


def test_terminal_long_text_warns_without_refusing(monkeypatch):
    from raven.cli import _terminal_rpc

    monkeypatch.setattr(_terminal_rpc, "request", AsyncMock(return_value={"ok": True}))
    result = CliRunner(mix_stderr=False).invoke(
        app, ["terminal", "send", "--terminal", "term_test", "--text", "a" * 1300, "--json"]
    )
    assert result.exit_code == 0
    assert "file" in result.stderr
    assert json.loads(result.stdout)["ok"] is True


async def test_rpc_transport_reads_token_and_matches_runtime(monkeypatch, tmp_path):
    from aiohttp import web

    from raven.cli import _terminal_rpc

    seen = []

    async def ws_handler(request):
        assert request.headers["X-Raven-Token"] == "test-private-token"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            frame = json.loads(message.data)
            seen.append(frame)
            if frame["method"] == "runtime.status":
                result = {
                    "runtime": {"state": "ready", "runtimeId": "remote", "environment": "development"},
                    "graph": {"state": "ready"},
                }
            else:
                result = {"terminal": {"handle": "term_test"}}
            await ws.send_json({"jsonrpc": "2.0", "method": "event", "params": {}})
            await ws.send_json(
                {"jsonrpc": "2.0", "id": frame["id"], "result": result, "_meta": {"runtimeId": "remote"}}
            )
        return ws

    server = web.Application()
    server.router.add_get("/rpc", ws_handler)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    metadata = tmp_path / "serve.json"
    metadata.write_text(json.dumps({"port": port, "token": "test-private-token"}))
    monkeypatch.setattr(_terminal_rpc, "state_path", lambda: metadata)
    try:
        result = await _terminal_rpc.request("terminal.show", {"handle": "term_test"}, environment="development")
        assert result["ok"] is True
        assert result["_meta"]["runtimeId"] == "remote"
        assert result["result"]["terminal"]["handle"] == "term_test"
        assert [f["method"] for f in seen] == ["runtime.status", "terminal.show"]
    finally:
        await runner.cleanup()


def test_runtime_identity_change_and_named_error_are_preserved():
    from raven.cli._terminal_rpc import _TransportError, _wrap

    with pytest.raises(_TransportError, match="identity changed"):
        _wrap({"id": "test", "result": {}, "_meta": {"runtimeId": "new"}}, "old")
    frame = {
        "id": "test",
        "error": {
            "code": -32099,
            "message": "agent_identity_error",
            "data": {"code": "agent_not_found", "message": "No exact name"},
        },
        "_meta": {"runtimeId": "remote"},
    }
    result = _wrap(frame, "remote")
    assert result["error"]["code"] == "agent_not_found"
    assert result["error"]["message"] == "No exact name"


async def test_missing_serve_metadata_returns_json_without_token(monkeypatch, tmp_path):
    from raven.cli import _terminal_rpc

    monkeypatch.setattr(_terminal_rpc, "state_path", lambda: tmp_path / "missing")
    result = await _terminal_rpc.request("terminal.show", {"handle": "term_test"})
    assert result["ok"] is False
    assert result["error"]["code"] == "runtime_unavailable"
    assert result["_meta"]["runtimeId"] is None
