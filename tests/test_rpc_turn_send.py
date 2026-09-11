"""Tests for ``turn.send`` real handler.

``turn.send`` submits a turn onto the spine (build_rpc_spine Scheduler) and returns
``{turn_id, accepted}`` synchronously; the turn streams out via the hub/sink.
These tests drive the handler with a fake Scheduler + emitter (the spine path
itself is covered in ``test_rpc_spine.py``).

Spec source:
- ``raven/rpc/models.py`` ``TurnSendParams`` / ``TurnSendResult``
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import ModelNotAvailableError, RpcError, TurnInProgressError
from raven.rpc.methods.turn import register_turn_methods, turn_send


class FakeHandle:
    def __init__(self) -> None:
        self.cancelled = False

    async def cancel(self) -> None:
        self.cancelled = True

    async def result(self):
        return None


class FakeScheduler:
    """Records submitted requests; returns a handle. Optionally raises."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.submitted: list = []
        self._raises = raises

    def submit(self, req):
        if self._raises is not None:
            raise self._raises
        self.submitted.append(req)
        return FakeHandle()


class FakeEmitter:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, session_key: str, event: dict) -> None:
        self.emitted.append((session_key, event))

    def types(self) -> list[str]:
        return [e["type"] for _k, e in self.emitted]


@pytest.fixture(autouse=True)
def _clear_active_turns():
    from raven.rpc.methods import turn as _turn_mod

    _turn_mod._active_turns.clear()
    yield
    _turn_mod._active_turns.clear()


@pytest.fixture
def dispatcher() -> Dispatcher:
    d = Dispatcher()
    register_turn_methods(d, emitter=FakeEmitter(), scheduler=FakeScheduler(), turn_ids={})
    return d


# --- Happy path ---


async def test_turn_send_happy_path_returns_turn_id_and_accepted() -> None:
    scheduler = FakeScheduler()
    turn_ids: dict[str, str] = {}
    emitter = FakeEmitter()

    result = await turn_send(
        {"session_key": "tui:default", "content": "hello"},
        emitter=emitter,
        scheduler=scheduler,
        turn_ids=turn_ids,
    )

    assert set(result) == {"turn_id", "accepted", "naming"}
    assert result["accepted"] is True
    # No agent loop is wired in this harness, so no namer can have started.
    assert result["naming"] is False
    assert isinstance(result["turn_id"], str) and len(result["turn_id"]) >= 16
    # The turn was submitted, the slot bound, message.start emitted.
    assert len(scheduler.submitted) == 1
    assert scheduler.submitted[0].conversation == "tui:default"
    assert turn_ids["tui:default"] == result["turn_id"]
    assert emitter.types() == ["message.start"]
    assert emitter.emitted[0][1]["payload"]["turn_id"] == result["turn_id"]


async def test_the_returned_turn_id_rides_the_submitted_request() -> None:
    """The client correlates on the id this call returns, and the lane stamps the
    lifecycle events from the request -- so the two have to be the same value or
    the completion the client is waiting for never names its turn."""
    scheduler = FakeScheduler()

    result = await turn_send({"session_key": "tui:default", "content": "hello"}, scheduler=scheduler, turn_ids={})

    assert scheduler.submitted[0].turn_id == result["turn_id"]


async def test_turn_send_generates_unique_turn_ids() -> None:
    scheduler = FakeScheduler()
    turn_ids: dict[str, str] = {}
    r1 = await turn_send({"session_key": "tui:a", "content": "x"}, scheduler=scheduler, turn_ids=turn_ids)
    r2 = await turn_send({"session_key": "tui:b", "content": "x"}, scheduler=scheduler, turn_ids=turn_ids)
    assert r1["turn_id"] != r2["turn_id"]


async def test_turn_send_binds_active_slot_after_submit() -> None:
    from raven.rpc.methods import turn as turn_mod

    scheduler = FakeScheduler()
    await turn_send({"session_key": "tui:default", "content": "hi"}, scheduler=scheduler, turn_ids={})
    assert turn_mod.is_turn_active("tui:default") is True


# --- Error paths ---


async def test_turn_send_rejects_active_turn_with_minus_32003() -> None:
    scheduler = FakeScheduler()
    turn_ids: dict[str, str] = {}
    await turn_send({"session_key": "tui:default", "content": "first"}, scheduler=scheduler, turn_ids=turn_ids)

    with pytest.raises(TurnInProgressError) as excinfo:
        await turn_send({"session_key": "tui:default", "content": "second"}, scheduler=scheduler, turn_ids=turn_ids)

    assert excinfo.value.CODE == -32003
    assert excinfo.value.MESSAGE == "turn_in_progress"


async def test_turn_send_rejects_unknown_model_with_minus_32008() -> None:
    with patch(
        "raven.rpc.methods.turn._resolve_model",
        side_effect=ModelNotAvailableError("no provider configured"),
    ):
        with pytest.raises(ModelNotAvailableError) as excinfo:
            await turn_send({"session_key": "tui:default", "content": "x"}, scheduler=FakeScheduler())

    assert excinfo.value.CODE == -32008


async def test_turn_send_without_scheduler_emits_model_not_available() -> None:
    # No agent loop wired (scheduler None, no build error) → per-turn -32008 event.
    emitter = FakeEmitter()
    result = await turn_send({"session_key": "tui:default", "content": "x"}, emitter=emitter, scheduler=None)
    assert result["accepted"] is True
    assert emitter.types() == ["message.start", "error"]
    assert emitter.emitted[-1][1]["payload"]["code"] == -32008


async def test_turn_send_when_submit_rejected_surfaces_turn_failed() -> None:
    # Server draining: submit raises → message.start + turn_failed error, no bind.
    from raven.rpc.methods import turn as turn_mod
    from raven.spine.scheduler import SchedulerDrainingError

    emitter = FakeEmitter()
    turn_ids: dict[str, str] = {}
    result = await turn_send(
        {"session_key": "tui:default", "content": "x"},
        emitter=emitter,
        scheduler=FakeScheduler(raises=SchedulerDrainingError("draining")),
        turn_ids=turn_ids,
    )
    assert result["accepted"] is True
    assert emitter.types() == ["message.start", "error"]
    assert emitter.emitted[-1][1]["payload"]["message"] == "turn_failed"
    # No leak: a rejected submit binds neither map.
    assert turn_ids == {} and "tui:default" not in turn_mod._active_turns


async def test_turn_send_without_scheduler_surfaces_build_error_code() -> None:
    # A latched build error surfaces with its own code, not -32008.
    class _BuildErr(RpcError):
        CODE = -32603
        MESSAGE = "internal_error"

    emitter = FakeEmitter()
    build_error = _BuildErr("boom")
    await turn_send(
        {"session_key": "tui:default", "content": "x"},
        emitter=emitter,
        scheduler=None,
        build_error=build_error,
    )
    assert emitter.types() == ["message.start", "error"]
    assert emitter.emitted[-1][1]["payload"]["code"] == -32603


# --- Params validation ---


async def test_turn_send_rejects_missing_session_key() -> None:
    with pytest.raises(ValidationError):
        await turn_send({"content": "missing session_key"}, scheduler=FakeScheduler())


async def test_turn_send_rejects_missing_content() -> None:
    with pytest.raises(ValidationError):
        await turn_send({"session_key": "tui:default"}, scheduler=FakeScheduler())


async def test_turn_send_accepts_optional_channel_chat_id_sender_id() -> None:
    scheduler = FakeScheduler()
    result = await turn_send(
        {
            "session_key": "tui:default",
            "content": "hi",
            "channel": "tui",
            "chat_id": "default",
            "sender_id": "user",
        },
        scheduler=scheduler,
        turn_ids={},
    )
    assert result["accepted"] is True
    src = scheduler.submitted[0].source
    assert (src.channel, src.chat_id, src.sender_id) == ("tui", "default", "user")


# --- End-to-end via Dispatcher ---


async def test_turn_send_dispatches_via_dispatcher(dispatcher: Dispatcher) -> None:
    resp = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn.send",
            "params": {"session_key": "tui:default", "content": "hello"},
        }
    )

    assert "error" not in resp, f"turn.send unexpectedly raised: {resp}"
    assert set(resp["result"]) == {"turn_id", "accepted", "naming"}
    assert resp["result"]["accepted"] is True


async def test_turn_send_dispatcher_returns_minus_32003_on_concurrent_send(
    dispatcher: Dispatcher,
) -> None:
    await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn.send",
            "params": {"session_key": "tui:default", "content": "first"},
        }
    )
    resp = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "turn.send",
            "params": {"session_key": "tui:default", "content": "second"},
        }
    )

    assert "error" in resp
    assert resp["error"]["code"] == -32003
    assert resp["error"]["message"] == "turn_in_progress"


# --- Attachments ---
#
# ``media`` carries paths, not bytes: the front end has already put the file in
# the workspace. What these cover is the resolution policy, because the failure
# mode downstream is silent -- ``build_user_content`` drops a path that is not a
# file without a word, so a wrong resolve loses the attachment with no error
# anywhere in the stack.


def _workspace_cfg(tmp_path, *, restrict: bool = True):
    """Patch load_config so the resolver sees ``tmp_path`` as the workspace."""
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.workspace_path = Path(tmp_path)
    cfg.tools.restrict_to_workspace = restrict
    return patch("raven.config.load_config", return_value=cfg)


async def test_turn_send_resolves_a_workspace_relative_attachment(tmp_path) -> None:
    scheduler = FakeScheduler()
    (tmp_path / "uploads").mkdir()
    shot = tmp_path / "uploads" / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")

    with _workspace_cfg(tmp_path):
        await turn_send(
            {"session_key": "tui:default", "content": "look", "media": ["uploads/shot.png"]},
            scheduler=scheduler,
            turn_ids={},
        )

    # Resolved against the workspace, not the process cwd -- the front end sends
    # back exactly what fs.upload returned, which is workspace-relative.
    assert [m.path for m in scheduler.submitted[0].media] == [str(shot)]


async def test_turn_send_accepts_an_absolute_attachment(tmp_path) -> None:
    scheduler = FakeScheduler()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")

    with _workspace_cfg(tmp_path):
        await turn_send(
            {"session_key": "tui:default", "content": "look", "media": [str(shot)]},
            scheduler=scheduler,
            turn_ids={},
        )

    assert [m.path for m in scheduler.submitted[0].media] == [str(shot)]


async def test_turn_send_drops_a_missing_attachment_without_failing_the_turn(tmp_path) -> None:
    scheduler = FakeScheduler()
    kept = tmp_path / "kept.png"
    kept.write_bytes(b"\x89PNG\r\n\x1a\n")

    with _workspace_cfg(tmp_path):
        result = await turn_send(
            {
                "session_key": "tui:default",
                "content": "look",
                "media": ["uploads/gone.png", str(kept)],
            },
            scheduler=scheduler,
            turn_ids={},
        )

    # One bad path must not cost the user the whole message.
    assert result["accepted"] is True
    assert [m.path for m in scheduler.submitted[0].media] == [str(kept)]


async def test_turn_send_refuses_an_attachment_outside_the_workspace(tmp_path) -> None:
    scheduler = FakeScheduler()
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")

    with _workspace_cfg(ws):
        await turn_send(
            {"session_key": "tui:default", "content": "look", "media": [str(outside)]},
            scheduler=scheduler,
            turn_ids={},
        )

    # The viewer and the file tools refuse this path; the attachment lane may
    # not become the way around them.
    assert scheduler.submitted[0].media == ()


async def test_turn_send_without_media_submits_none(tmp_path) -> None:
    scheduler = FakeScheduler()
    await turn_send({"session_key": "tui:default", "content": "hi"}, scheduler=scheduler, turn_ids={})
    assert scheduler.submitted[0].media == ()


async def test_turn_send_accepts_an_absolute_path_when_the_workspace_is_not_enforced(tmp_path) -> None:
    """`restrict_to_workspace` defaults to False, so this is the shipped path.

    The attachment lane deliberately matches the filesystem tools rather than
    inventing a second policy: a file the agent may read is a file the user may
    hand it.
    """
    scheduler = FakeScheduler()
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    ws = tmp_path / "ws"
    ws.mkdir()

    with _workspace_cfg(ws, restrict=False):
        await turn_send(
            {"session_key": "tui:default", "content": "look", "media": [str(outside)]},
            scheduler=scheduler,
            turn_ids={},
        )

    assert [m.path for m in scheduler.submitted[0].media] == [str(outside)]


@pytest.mark.parametrize(
    "bad",
    [
        "with\x00null.png",  # ValueError from the OS layer
        "~nosuchuser42/x.png",  # RuntimeError from expanduser
        "x" * 300 + ".png",  # OSError: name too long
    ],
)
async def test_a_path_the_os_rejects_drops_the_attachment_not_the_turn(tmp_path, bad) -> None:
    """Every rejection shape has to be caught here. Escaping this function turns
    one unusable attachment into a turn that never runs at all."""
    scheduler = FakeScheduler()
    kept = tmp_path / "kept.png"
    kept.write_bytes(b"\x89PNG\r\n\x1a\n")

    with _workspace_cfg(tmp_path):
        result = await turn_send(
            {"session_key": "tui:default", "content": "look", "media": [bad, str(kept)]},
            scheduler=scheduler,
            turn_ids={},
        )

    assert result["accepted"] is True
    assert [m.path for m in scheduler.submitted[0].media] == [str(kept)]


async def test_a_broken_config_drops_attachments_without_failing_the_turn(tmp_path) -> None:
    scheduler = FakeScheduler()
    with patch("raven.config.load_config", side_effect=RuntimeError("config on fire")):
        result = await turn_send(
            {"session_key": "tui:default", "content": "look", "media": ["uploads/x.png"]},
            scheduler=scheduler,
            turn_ids={},
        )

    assert result["accepted"] is True
    assert scheduler.submitted[0].media == ()


async def test_message_start_carries_the_question_that_opened_the_turn() -> None:
    """A window that did not send the turn has no other way to learn it.

    The user entry reaches the transcript only when the turn ends, so a second
    window -- or a shared link opened mid-turn -- saw an answer streaming under
    no question at all.
    """
    emitter = FakeEmitter()

    await turn_send(
        {"session_key": "tui:default", "content": "render this log for me"},
        emitter=emitter,
        scheduler=FakeScheduler(),
        turn_ids={},
    )

    assert emitter.emitted[0][1]["payload"]["content"] == "render this log for me"


# ---------------------------------------------------------------------------
# Direct chat: addressing one sub-agent instance instead of the main agent
# ---------------------------------------------------------------------------


async def test_target_becomes_direct_target_on_the_request() -> None:
    scheduler = FakeScheduler()

    await turn_send(
        {
            "session_key": "tui:default",
            "content": "fix it",
            "target": {"agent": "Raven-Code", "handle": "refactor-auth"},
        },
        scheduler=scheduler,
        turn_ids={},
    )

    assert scheduler.submitted[0].direct_target == ("Raven-Code", "refactor-auth")


async def test_no_target_leaves_direct_target_none() -> None:
    scheduler = FakeScheduler()

    await turn_send({"session_key": "tui:default", "content": "hi"}, scheduler=scheduler, turn_ids={})

    assert scheduler.submitted[0].direct_target is None


async def test_a_partial_target_is_refused_at_the_schema() -> None:
    """Both halves are the instance's identity; one alone would address nothing."""
    with pytest.raises(ValidationError):
        await turn_send(
            {"session_key": "tui:default", "content": "hi", "target": {"agent": "Raven-Code"}},
            scheduler=FakeScheduler(),
            turn_ids={},
        )


async def test_message_start_carries_the_target() -> None:
    emitter = FakeEmitter()

    await turn_send(
        {
            "session_key": "tui:default",
            "content": "fix it",
            "target": {"agent": "Raven-Code", "handle": "refactor-auth"},
        },
        emitter=emitter,
        scheduler=FakeScheduler(),
        turn_ids={},
    )

    start = next(e for _k, e in emitter.emitted if e["type"] == "message.start")
    assert start["payload"]["target"] == {"agent": "Raven-Code", "handle": "refactor-auth"}


async def test_a_main_agent_message_start_carries_no_target_key() -> None:
    """Absent, not null: every payload the wire already carried keeps its shape,
    and an untagged frame reads as the main conversation's by its own content."""
    emitter = FakeEmitter()

    await turn_send(
        {"session_key": "tui:default", "content": "hi"}, emitter=emitter, scheduler=FakeScheduler(), turn_ids={}
    )

    start = next(e for _k, e in emitter.emitted if e["type"] == "message.start")
    # Absence is the assertion, not the payload's exact shape: this event also
    # carries the question that opened the turn, for a window that did not send
    # it. Pinning the whole dict here would fail on any later addition without
    # saying anything about the tag.
    assert "target" not in start["payload"]
    assert start["payload"]["turn_id"]


async def test_the_target_map_is_bound_for_the_outlet_to_read() -> None:
    targets: dict[str, dict[str, str]] = {}

    await turn_send(
        {
            "session_key": "tui:default",
            "content": "fix it",
            "target": {"agent": "Raven-Code", "handle": "refactor-auth"},
        },
        scheduler=FakeScheduler(),
        turn_ids={},
        direct_targets=targets,
    )

    # Keyed by the instance's lane, which is what the turn runs on and what the
    # outlet reads back; a session-keyed slot would be shared by every instance.
    assert targets == {"tui:default#Raven-Code/refactor-auth": {"agent": "Raven-Code", "handle": "refactor-auth"}}


async def test_a_main_agent_turn_clears_a_stale_binding() -> None:
    """The sink drops the slot at turn end, but a turn that never reached the
    sink would otherwise leave one behind and tag the next reply as a
    sub-agent's."""
    targets: dict[str, dict[str, str]] = {"tui:default": {"agent": "Raven-Code", "handle": "refactor-auth"}}

    await turn_send(
        {"session_key": "tui:default", "content": "hi"},
        scheduler=FakeScheduler(),
        turn_ids={},
        direct_targets=targets,
    )

    assert targets == {}


async def test_a_turn_that_never_ran_still_reports_against_its_target() -> None:
    """No scheduler wired: the start/error pair is all the client gets, and it
    must clear the spinner in the view the user was actually looking at."""
    emitter = FakeEmitter()

    await turn_send(
        {
            "session_key": "tui:default",
            "content": "fix it",
            "target": {"agent": "Raven-Code", "handle": "refactor-auth"},
        },
        emitter=emitter,
        scheduler=None,
    )

    tagged = [e["payload"].get("target") for _k, e in emitter.emitted]
    assert tagged == [
        {"agent": "Raven-Code", "handle": "refactor-auth"},
        {"agent": "Raven-Code", "handle": "refactor-auth"},
    ]


# --- Per-connection surface ---


async def test_turn_send_stamps_the_connection_surface_on_the_source() -> None:
    """The turn runs on the spine's own task, where the connection's
    contextvars cannot reach, so the declared surface must ride the request."""
    from raven.rpc import connection

    scheduler = FakeScheduler()
    token = connection.bind_connection()
    try:
        connection.declare_surface("shell")
        await turn_send({"session_key": "tui:default", "content": "hi"}, scheduler=scheduler, turn_ids={})
    finally:
        connection.unbind_connection(token)

    src = scheduler.submitted[0].source
    assert src.surface == "shell"
    assert src.channel == "tui", "identity is tracing-only; the shared channel must not move"


async def test_turn_send_without_a_declaration_leaves_the_surface_unset() -> None:
    """An undeclared connection (or no connection scope at all) submits exactly
    the request it always did -- tracing falls back to the process-wide value."""
    scheduler = FakeScheduler()
    await turn_send({"session_key": "tui:default", "content": "hi"}, scheduler=scheduler, turn_ids={})
    assert scheduler.submitted[0].source.surface is None


async def test_turn_send_claims_the_conversation_for_this_connection() -> None:
    """A mid-turn question belongs to whoever sent the turn. The broker emits
    from the engine's own task, where no connection is bound, so turn.send has
    to record the owner while it still has the context."""
    from raven.rpc import connection

    async def sink(_frame: dict) -> None:
        pass

    token = connection.bind_connection()
    try:
        connection.set_frame_sink(sink)
        await turn_send({"session_key": "tui:default", "content": "hi"}, scheduler=FakeScheduler(), turn_ids={})
        assert connection.frame_sink_for("tui:default") is sink
    finally:
        connection.unbind_connection(token)
    # The claim is the socket's, not the process's: it goes when the socket does.
    assert connection.frame_sink_for("tui:default") is None


async def test_turn_send_claims_a_direct_chat_by_its_own_lane() -> None:
    """A direct-chat turn runs on that instance's lane, and that lane is the
    conversation the question is keyed by -- so the lane is what gets claimed."""
    from raven.rpc import connection
    from raven.spine import direct_lane

    async def sink(_frame: dict) -> None:
        pass

    lane = direct_lane("tui:default", "Raven-Code", "refactor-auth")
    token = connection.bind_connection()
    try:
        connection.set_frame_sink(sink)
        await turn_send(
            {
                "session_key": "tui:default",
                "content": "fix it",
                "target": {"agent": "Raven-Code", "handle": "refactor-auth"},
            },
            scheduler=FakeScheduler(),
            turn_ids={},
        )
        assert connection.frame_sink_for(lane) is sink
    finally:
        connection.unbind_connection(token)


async def test_the_result_carries_the_namers_answer_rather_than_a_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`naming` is what the seam actually decided, in both directions.

    Asserting it only on `_name_session` leaves the field free to be a literal
    here, and a literal `True` puts the front end back to holding a placeholder
    through its whole grace period for an event that is never coming -- the bug
    the field exists to remove, reintroduced one layer up and invisible.
    """
    import raven.rpc.methods.turn as turn_module

    # A key per case: a lane still holding an active turn refuses the next send
    # outright, and both cases would then never reach the assertion.
    for answer in (True, False):
        monkeypatch.setattr(turn_module, "_name_session", lambda *a, **k: answer)
        result = await turn_send(
            {"session_key": f"tui:answer-{answer}", "content": "hello"},
            scheduler=FakeScheduler(),
            turn_ids={},
        )
        assert result["naming"] is answer


async def test_a_turn_aimed_at_an_instance_never_claims_a_name_is_coming() -> None:
    """A direct chat names its instance's lane, not this session."""
    result = await turn_send(
        {"session_key": "tui:default", "content": "hello", "target": {"agent": "raven", "handle": "h1"}},
        scheduler=FakeScheduler(),
        turn_ids={},
    )

    assert result["naming"] is False


async def test_busy_inject_hands_the_text_to_the_running_turn() -> None:
    """``busy: inject`` on a lane mid-turn does not refuse: the request goes to
    the scheduler as ``BusyPolicy.INJECT`` (the running turn's worker merges it
    at its next tool-loop gap) and no slot is bound -- the running turn owns the
    lane's. The id answered is one minted for the text, since that is the id
    its events carry if the host ends first and it runs as a turn of its own.
    Without ``busy`` the same send still refuses with -32003."""
    from raven.rpc.methods import turn as turn_mod
    from raven.spine.turn import BusyPolicy

    scheduler = FakeScheduler()
    turn_ids = {"tui:default": "running-1"}
    turn_mod._active_turns["tui:default"] = FakeHandle()

    with pytest.raises(TurnInProgressError):
        await turn_send({"session_key": "tui:default", "content": "hi"}, scheduler=scheduler, turn_ids=turn_ids)

    result = await turn_send(
        {"session_key": "tui:default", "content": "how far along?", "busy": "inject"},
        scheduler=scheduler,
        turn_ids=turn_ids,
    )

    assert result["accepted"] is True and result["naming"] is False
    assert result["turn_id"] and result["turn_id"] != "running-1", "the inject's own id, not the host's"
    assert len(scheduler.submitted) == 1
    req = scheduler.submitted[0]
    assert req.busy is BusyPolicy.INJECT and req.text == "how far along?" and req.conversation == "tui:default"
    assert req.turn_id == result["turn_id"]
    assert turn_ids == {"tui:default": "running-1"}, "the running turn keeps the lane's slot"


async def test_busy_inject_on_an_idle_lane_is_an_ordinary_send() -> None:
    scheduler = FakeScheduler()
    result = await turn_send({"session_key": "tui:idle", "content": "hi", "busy": "inject"}, scheduler=scheduler)
    assert result["accepted"] is True and result["turn_id"]
    from raven.spine.turn import BusyPolicy

    assert scheduler.submitted[0].busy is BusyPolicy.APPEND


async def test_busy_inject_merges_into_a_wake_the_real_scheduler_is_running() -> None:
    """The reviewer's reproduction (2026-09-09), kept as the test the fakes above
    cannot be: a turn the runtime submitted straight to the real ``Scheduler``
    -- the way ``make_on_session_wake`` submits an armed wake -- with no
    ``turn.send`` slot bound, and a steer arriving while it runs. Before the
    fix the lane read ``pending=1, inject_mailbox=0``: a second turn queued
    behind the wake, reported as ``injected``. Now the text sits in the inject
    mailbox for the running turn's worker to drain, and nothing is queued."""
    from raven.spine import ChatType, Origin, Source, TurnRequest
    from raven.spine.events import Usage
    from raven.spine.runner import TurnOutcome
    from raven.spine.scheduler import OriginPools, Scheduler

    started = asyncio.Event()
    release = asyncio.Event()
    drained: list = []

    class _SleepingWake:
        async def run(self, req, emit, drain):
            started.set()
            await release.wait()
            drained.extend(drain())
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    async def _sink(event) -> None:
        return None

    scheduler = Scheduler(_SleepingWake(), OriginPools(user=0, system=0), _sink)
    wake = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="acp", chat_id="w1", sender_id="cron", chat_type=ChatType.DM),
        text="wake: look at the job",
        conversation="acp:w1",
    )
    handle = scheduler.submit(wake)
    await asyncio.wait_for(started.wait(), 2)
    from raven.rpc.methods import turn as turn_mod

    assert not turn_mod.is_turn_active("acp:w1") and scheduler.has_inflight("acp:w1")

    try:
        result = await turn_send(
            {"session_key": "acp:w1", "channel": "acp", "chat_id": "w1", "content": "how far along?", "busy": "inject"},
            scheduler=scheduler,
            turn_ids={},
        )

        lane = scheduler._lanes["acp:w1"]
        assert result["accepted"] is True
        assert (len(lane._inject_mailbox), len(lane._pending)) == (1, 0), "merged into the wake, not queued behind it"
        release.set()
        await asyncio.wait_for(handle.result(), 2)
        assert [r.text for r in drained] == ["how far along?"], "the running turn drained the steer at its gap"
    finally:
        # A failed assertion must not leave the wake's worker parked on
        # ``release`` forever -- that hangs the loop's teardown, not the test.
        release.set()
        await scheduler.shutdown(grace=1)


class _DrainOnceLoop:
    """The real loop's shape (turn_path.py): drain the injects once at the top
    of the iteration, then work until released. A second drain never comes, so
    an inject arriving after the first is the undrained case the reviewer
    reproduced (2026-09-10)."""

    def __init__(self) -> None:
        self.saw: list[tuple[str, list[str]]] = []
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.tools: dict = {}

    def gate(self, text: str) -> asyncio.Event:
        self.started.setdefault(text, asyncio.Event())
        return self.release.setdefault(text, asyncio.Event())

    async def run_turn(self, req, emit, drain, *, stream, inline_tool_stream=False, usage_sink=None, text_sink=None):
        from raven.spine.events import Usage
        from raven.spine.runner import TurnOutcome

        self.saw.append((req.text, [r.text for r in drain()]))
        self.gate(req.text)
        self.started[req.text].set()
        await self.release[req.text].wait()
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)


async def _spine_with_injects():
    from raven.rpc.methods import turn as turn_mod
    from raven.rpc.spine import build_rpc_spine
    from tests.test_rpc_spine import FakeEmitter

    loop = _DrainOnceLoop()
    emitter = FakeEmitter()
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(
        loop,
        emitter,
        on_turn_end=turn_mod.clear_active,
        on_turn_start=turn_mod.promote_pending_inject,
        user_pool=0,
        system_pool=0,
    )
    return loop, emitter, scheduler, turn_ids, teardown


async def test_an_undrained_inject_runs_as_a_turn_the_surface_can_see_and_cancel() -> None:
    """Reviewer 2026-09-10, reproduced through ``build_rpc_spine`` with the sink
    wired as bootstrap wires it. The host turn ends before its next drain; the
    spine falls the inject back to a turn of its own. That turn used to own
    nothing: ``turn_ids`` empty, ``is_turn_active`` false, ``turn.cancel`` a
    no-op, no ``message.start`` -- the agent kept working after a person's
    cancel did nothing. Now the sink promotes it: the lane is bound to the
    inject's id, the turn opens on the wire with the person's text, and cancel
    ends it."""
    from raven.rpc.methods import turn as turn_mod
    from raven.rpc.methods.turn import turn_cancel

    loop, emitter, scheduler, turn_ids, teardown = await _spine_with_injects()
    lane = "acp:w1"
    try:
        first = await turn_send(
            {"session_key": lane, "content": "go"}, emitter=emitter, scheduler=scheduler, turn_ids=turn_ids
        )
        await asyncio.wait_for(loop.started.setdefault("go", asyncio.Event()).wait(), 2)

        steer = await turn_send(
            {"session_key": lane, "content": "the docs first", "busy": "inject"},
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
        )
        assert steer["turn_id"] not in ("", first["turn_id"])
        assert turn_ids[lane] == first["turn_id"], "while the host runs, it keeps the lane"

        loop.gate("go").set()  # the host ends past its only drain: the inject falls back
        await asyncio.wait_for(loop.started.setdefault("the docs first", asyncio.Event()).wait(), 2)
        for _ in range(5):
            await asyncio.sleep(0)

        assert loop.saw == [("go", []), ("the docs first", [])], "the text runs as a full turn either way"
        assert turn_ids[lane] == steer["turn_id"], "the fallback turn now owns the lane"
        assert turn_mod.is_turn_active(lane), "and the cancel paths can see it"
        starts = [e for _k, e in emitter.emitted if e["type"] == "message.start"]
        assert [e["payload"].get("turn_id") for e in starts] == [first["turn_id"], steer["turn_id"]]
        assert starts[-1]["payload"]["content"] == "the docs first", "opened on the wire with the person's words"

        cancelled = await turn_cancel({"session_key": lane}, emitter=emitter, turn_ids=turn_ids)
        assert cancelled == {"cancelled": True}
        assert not turn_mod.is_turn_active(lane) and lane not in turn_ids
        errors = [e for _k, e in emitter.emitted if e["type"] == "error"]
        assert errors and errors[-1]["payload"]["turn_id"] == steer["turn_id"]
        assert lane not in turn_mod._pending_injects
    finally:
        for gate in loop.release.values():
            gate.set()
        await teardown()


async def test_a_pending_inject_can_be_cancelled_before_it_merges_or_falls_back() -> None:
    """While the host still runs, the inject sits in the lane's mailbox and owns
    no slot; ``turn.cancel`` reaches it through the pending record all the same,
    and the host turn is left alone."""
    from raven.rpc.methods import turn as turn_mod
    from raven.rpc.methods.turn import turn_cancel

    loop, emitter, scheduler, turn_ids, teardown = await _spine_with_injects()
    lane = "acp:w2"
    try:
        first = await turn_send(
            {"session_key": lane, "content": "go"}, emitter=emitter, scheduler=scheduler, turn_ids=turn_ids
        )
        await asyncio.wait_for(loop.started.setdefault("go", asyncio.Event()).wait(), 2)
        # Cancelling now reaches the host turn: it is the active one.
        assert turn_mod._cancellable(lane)[1] is None

        # Take the host out of the active slot as the sink would at its end, but
        # keep it running: the window between the host releasing the lane and the
        # fallback starting is where a cancel used to find nothing at all.
        steer = await turn_send(
            {"session_key": lane, "content": "hold on", "busy": "inject"},
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
        )
        assert scheduler._lanes[lane]._inject_mailbox and lane in turn_mod._pending_injects
        turn_mod._active_turns.pop(lane)
        assert turn_mod._cancellable(lane) is not None and turn_mod._cancellable(lane)[1] == steer["turn_id"]

        cancelled = await turn_cancel({"session_key": lane}, emitter=emitter, turn_ids=turn_ids)

        assert cancelled == {"cancelled": True}
        assert not scheduler._lanes[lane]._inject_mailbox, "the inject is out of the mailbox"
        assert lane not in turn_mod._pending_injects
        assert scheduler.has_inflight(lane), "the host turn was not touched"
        errors = [e for _k, e in emitter.emitted if e["type"] == "error"]
        assert errors[-1]["payload"]["turn_id"] == steer["turn_id"]
        loop.gate("go").set()
        await asyncio.sleep(0.05)
        assert loop.saw == [("go", [])], "nothing fell back: the cancelled inject never ran"
    finally:
        for gate in loop.release.values():
            gate.set()
        await teardown()


async def test_two_undrained_injects_each_get_their_own_turn_promoted_and_cancelled() -> None:
    """Reviewer 2026-09-10, second counterexample: two steers after the host's
    only drain. One replaceable record per lane kept only the newer, so the
    first fallback started unowned -- ``turn_ids`` empty, no ``message.start``
    -- and a cancel took out the still-pending second while the first kept
    running. Every inject is kept under its own id: the first fallback promotes
    its own handle, its cancel ends it, then the second does the same."""
    from raven.rpc.methods import turn as turn_mod
    from raven.rpc.methods.turn import turn_cancel

    loop, emitter, scheduler, turn_ids, teardown = await _spine_with_injects()
    lane = "acp:w3"
    try:
        host = await turn_send(
            {"session_key": lane, "content": "wake"}, emitter=emitter, scheduler=scheduler, turn_ids=turn_ids
        )
        await asyncio.wait_for(loop.started.setdefault("wake", asyncio.Event()).wait(), 2)
        one = await turn_send(
            {"session_key": lane, "content": "first steer", "busy": "inject"},
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
        )
        two = await turn_send(
            {"session_key": lane, "content": "second steer", "busy": "inject"},
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
        )
        assert set(turn_mod._pending_injects[lane]) == {one["turn_id"], two["turn_id"]}, (
            "both kept, not the newest only"
        )

        loop.gate("wake").set()
        await asyncio.wait_for(loop.started.setdefault("first steer", asyncio.Event()).wait(), 2)
        for _ in range(5):
            await asyncio.sleep(0)

        assert turn_ids[lane] == one["turn_id"] and turn_mod.is_turn_active(lane), "the first fallback owns the lane"
        starts = [e["payload"] for _k, e in emitter.emitted if e["type"] == "message.start"]
        assert [p["turn_id"] for p in starts] == [host["turn_id"], one["turn_id"]]
        assert starts[-1]["content"] == "first steer"
        assert set(turn_mod._pending_injects[lane]) == {two["turn_id"]}, "the second is still pending, by its own id"

        assert await turn_cancel({"session_key": lane}, emitter=emitter, turn_ids=turn_ids) == {"cancelled": True}
        errors = [e["payload"]["turn_id"] for _k, e in emitter.emitted if e["type"] == "error"]
        assert errors[-1] == one["turn_id"], "the cancel ended the running first fallback, not the pending second"

        await asyncio.wait_for(loop.started.setdefault("second steer", asyncio.Event()).wait(), 2)
        for _ in range(5):
            await asyncio.sleep(0)
        assert turn_ids[lane] == two["turn_id"] and turn_mod.is_turn_active(lane), "then the second is promoted in turn"
        starts = [e["payload"]["turn_id"] for _k, e in emitter.emitted if e["type"] == "message.start"]
        assert starts[-1] == two["turn_id"]
        assert await turn_cancel({"session_key": lane}, emitter=emitter, turn_ids=turn_ids) == {"cancelled": True}
        assert lane not in turn_mod._pending_injects and not turn_mod.is_turn_active(lane)
        assert loop.saw == [("wake", []), ("first steer", []), ("second steer", [])]
    finally:
        for gate in loop.release.values():
            gate.set()
        await teardown()
