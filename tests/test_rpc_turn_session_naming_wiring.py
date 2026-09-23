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

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from raven.rpc.methods import session as session_module
from raven.rpc.methods import turn as turn_module
from raven.rpc.models import TurnSendParams
from raven.session.manager import SessionManager


class _Loop:
    provider = object()
    sessions = None


def _use_loop(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """Stand in for the two helpers ``_name_session`` imports from session.py.

    Patched where they are defined rather than on ``turn``: that function
    imports them at call time, so ``turn`` never carries them as attributes.
    """
    monkeypatch.setattr(session_module, "_safe_invoke_factory", lambda _factory: loop)
    monkeypatch.setattr(session_module, "manager_for", lambda _loop, _config: object())


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
    assert seen["min_input_width"] == 6
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


def test_the_seam_reports_whether_a_namer_started(parsed: TurnSendParams, monkeypatch: pytest.MonkeyPatch) -> None:
    """`turn.send` answers "is a title coming?", and the answer must be the truth.

    This is the whole reason the field exists. A client that cannot ask has only
    one way to find out -- hold a placeholder until a grace period expires --
    which made an opening refused in microseconds the slowest one to show a
    name, and a short "hi" slower than a long request that really did generate.
    """
    monkeypatch.setattr("raven.rpc.session_naming.name_session_alongside_turn", lambda **k: object())
    _use_loop(monkeypatch, _Loop())
    assert turn_module._name_session(parsed, agent_loop_factory=lambda: _Loop(), emitter=None) is True

    monkeypatch.setattr("raven.rpc.session_naming.name_session_alongside_turn", lambda **k: None)
    assert turn_module._name_session(parsed, agent_loop_factory=lambda: _Loop(), emitter=None) is False


def test_a_seam_that_cannot_run_reports_no_naming(parsed: TurnSendParams, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken seam must not claim a title is coming.

    It already swallows its exception so a turn is never broken by naming; if it
    swallowed it and returned nothing, the client would read that as "a name is
    on its way" and wait out the full grace period for an event that cannot come.
    """

    def _explode(**_: object) -> None:
        raise RuntimeError("seam is broken")

    monkeypatch.setattr("raven.rpc.session_naming.name_session_alongside_turn", _explode)
    _use_loop(monkeypatch, _Loop())

    assert turn_module._name_session(parsed, agent_loop_factory=lambda: _Loop(), emitter=None) is False


def test_no_agent_loop_reports_no_naming(parsed: TurnSendParams, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_loop(monkeypatch, None)
    assert turn_module._name_session(parsed, agent_loop_factory=lambda: None, emitter=None) is False


class _Handle:
    async def cancel(self) -> None:
        return None


class _Scheduler:
    def __init__(self) -> None:
        self.submitted: list[Any] = []

    def submit(self, req: Any) -> _Handle:
        self.submitted.append(req)
        return _Handle()


class _FilingEmitter:
    """Files the turn's question the way the submitted worker does.

    ``scheduler.submit`` is synchronous and the emit below is the first await
    after it, so the worker's opening write -- which now happens before its
    first model call -- lands here. What the namer sees on disk therefore
    depends on which side of this emit it is called from.
    """

    def __init__(self, mgr: SessionManager, session_key: str, text: str) -> None:
        self._mgr = mgr
        self._key = session_key
        self._text = text
        self.types: list[str] = []

    async def emit(self, session_key: str, event: dict[str, Any]) -> None:
        session = self._mgr.get_or_create(self._key)
        session.add_message("user", self._text)
        self._mgr.save(session)
        self.types.append(str(event.get("type")))


class _TitleCall:
    arguments = json.dumps({"title": "Cut a release"})


class _TitleResponse:
    content = None
    tool_calls = [_TitleCall()]


class _TitlingLoop:
    sessions = None

    class provider:  # noqa: N801 - a stand-in, not a class the product names
        @staticmethod
        async def chat_with_retry(**_kwargs: Any) -> _TitleResponse:
            return _TitleResponse()


async def test_naming_starts_before_the_turn_files_its_opening_question(
    parsed: TurnSendParams, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The namer identifies an opening turn by "no user message on disk", and
    the turn now puts one there as its first act. Called from the far side of
    the message.start emit, it found the question already filed and declined to
    name anything -- so every new conversation kept the mechanical title."""
    from raven.rpc.methods.turn import turn_send
    from raven.rpc.session_naming import _tasks

    mgr = SessionManager(tmp_path)
    loop = _TitlingLoop()
    monkeypatch.setattr(session_module, "_safe_invoke_factory", lambda _factory: loop)
    # ``_name_session`` imports this one from ``raven.session.resolve`` at call
    # time, which is where it is defined -- patching session.py's copy leaves
    # the namer reading the real workspace.
    monkeypatch.setattr("raven.session.resolve.manager_for", lambda _loop, _config: mgr)
    emitter = _FilingEmitter(mgr, parsed.session_key, parsed.content or "")
    turn_module._active_turns.clear()
    try:
        result = await turn_send(
            {"session_key": parsed.session_key, "content": parsed.content},
            emitter=emitter,
            scheduler=_Scheduler(),
            turn_ids={},
            agent_loop_factory=lambda: loop,
        )
    finally:
        turn_module._active_turns.clear()

    assert emitter.types == ["message.start"], "the client still gets its opening event"
    assert result["naming"] is True, "the namer was skipped -- the question was already on disk"
    await asyncio.gather(*list(_tasks), return_exceptions=True)
    assert mgr.get_or_create(parsed.session_key).metadata["title"] == "Cut a release"
