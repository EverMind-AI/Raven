"""The seam between ``turn.send`` and the session namer.

The unit tests around ``name_session_alongside_turn`` pass its arguments in
directly, so they say nothing about whether ``turn.send`` can assemble those
arguments. It could not: the first version read the settings off the base
``Config``, which has no attribute for them, and the broad except in
``_name_session`` turned the AttributeError into silence. The feature shipped
and never ran once.

These tests exercise that assembly, which is the only place the wiring exists.
"""

from __future__ import annotations

from typing import Any

import pytest

from raven.rpc.methods import session as session_module
from raven.rpc.methods import turn as turn_module
from raven.rpc.models import TurnSendParams


class _Loop:
    provider = object()
    sessions = None


def _use_loop(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """Stand in for the two helpers ``_name_session`` imports from session.py.

    Patched where they are defined rather than on ``turn``: that function
    imports them at call time, so ``turn`` never carries them as attributes.
    """
    monkeypatch.setattr(session_module, "_safe_invoke_factory", lambda _factory: loop)
    monkeypatch.setattr(session_module, "_manager_for", lambda _loop, _config: object())


@pytest.fixture
def parsed() -> TurnSendParams:
    return TurnSendParams.model_validate(
        {"session_key": "tui:20260610_100000_wiring", "content": "please cut a desktop release"}
    )


def test_turn_send_hands_the_namer_the_configured_settings(
    parsed: TurnSendParams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assembly must survive a real config read.

    Asserting on the values rather than on 'it did not raise': the failure this
    covers was a silent skip, which a call-count-free test would have passed.
    """
    seen: dict[str, Any] = {}

    def _record(**kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr("raven.rpc.session_naming.name_session_alongside_turn", _record)
    _use_loop(monkeypatch, _Loop())

    turn_module._name_session(parsed, agent_loop_factory=lambda: _Loop(), emitter=None)

    assert seen, "the namer was never reached -- turn.send could not assemble its arguments"
    assert seen["session_key"] == "tui:20260610_100000_wiring"
    assert seen["text"] == "please cut a desktop release"
    assert seen["provider"] is not None
    # Straight off SessionTitleConfig. A wrong path to those settings cannot
    # produce these values, which is what makes this test load-bearing.
    assert seen["enabled"] is True
    assert seen["budget"] == 24
    assert seen["min_input_chars"] == 8
    assert seen["timeout_seconds"] == 8.0


def test_turn_send_skips_naming_when_no_agent_loop_is_running(
    parsed: TurnSendParams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lazy gateway has no provider to call, and that is a refusal, not a fault."""
    calls: list[Any] = []
    monkeypatch.setattr("raven.rpc.session_naming.name_session_alongside_turn", lambda **k: calls.append(k))
    _use_loop(monkeypatch, None)

    turn_module._name_session(parsed, agent_loop_factory=lambda: None, emitter=None)

    assert calls == []


def test_a_broken_seam_is_reported_with_its_cause(parsed: TurnSendParams, monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming must never break a turn, but a broken seam must be readable.

    Two ways to fail this, and the first version failed both in turn. It logged
    at debug, so a feature that never ran once looked identical to one with
    nothing to name. Then it raised the level and passed `exc_info=True` -- a
    stdlib kwarg loguru files under `record["extra"]`, which the sinks in
    cli/_log_file.py never format -- so the warning arrived with no exception
    type, message or frame: less than the debug line it replaced.

    So this asserts the cause is rendered, not merely that a warning happened.
    """
    from loguru import logger

    rendered: list[str] = []
    levels: list[str] = []

    def _sink(message: Any) -> None:
        rendered.append(str(message))
        levels.append(message.record["level"].name)

    sink_id = logger.add(_sink, level="WARNING")
    try:

        def _boom(**_kwargs: Any) -> None:
            raise RuntimeError("the seam is broken")

        monkeypatch.setattr("raven.rpc.session_naming.name_session_alongside_turn", _boom)
        _use_loop(monkeypatch, _Loop())

        turn_module._name_session(parsed, agent_loop_factory=lambda: _Loop(), emitter=None)
    finally:
        logger.remove(sink_id)

    assert "WARNING" in levels, "a broken seam left no trace above debug"
    joined = "\n".join(rendered)
    assert "RuntimeError" in joined, f"the warning carried no exception type: {joined!r}"
    assert "the seam is broken" in joined, f"the warning carried no cause: {joined!r}"
