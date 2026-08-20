"""Tests for hermes-only stub RPC handlers.

All 6 stub groups (10 actual method names) return JSON-RPC error -32012
``not_supported_in_v01`` with a structured error message. The dispatcher
serializes ``NotSupportedInV01Error`` to a ``{code, message, data}`` frame.
"""

from __future__ import annotations

import pytest

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.errors import NotSupportedInV01Error
from raven.tui_rpc.methods._stubs import (
    HERMES_ONLY_STUB_METHODS,
    register_stub_methods,
)

# (method_name, expected_message_substring, expected_hint_present)
_STUB_CASES = [
    # Original 6-group hermes-only stubs (10 names)
    ("voice.toggle", "voice not supported", False),
    ("voice.record", "voice recording not supported", False),
    ("browser.manage", "browser automation not supported", False),
    ("process.stop", "process.stop not supported; use Ctrl+C", True),
    ("rollback.list", "filesystem-level rollback not supported", False),
    ("rollback.diff", "filesystem-level rollback not supported", False),
    ("rollback.restore", "filesystem-level rollback not supported", False),
    ("spawn_tree.save", "spawn_tree topology not supported", False),
    ("spawn_tree.list", "spawn_tree topology not supported", False),
    ("spawn_tree.load", "spawn_tree topology not supported", False),
    ("tools.configure", "tools configuration via TUI not supported", False),
    # session.* slash-command stubs.
    # session.branch promoted to a real handler in methods/session.py
    # (session fork) — see test_tui_rpc_session.py::test_session_branch_*
    ("session.compress", "session.compress not supported", True),
    ("session.save", "session.save not supported", True),
    # session.status promoted to real handler in slash_routing.py —
    # see test_tui_rpc_slash_routing.py::test_session_status_*
    ("session.steer", "session.steer not supported", True),
    # session.title / session.undo promoted to real handlers in methods/session.py
    # (parity tests live in test_tui_rpc_session.py)
    ("session.usage", "session.usage not supported", True),
    # skills.reload
    ("skills.reload", "skills.reload not supported", True),
    # reload.env
    ("reload.env", "reload.env not supported", True),
    # approval / sudo / secret response (3)
    ("approval.respond", "approval.respond not supported", True),
    ("sudo.respond", "sudo.respond not supported", True),
    ("secret.respond", "secret.respond not supported", True),
    # NOTE: ``commands.catalog`` was promoted to a real handler in
    # ``raven.tui_rpc.methods.commands`` (harness-command-catalog-dynamic);
    # its parity test is ``tests/test_tui_rpc_commands_catalog.py``.
    # image.attach
    ("image.attach", "image.attach not supported", True),
    # prompt.submit / prompt.background (2)
    ("prompt.submit", "prompt.submit not supported", True),
    ("prompt.background", "prompt.background not supported", True),
]


def test_stub_registry_lists_all_method_names() -> None:
    """The exported list must enumerate every stub method name; this guards
    against accidental drift between registry and dispatcher wiring."""
    expected = {case[0] for case in _STUB_CASES}
    assert set(HERMES_ONLY_STUB_METHODS) == expected


@pytest.mark.parametrize("method,message_substr,has_hint", _STUB_CASES)
async def test_stub_raises_not_supported_with_expected_message(
    method: str, message_substr: str, has_hint: bool
) -> None:
    d = Dispatcher()
    register_stub_methods(d)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})
    assert "error" in resp, f"{method} should emit an error frame, got {resp}"
    err = resp["error"]
    assert err["code"] == -32012
    assert err["message"] == "not_supported_in_v01"
    # The structured "error" string lives under data.error per the stub contract.
    data = err.get("data", {})
    assert "error" in data, f"{method} stub data must include 'error' key: {data}"
    assert message_substr.lower() in data["error"].lower()
    if has_hint:
        assert "hint" in data, f"{method} stub must include 'hint' key"
        assert data["hint"]


@pytest.mark.parametrize("method,_msg,_hint", _STUB_CASES)
async def test_stub_handler_callable_raises_directly(method: str, _msg: str, _hint: bool) -> None:
    """Calling the registered handler directly raises the typed exception so
    other tests / future callers can assert exception type rather than parsing
    the JSON-RPC error frame."""
    d = Dispatcher()
    register_stub_methods(d)
    handler = d._handlers[method]  # type: ignore[attr-defined]
    with pytest.raises(NotSupportedInV01Error):
        await handler({})


def test_no_per_task_progress_rpc_method() -> None:
    """R7 negative pin: the full production dispatcher exposes NO per-task
    "progress" RPC method (spawn_tree topology is stub-only). Wiring a real
    ``task.progress`` / ``spawn_tree.progress`` endpoint later must break here.
    """
    from raven.tui_rpc.methods import register_aligned_methods

    d = Dispatcher()
    register_aligned_methods(d)
    progress_methods = [m for m in d.methods() if "progress" in m.lower()]
    assert progress_methods == [], f"unexpected progress RPC method(s): {progress_methods}"


def test_process_stop_is_not_supported_stub_exact_shape() -> None:
    """R8 pin: single-task stop (``process.stop``) is a not-supported stub, not a
    real endpoint. Assert the EXACT error/hint shape so promoting it to a real
    per-task stop handler breaks this test."""
    assert "process.stop" in HERMES_ONLY_STUB_METHODS

    from raven.tui_rpc.methods import register_aligned_methods

    d = Dispatcher()
    register_aligned_methods(d)
    assert "process.stop" in d.methods()


async def test_process_stop_stub_error_and_hint_exact() -> None:
    d = Dispatcher()
    register_stub_methods(d)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "process.stop", "params": {}})
    err = resp["error"]
    assert err["code"] == -32012
    assert err["message"] == "not_supported_in_v01"
    assert err["data"]["error"] == "process.stop not supported; use Ctrl+C"
    assert err["data"]["hint"] == "Press Ctrl+C in the TUI to interrupt a running turn."


async def test_stub_calls_do_not_mutate_dispatcher_state() -> None:
    """Calling stubs repeatedly must not corrupt subsequent dispatches."""
    d = Dispatcher()
    register_stub_methods(d)

    async def echo(params: dict) -> dict:
        return {"echoed": params}

    d.register("test.echo", echo)
    for _ in range(5):
        resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "voice.toggle", "params": {}})
        assert resp["error"]["code"] == -32012
    # The "real" handler still works after a flurry of stub calls.
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 99, "method": "test.echo", "params": {"x": 1}})
    assert resp["result"] == {"echoed": {"x": 1}}
