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
        "min_input_width": 6,
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
    # Nothing at all: a guard that refuses before the namer starts has nobody
    # waiting on it either -- `turn.send` already answered `naming: false`. The
    # naming_ended event is for a call that ran and came back with no title.
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
    # Nothing at all: a guard that refuses before the namer starts has nobody
    # waiting on it either -- `turn.send` already answered `naming: false`. The
    # naming_ended event is for a call that ran and came back with no title.
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
    # The call ran and came back with nothing to publish, so the client holding
    # a placeholder is told rather than left to time out.
    assert [(e["type"], e["payload"]["reason"]) for _, e in emitter.events] == [("session.naming_ended", "renamed")]


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

    # The call ran and came back with nothing to publish, so the client holding
    # a placeholder is told rather than left to time out.
    assert [(e["type"], e["payload"]["reason"]) for _, e in emitter.events] == [("session.naming_ended", "timeout")]
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

    # The call ran and came back with nothing to publish, so the client holding
    # a placeholder is told rather than left to time out.
    assert [(e["type"], e["payload"]["reason"]) for _, e in emitter.events] == [("session.naming_ended", "no_title")]
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

    # The call ran and came back with nothing to publish, so the client holding
    # a placeholder is told rather than left to time out.
    assert [(e["type"], e["payload"]["reason"]) for _, e in emitter.events] == [("session.naming_ended", "no_title")]


@pytest.mark.parametrize(
    ("opening", "should_name"),
    [
        # Openings taken from the session store this gate was tuned against.
        ("你好", False),
        ("你哈", False),
        ("？", False),
        ("rpc", False),
        ("nihao", False),
        ("hi", False),
        ("hello", False),
        ("你是谁", True),
        ("你能做什么", True),
        ("调研hermes", True),
        ("fix login", True),
    ],
)
@pytest.mark.asyncio
async def test_the_gate_splits_greetings_from_questions_in_either_script(
    tmp_path: Path, opening: str, should_name: bool
) -> None:
    """Six columns, so one number is fair to both scripts.

    Measured in code points instead, this same 6 would admit "nihao" (5 latin
    letters, a greeting) and refuse "你能做什么" (5 ideographs, a whole question) --
    the wrong way round on both counts.
    """
    mgr = SessionManager(tmp_path)
    provider, emitter = _Provider(), _Emitter()

    # Its own key per case: `_in_flight` is module state, so cases sharing one
    # key would refuse each other and every "should name" would pass for the
    # wrong reason.
    task = _start(mgr, provider, emitter, text=opening, min_input_width=6, session_key=f"tui:gate-{opening}")

    assert (task is not None) is should_name
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_a_refused_opening_never_reaches_the_model(tmp_path: Path) -> None:
    """The refusal is free, which is the premise the front end now relies on."""
    mgr = SessionManager(tmp_path)
    provider, emitter = _Provider(), _Emitter()

    task = _start(mgr, provider, emitter, text="你好", min_input_width=6, session_key="tui:refused")

    assert task is None
    assert provider.calls == 0
    # Nothing at all: a guard that refuses before the namer starts has nobody
    # waiting on it either -- `turn.send` already answered `naming: false`. The
    # naming_ended event is for a call that ran and came back with no title.
    assert emitter.events == []


class _Silent:
    """A provider that answers without ever calling the naming tool.

    Observed live against the configured model: a short opening gets a prose
    reply about a third of the time, in roughly a second. That is the common
    quiet ending, not the timeout.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_retry(self, **kwargs: object) -> object:
        self.calls += 1

        class _R:
            tool_calls: list = []
            content = "Sure, I can help with that."

        return _R()


class _Raiser:
    async def chat_with_retry(self, **kwargs: object) -> object:
        raise RuntimeError("provider is down")


class _Slow:
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def chat_with_retry(self, **kwargs: object) -> object:
        await asyncio.sleep(self.delay)
        raise AssertionError("the timeout should have fired first")


def _ended_reasons(emitter: _Emitter) -> list[str]:
    return [e["payload"]["reason"] for _, e in emitter.events if e["type"] == "session.naming_ended"]


@pytest.mark.asyncio
async def test_a_model_that_never_calls_the_tool_says_so(tmp_path: Path) -> None:
    """The quiet ending that actually happens, and the reason the event exists."""
    mgr = SessionManager(tmp_path)
    provider, emitter = _Silent(), _Emitter()

    task = _start(mgr, provider, emitter, session_key="tui:no-title")
    assert task is not None
    await task

    assert provider.calls == 1
    assert _ended_reasons(emitter) == ["no_title"]
    assert [e["type"] for _, e in emitter.events] == ["session.naming_ended"]


@pytest.mark.asyncio
async def test_a_provider_that_raises_arrives_as_no_title(tmp_path: Path) -> None:
    """Not 'error', and the reason matters for whoever reads the event.

    `generate_title` catches the provider call's exception itself and answers
    None, so by the time the seam sees it there is nothing left to tell it apart
    from a model that simply did not call the tool. The cause is in the debug
    line `generate_title` writes; the enum does not claim a distinction the code
    cannot make.
    """
    mgr = SessionManager(tmp_path)
    emitter = _Emitter()

    task = _start(mgr, _Raiser(), emitter, session_key="tui:err")
    assert task is not None
    await task

    assert _ended_reasons(emitter) == ["no_title"]


@pytest.mark.asyncio
async def test_a_broken_naming_seam_arrives_as_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What 'error' is actually for: this code raising, not the model failing.

    Rare, and worth an event anyway -- a client is holding a placeholder, and a
    bug here should not also cost it the full grace period.
    """

    async def _explode(*a: object, **k: object) -> str:
        raise RuntimeError("naming seam is broken")

    monkeypatch.setattr("raven.rpc.session_naming.generate_title", _explode)
    mgr = SessionManager(tmp_path)
    emitter = _Emitter()

    task = _start(mgr, _Provider(), emitter, session_key="tui:seam")
    assert task is not None
    await task

    assert _ended_reasons(emitter) == ["error"]


@pytest.mark.asyncio
async def test_a_call_that_outruns_its_budget_says_so(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    emitter = _Emitter()

    task = _start(mgr, _Slow(5.0), emitter, session_key="tui:slow", timeout_seconds=0.05)
    assert task is not None
    await task

    assert _ended_reasons(emitter) == ["timeout"]


@pytest.mark.asyncio
async def test_a_success_sends_titled_and_nothing_else(tmp_path: Path) -> None:
    """Exactly one of the two events follows a namer that started.

    Both would leave a client settling twice, and `session.naming_ended` after a
    title would settle it onto the opening line -- overwriting the name that had
    just arrived.
    """
    mgr = SessionManager(tmp_path)
    emitter = _Emitter()

    task = _start(mgr, _Provider("Cut a release"), emitter, session_key="tui:ok")
    assert task is not None
    await task

    assert [e["type"] for _, e in emitter.events] == ["session.titled"]


def test_the_reason_set_is_closed_and_matches_the_contract() -> None:
    """The model's Literal and the schema's enum have to be the same set.

    A client is generated from the schema, so a reason the server can send but
    the schema does not list is one the client rejects -- and the drift test
    compares field types, not enum members, so nothing else here would notice.
    Loosening the Literal to `str` passes every other test in this file.
    """
    import json
    from pathlib import Path as _Path
    from typing import get_args, get_type_hints

    from raven.rpc.models import SessionNamingEndedPayload

    declared = set(get_args(get_type_hints(SessionNamingEndedPayload)["reason"]))
    assert declared, "reason must stay a closed Literal, not a bare str"

    schema_path = _Path(__file__).resolve().parents[1] / "rpc-schema" / "openrpc.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    event = schema["components"]["schemas"]["SessionNamingEndedEvent"]
    published = set(event["properties"]["payload"]["properties"]["reason"]["enum"])

    assert declared == published

    # And every one of them is a reason the code can actually produce, so the
    # contract does not advertise a state no reader will ever see. Parsed rather
    # than grepped: the first version looked for the literal `_ended("renamed")`
    # and broke the moment that call became a conditional expression, which is a
    # test failing on formatting instead of on meaning.
    import ast

    module = ast.parse(
        (_Path(__file__).resolve().parents[1] / "raven" / "rpc" / "session_naming.py").read_text(encoding="utf-8")
    )
    emitted: set[str] = set()
    for node in ast.walk(module):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_ended"):
            continue
        for arg in node.args:
            for part in (arg.body, arg.orelse) if isinstance(arg, ast.IfExp) else (arg,):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    emitted.add(part.value)

    assert emitted == declared, f"published {sorted(declared)} but emit {sorted(emitted)}"


# Both prior states, because the check has two halves and each one alone lets a
# different case through: with no title at all, "not title_auto" is true and
# would call it a rename; with the mechanical title on it, "has a title" is true
# and would do the same.
@pytest.mark.parametrize("seed_auto_title", [False, True])
@pytest.mark.asyncio
async def test_a_title_too_long_to_store_is_not_reported_as_a_rename(tmp_path: Path, seed_auto_title: bool) -> None:
    """`set_generated_title` refuses for two unrelated reasons, and the client
    acts on the difference: 'renamed' tells it the row already holds a better
    name and must not be settled onto the opening line. A title that came back
    unstorably long is nobody's rename, and calling it one would make the client
    keep a name that does not exist.

    Unreachable while `budget` stays under TITLE_STORAGE_MAX, since
    `clean_model_title` clamps to it -- which is exactly why it needs a test
    rather than a comment: a config raising `budget` past 200 is the only way in.
    """
    from raven.session.manager import TITLE_STORAGE_MAX

    mgr = SessionManager(tmp_path)
    emitter = _Emitter()
    over = "x" * (TITLE_STORAGE_MAX + 10)
    key = f"tui:toolong-{seed_auto_title}"
    if seed_auto_title:
        # The realistic state by the time an answer lands: `SessionManager.save`
        # writes the mechanical title at turn end.
        seeded = mgr.get_or_create(key)
        seeded.metadata["title"] = "please cut a release"
        seeded.metadata["title_auto"] = True

    task = _start(mgr, _Provider(over), emitter, session_key=key, budget=TITLE_STORAGE_MAX + 50)
    assert task is not None
    await task

    assert _ended_reasons(emitter) == ["no_title"]
    # Whatever it had stands; the unstorable answer is dropped either way.
    expected = "please cut a release" if seed_auto_title else None
    assert mgr.get_or_create(key).metadata.get("title") == expected


@pytest.mark.asyncio
async def test_a_rename_typed_mid_call_is_reported_as_a_rename(tmp_path: Path) -> None:
    """The other half of the pair above, so the two cannot collapse into one."""
    mgr = SessionManager(tmp_path)
    provider, emitter = _Provider(delay=0.05), _Emitter()

    task = _start(mgr, provider, emitter, session_key="tui:byhand")
    assert task is not None
    mgr.get_or_create("tui:byhand").set_title("Release checklist")
    await task

    assert _ended_reasons(emitter) == ["renamed"]
