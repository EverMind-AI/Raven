"""The naming call fired beside a session's opening turn.

Every case here is a decision to *not* publish a title, which is the half of
this feature that cannot be seen working: a session keeps the mechanical name
``SessionManager.save`` derived, and the only evidence the guard fired is that
no event went out and no model was called.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from raven.rpc.session_naming import name_session_alongside_turn
from raven.session.manager import SessionManager


class _Call:
    def __init__(self, title: str) -> None:
        self.arguments = json.dumps({"title": title})


class _Response:
    def __init__(self, title: str) -> None:
        self.tool_calls = [_Call(title)]
        self.content = None


class _Provider:
    """Answers with one title and counts how often it was asked."""

    def __init__(self, title: str = "Cut a release", delay: float = 0.0) -> None:
        self.title = title
        self.delay = delay
        self.calls = 0

    async def chat_with_retry(self, **kwargs: object) -> _Response:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return _Response(self.title)


class _Emitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, session_key: str, event: dict) -> None:
        self.events.append((session_key, event))


OPENING = "please cut a release for the desktop build"


def _start(mgr: SessionManager, provider: object, emitter: object, **over: object):
    kwargs: dict = {
        "session_key": "tui:name01",
        "text": OPENING,
        "mgr": mgr,
        "provider": provider,
        "emitter": emitter,
        "enabled": True,
        "model": None,
        "budget": 24,
        "min_input_chars": 8,
        "timeout_seconds": 5.0,
    }
    kwargs.update(over)
    return name_session_alongside_turn(**kwargs)


@pytest.mark.asyncio
async def test_names_a_fresh_session_and_says_so_on_its_own_conversation(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    provider, emitter = _Provider(), _Emitter()

    task = _start(mgr, provider, emitter)
    assert task is not None
    await task

    assert mgr.get_or_create("tui:name01").metadata["title"] == "Cut a release"
    # Named with the session key, not broadcast: another client's rail must not
    # light up for a session it is not watching.
    assert emitter.events == [
        ("tui:name01", {"type": "session.titled", "payload": {"session_id": "tui:name01", "title": "Cut a release"}})
    ]


@pytest.mark.asyncio
async def test_a_greeting_is_not_worth_a_model_call(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    provider, emitter = _Provider(), _Emitter()

    assert _start(mgr, provider, emitter, text="hi") is None

    assert provider.calls == 0
    assert emitter.events == []


@pytest.mark.asyncio
async def test_disabled_names_nothing(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    provider, emitter = _Provider(), _Emitter()

    assert _start(mgr, provider, emitter, enabled=False) is None

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_a_session_with_history_is_not_the_one_being_opened(tmp_path: Path) -> None:
    """Only the opening turn names a session. A later turn arriving at an
    auto-named session must not pay for a second title."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:name01")
    session.add_message("user", OPENING)
    mgr.save(session)
    provider, emitter = _Provider(), _Emitter()

    assert _start(mgr, provider, emitter) is None

    assert provider.calls == 0
    assert emitter.events == []


@pytest.mark.asyncio
async def test_a_session_a_person_already_named_is_left_alone(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:name01")
    session.set_title("Release checklist")
    provider, emitter = _Provider(), _Emitter()

    assert _start(mgr, provider, emitter) is None

    assert provider.calls == 0
    assert mgr.get_or_create("tui:name01").metadata["title"] == "Release checklist"


@pytest.mark.asyncio
async def test_a_rename_typed_while_the_call_ran_wins(tmp_path: Path) -> None:
    """The answer is older than the rename by the time it lands, so it is
    dropped -- and nothing is emitted, or the client would paint over the name
    the person just typed."""
    mgr = SessionManager(tmp_path)
    provider, emitter = _Provider(delay=0.05), _Emitter()

    task = _start(mgr, provider, emitter)
    assert task is not None
    mgr.get_or_create("tui:name01").set_title("Release checklist")
    await task

    assert provider.calls == 1
    assert mgr.get_or_create("tui:name01").metadata["title"] == "Release checklist"
    assert emitter.events == []


@pytest.mark.asyncio
async def test_a_call_that_outruns_the_timeout_leaves_the_fallback_standing(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:name01")
    session.add_message("user", OPENING)
    provider, emitter = _Provider(delay=1.0), _Emitter()

    # Seeded above so the mechanical title exists to be left alone; the guard
    # that skips a session with history is bypassed by naming a second key.
    task = _start(mgr, provider, emitter, session_key="tui:name02", timeout_seconds=0.01)
    assert task is not None
    await task

    assert emitter.events == []
    assert mgr.get_or_create("tui:name02").metadata.get("title") is None


@pytest.mark.asyncio
async def test_two_sends_before_the_first_message_lands_pay_for_one_title(tmp_path: Path) -> None:
    """Both see a message-less session, and without the in-flight guard both
    would call the model for the same name."""
    mgr = SessionManager(tmp_path)
    provider, emitter = _Provider(delay=0.02), _Emitter()

    first = _start(mgr, provider, emitter)
    second = _start(mgr, provider, emitter)

    assert first is not None
    assert second is None
    await first
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_an_answer_that_ignored_the_budget_publishes_nothing(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    provider, emitter = _Provider(title="z" * 200), _Emitter()

    task = _start(mgr, provider, emitter)
    assert task is not None
    await task

    assert emitter.events == []
    assert mgr.get_or_create("tui:name01").metadata.get("title") is None


@pytest.mark.asyncio
async def test_a_provider_that_raises_is_not_a_failed_turn(tmp_path: Path) -> None:
    class _Broken:
        async def chat_with_retry(self, **kwargs: object) -> _Response:
            raise RuntimeError("no route to model")

    mgr = SessionManager(tmp_path)
    emitter = _Emitter()

    task = _start(mgr, _Broken(), emitter)
    assert task is not None
    await task

    assert emitter.events == []
