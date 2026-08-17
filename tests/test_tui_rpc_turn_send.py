"""Tests for ``turn.send`` real handler.

``turn.send`` submits a turn onto the spine (build_tui Scheduler) and returns
``{turn_id, accepted}`` synchronously; the turn streams out via the hub/sink.
These tests drive the handler with a fake Scheduler + emitter (the spine path
itself is covered in ``test_tui_rpc_spine.py``).

Spec source:
- ``raven/tui_rpc/models.py`` ``TurnSendParams`` / ``TurnSendResult``
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.errors import ModelNotAvailableError, RpcError, TurnInProgressError
from raven.tui_rpc.methods.turn import register_turn_methods, turn_send


class FakeHandle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
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
    from raven.tui_rpc.methods import turn as _turn_mod

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

    assert set(result) == {"turn_id", "accepted"}
    assert result["accepted"] is True
    assert isinstance(result["turn_id"], str) and len(result["turn_id"]) >= 16
    # The turn was submitted, the slot bound, message.start emitted.
    assert len(scheduler.submitted) == 1
    assert scheduler.submitted[0].conversation == "tui:default"
    assert turn_ids["tui:default"] == result["turn_id"]
    assert emitter.types() == ["message.start"]
    assert emitter.emitted[0][1]["payload"]["turn_id"] == result["turn_id"]


async def test_turn_send_generates_unique_turn_ids() -> None:
    scheduler = FakeScheduler()
    turn_ids: dict[str, str] = {}
    r1 = await turn_send({"session_key": "tui:a", "content": "x"}, scheduler=scheduler, turn_ids=turn_ids)
    r2 = await turn_send({"session_key": "tui:b", "content": "x"}, scheduler=scheduler, turn_ids=turn_ids)
    assert r1["turn_id"] != r2["turn_id"]


async def test_turn_send_binds_active_slot_after_submit() -> None:
    from raven.tui_rpc.methods import turn as turn_mod

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
        "raven.tui_rpc.methods.turn._resolve_model",
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
    from raven.spine.scheduler import SchedulerDrainingError
    from raven.tui_rpc.methods import turn as turn_mod

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


async def test_turn_send_emits_the_build_error_cause_not_just_its_code() -> None:
    # -32603 internal_error names no cause on its own; the init crash detail and
    # the log path are what make the failure diagnosable in the transcript.
    class _BuildErr(RpcError):
        CODE = -32603
        MESSAGE = "internal_error"

    emitter = FakeEmitter()
    build_error = _BuildErr(
        "Config at ~/.raven/config.json fails schema validation",
        {"reason": "tui_init_crash", "log_path": "~/.raven/logs/tui.log"},
    )
    await turn_send(
        {"session_key": "tui:default", "content": "x"},
        emitter=emitter,
        scheduler=None,
        build_error=build_error,
    )

    payload = emitter.emitted[-1][1]["payload"]
    assert payload["detail"] == (
        "(details in ~/.raven/logs/tui.log) Config at ~/.raven/config.json fails schema validation"
    )


async def test_the_log_path_survives_the_transcript_renderer_on_a_multiline_cause() -> None:
    # The TUI renders the first line of `detail` and nothing else. The crash this
    # detail exists for is an unparseable config, whose ValidationError str() runs
    # to several lines -- so a trailing pointer was dropped for exactly the case
    # that needs it. Asserts the surviving slice, not just the whole string.
    class _BuildErr(RpcError):
        CODE = -32603
        MESSAGE = "internal_error"

    emitter = FakeEmitter()
    multiline = "2 validation errors for RavenConfig\nagents.defaults.model\n  Input should be a valid string"
    await turn_send(
        {"session_key": "tui:default", "content": "x"},
        emitter=emitter,
        scheduler=None,
        build_error=_BuildErr(multiline, {"reason": "tui_init_crash", "log_path": "~/.raven/logs/tui.log"}),
    )

    detail = emitter.emitted[-1][1]["payload"]["detail"]
    rendered = detail.split("\n")[0][:200]  # ui-tui/src/app/chatStream.ts
    assert "~/.raven/logs/tui.log" in rendered
    assert "2 validation errors for RavenConfig" in rendered


async def test_turn_send_omits_detail_when_the_build_error_has_no_cause() -> None:
    class _BuildErr(RpcError):
        CODE = -32603
        MESSAGE = "internal_error"

    emitter = FakeEmitter()
    await turn_send(
        {"session_key": "tui:default", "content": "x"},
        emitter=emitter,
        scheduler=None,
        build_error=_BuildErr(),
    )

    assert "detail" not in emitter.emitted[-1][1]["payload"]


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
    assert set(resp["result"]) == {"turn_id", "accepted"}
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
    cfg.agents.defaults.workspace = str(tmp_path)
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
