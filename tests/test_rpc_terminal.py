"""Tests for ``terminal.resize`` RPC handler.

The handler records the latest ``cols`` / ``rows`` and always returns
``{ok: true}`` regardless of param shape — SIGWINCH bursts must never
produce error frames.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import terminal as terminal_mod
from raven.rpc.methods.terminal import (
    register_terminal_methods,
    terminal_resize,
)


@pytest.fixture(autouse=True)
def _reset_terminal_state() -> None:
    """Reset module-level latest-size state between tests."""
    terminal_mod._LATEST_COLS = None
    terminal_mod._LATEST_ROWS = None
    yield
    terminal_mod._LATEST_COLS = None
    terminal_mod._LATEST_ROWS = None


async def test_terminal_resize_records_cols_and_rows() -> None:
    result = await terminal_resize({"cols": 120, "rows": 40})
    assert result == {"ok": True}
    assert terminal_mod._LATEST_COLS == 120
    assert terminal_mod._LATEST_ROWS == 40


async def test_terminal_resize_partial_payload_only_records_provided_dims() -> None:
    await terminal_resize({"cols": 80})
    assert terminal_mod._LATEST_COLS == 80
    assert terminal_mod._LATEST_ROWS is None

    await terminal_resize({"rows": 24})
    # cols persists across calls.
    assert terminal_mod._LATEST_COLS == 80
    assert terminal_mod._LATEST_ROWS == 24


async def test_terminal_resize_rejects_non_positive_and_bool() -> None:
    """Bogus dims are silently dropped — the handler must never raise on
    surprising payloads from a SIGWINCH burst."""
    # Booleans are subclass of int but should be ignored.
    result = await terminal_resize({"cols": True, "rows": False})
    assert result == {"ok": True}
    assert terminal_mod._LATEST_COLS is None
    assert terminal_mod._LATEST_ROWS is None

    # Zero / negative dims are also rejected.
    await terminal_resize({"cols": 0, "rows": -1})
    assert terminal_mod._LATEST_COLS is None
    assert terminal_mod._LATEST_ROWS is None


async def test_terminal_resize_accepts_empty_params() -> None:
    result = await terminal_resize({})
    assert result == {"ok": True}


async def test_terminal_resize_via_dispatcher_does_not_emit_error() -> None:
    """End-to-end: the handler is reachable through the Dispatcher and the
    response frame carries a ``result`` key (no ``error``)."""
    d = Dispatcher()
    register_terminal_methods(d)
    resp = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "terminal.resize",
            "params": {"cols": 100, "rows": 30},
        }
    )
    assert "error" not in resp
    assert resp["result"] == {"ok": True}


class StubTerminalHost:
    """In-memory C0-shaped host for testing the transport without a PTY."""

    def __init__(self):
        self.records = {}
        self.write = AsyncMock()
        self.resize = AsyncMock()
        self.close = AsyncMock()

    async def create(self, *, worktree_id, command, title, owner):
        index = len(self.records)
        record = SimpleNamespace(
            handle=f"term_{index}",
            incarnation_id=f"incarnation-{index}",
            pty_id=f"{worktree_id}@@{index:08x}",
            tab_id=f"tab-{index}",
            leaf_id=f"leaf-{index}",
            pane_key=f"tab-{index}:leaf-{index}",
            worktree_id=worktree_id,
            worktree_path=worktree_id.split("::", 1)[1],
            execution_host_id="local",
            title=title,
            status="unknown",
            liveness="live",
            connected=True,
            writable=True,
            orphaned=False,
            visible=False,
            owner=owner,
            last_output_at=None,
        )
        self.records[record.handle] = record
        return record

    def list(self, worktree_id=None):
        return [r for r in self.records.values() if worktree_id is None or r.worktree_id == worktree_id]

    def show(self, handle):
        return self.records[handle]


@pytest.fixture
def hosted_rpc():
    host = StubTerminalHost()
    delivery = SimpleNamespace(send=AsyncMock(), wait=AsyncMock())
    dispatcher = Dispatcher()
    register_terminal_methods(dispatcher, host=host, delivery=delivery)
    return dispatcher, host, delivery


async def call_terminal(dispatcher, method, **params):
    return await dispatcher.dispatch({"jsonrpc": "2.0", "id": 42, "method": f"terminal.{method}", "params": params})


async def test_hosted_create_show_and_invisible_topology(hosted_rpc):
    dispatcher, host, _ = hosted_rpc
    created = await call_terminal(dispatcher, "create", worktree_id="repo::/tmp/task", command="codex", title="worker")
    record = created["result"]["terminal"]
    assert record["incarnationId"] == "incarnation-0"
    shown = await call_terminal(dispatcher, "show", handle=record["handle"])
    assert shown["result"]["terminal"] == record
    listed = await call_terminal(dispatcher, "list", worktree_id="repo::/tmp/task", include_visual_layouts=True)
    result = listed["result"]
    assert result["terminals"] == [record]
    assert record["connected"] is True and record["visible"] is False
    assert result["truncated"] is False
    assert result["hostScope"] == {"hostIds": ["local"], "omittedHostIds": []}
    assert isinstance(result["topologyRevisions"]["repo::/tmp/task"], int)
    assert result["visualLayouts"][0]["root"]["tabs"][0]["panes"]["handle"] == record["handle"]


async def test_hosted_list_reports_truncation_and_scope(hosted_rpc):
    dispatcher, host, _ = hosted_rpc
    for worktree in ["repo::/tmp/task", "repo::/tmp/task", "repo::/tmp/other"]:
        await host.create(worktree_id=worktree, command="codex", title="worker", owner="human")
    result = (await call_terminal(dispatcher, "list", worktree_id="repo::/tmp/task", limit=1))["result"]
    assert len(result["terminals"]) == 1
    assert result["truncated"] is True
    assert "visualLayouts" not in result
    assert set(result["topologyRevisions"]) == {"repo::/tmp/task"}


async def test_hosted_send_wait_input_and_resize_use_host(hosted_rpc):
    dispatcher, host, delivery = hosted_rpc
    await host.create(worktree_id="repo::/tmp/task", command="codex", title="worker", owner="human")
    delivery.send.return_value = {"handle": "term_0", "accepted": True, "state": "accepted"}
    delivery.wait.return_value = {"satisfied": True}
    assert (await call_terminal(dispatcher, "send", handle="term_0", text="hello", enter=True))["result"]["send"][
        "accepted"
    ]
    delivery.send.assert_awaited_once_with("term_0", "hello", enter=True, require_ack=False)
    assert (await call_terminal(dispatcher, "wait", handle="term_0", **{"for": "tui-idle"}))["result"]["wait"][
        "satisfied"
    ]
    await call_terminal(dispatcher, "input", handle="term_0", data="a\r")
    host.write.assert_awaited_once_with("term_0", b"a\r")
    await call_terminal(dispatcher, "resize", handle="term_0", cols=100, rows=30)
    host.resize.assert_awaited_once_with("term_0", 100, 30)


@pytest.mark.parametrize(
    "method,params",
    [
        ("list", {"limit": True}),
        ("list", {"limit": 0}),
        ("send", {"handle": "term_0", "text": 3}),
        ("resize", {"handle": "term_0", "cols": -1, "rows": 24}),
        ("wait", {"handle": "term_0", "for": "unknown"}),
    ],
)
async def test_hosted_invalid_arguments_are_rpc_errors(hosted_rpc, method, params):
    response = await call_terminal(hosted_rpc[0], method, **params)
    assert response["error"]["code"] == -32602


async def test_hosted_missing_terminal_is_named_error(hosted_rpc):
    response = await call_terminal(hosted_rpc[0], "show", handle="missing")
    assert response["error"]["message"] == "terminal_not_found"


async def test_hosted_close_uses_connection_identity_not_caller_owner(hosted_rpc):
    from raven.rpc import connection

    dispatcher, host, _ = hosted_rpc
    token = connection.bind_connection()
    try:
        connection.current_state()["terminal_owner"] = "worker"
        await call_terminal(dispatcher, "close", handle="term_0", owner="human")
        host.close.assert_awaited_once_with("term_0", owner="worker")
    finally:
        connection.unbind_connection(token)


async def test_hosted_rename_is_idempotent_canonical_projection(hosted_rpc):
    dispatcher, host, _ = hosted_rpc
    await host.create(worktree_id="repo::/tmp/task", command="codex", title="worker", owner="human")
    response = await call_terminal(dispatcher, "rename", handle="term_0", title="worker")
    assert response["result"]["rename"]["liveTitle"] == "worker"
    refused = await call_terminal(dispatcher, "rename", handle="term_0", title="different")
    assert refused["error"]["message"] == "title_is_canonical_projection"
    assert host.show("term_0").title == "worker"
