"""Tests for ``terminal.resize`` RPC handler.

The handler records the latest ``cols`` / ``rows`` and always returns
``{ok: true}`` regardless of param shape — SIGWINCH bursts must never
produce error frames.
"""

from __future__ import annotations

import pytest

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods import terminal as terminal_mod
from raven.tui_rpc.methods.terminal import (
    get_latest_cols,
    get_latest_rows,
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
    assert get_latest_cols() == 120
    assert get_latest_rows() == 40


async def test_terminal_resize_partial_payload_only_records_provided_dims() -> None:
    await terminal_resize({"cols": 80})
    assert get_latest_cols() == 80
    assert get_latest_rows() is None

    await terminal_resize({"rows": 24})
    # cols persists across calls.
    assert get_latest_cols() == 80
    assert get_latest_rows() == 24


async def test_terminal_resize_rejects_non_positive_and_bool() -> None:
    """Bogus dims are silently dropped — the handler must never raise on
    surprising payloads from a SIGWINCH burst."""
    # Booleans are subclass of int but should be ignored.
    result = await terminal_resize({"cols": True, "rows": False})
    assert result == {"ok": True}
    assert get_latest_cols() is None
    assert get_latest_rows() is None

    # Zero / negative dims are also rejected.
    await terminal_resize({"cols": 0, "rows": -1})
    assert get_latest_cols() is None
    assert get_latest_rows() is None


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
