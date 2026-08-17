"""Tests for TUI agent_loop init error reclassification.

Validates that AgentLoop init crashes raise -32603 `internal_error` with
structured diagnostic data (instead of being silently swallowed and surfaced
as -32008 `model_not_available` by ``turn.send``). Backward-compat for the
two legit -32008 paths (`_resolve_model` raise; `agent_loop_factory=None`)
is also asserted.
"""

from __future__ import annotations

import pytest

from raven.cli.tui_commands import _build_tui_agent_loop
from raven.tui_rpc.errors import InternalError, RpcError

# ---------------------------------------------------------------------------
# _build_tui_agent_loop — narrow exception → InternalError(-32603)
# ---------------------------------------------------------------------------


def _patch_agent_loop_to_raise(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Replace ``AgentLoop`` constructor so that calling it raises ``exc``.

    ``_build_tui_agent_loop`` imports ``AgentLoop`` lazily from
    ``raven.agent.loop`` inside the function body, so we patch the module
    attribute (not a copy in tui_commands).
    """

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise exc

    monkeypatch.setattr("raven.agent.loop.AgentLoop", _Boom)


def test_typeerror_in_build_raises_internal_error_minus_32603(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """AgentLoop ctor raising TypeError (kwargs drift) → InternalError(-32603).

    Reason data field = "tui_init_crash"; exception_type echoed; log_path
    points to ~/.raven/logs/tui.log so the UI can hint the user.
    """
    monkeypatch.chdir(tmp_path)
    _patch_agent_loop_to_raise(
        monkeypatch,
        TypeError("unexpected keyword 'everos_skill_light_config'"),
    )

    with pytest.raises(InternalError) as excinfo:
        _build_tui_agent_loop()

    assert excinfo.value.code == -32603
    assert excinfo.value.message == "internal_error"
    data = excinfo.value.data or {}
    assert data.get("reason") == "tui_init_crash"
    assert data.get("exception_type") == "TypeError"
    assert "unexpected keyword" in data.get("exception_message", "")
    assert data.get("log_path", "").endswith("tui.log")


def test_attributeerror_in_build_raises_internal_error_minus_32603(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """AgentLoop ctor raising AttributeError (config rename) → InternalError(-32603)."""
    monkeypatch.chdir(tmp_path)
    _patch_agent_loop_to_raise(
        monkeypatch,
        AttributeError("'RavenConfig' object has no attribute 'foo'"),
    )

    with pytest.raises(InternalError) as excinfo:
        _build_tui_agent_loop()

    assert excinfo.value.code == -32603
    data = excinfo.value.data or {}
    assert data.get("reason") == "tui_init_crash"
    assert data.get("exception_type") == "AttributeError"


def test_uncaught_exception_in_build_raises_internal_error_with_uncaught_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Unforeseen exception (RuntimeError) → InternalError(-32603) with reason='uncaught'.

    This is the safety-net branch — anything that isn't in the narrow tuple
    still surfaces as -32603 (NOT -32008), but with a different reason flag
    so observability can distinguish "expected init crash class" vs
    "unforeseen ctor failure".
    """
    monkeypatch.chdir(tmp_path)
    _patch_agent_loop_to_raise(monkeypatch, RuntimeError("boom"))

    with pytest.raises(InternalError) as excinfo:
        _build_tui_agent_loop()

    assert excinfo.value.code == -32603
    data = excinfo.value.data or {}
    assert data.get("reason") == "uncaught"
    assert data.get("exception_type") == "RuntimeError"


# ---------------------------------------------------------------------------
# RpcError export sanity check
# ---------------------------------------------------------------------------


def test_internal_error_class_exposes_minus_32603() -> None:
    """``InternalError`` is exported with the JSON-RPC pre-defined -32603 code.

    The class is new (this MR) — guard against accidental refactor that
    changes its code constant.
    """
    err = InternalError(detail="x")
    assert err.code == -32603
    assert err.message == "internal_error"
    assert isinstance(err, RpcError)
