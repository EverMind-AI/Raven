"""Tests for ``imported.upload`` and the read-only guard (`raven.rpc.methods.imported`)."""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from raven.config.loader import load_config
from raven.rpc.approval_broker import ApprovalBroker
from raven.rpc.confirm_broker import ConfirmBroker
from raven.rpc.credential_broker import CredentialBroker
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import imported as imported_rpc
from raven.rpc.methods import register_aligned_methods_except_system
from raven.rpc.methods.system import register_system_methods
from raven.rpc.question_broker import QuestionBroker
from raven.rpc.subscriptions import SubscriptionEmitter
from raven.rpc.transports.ws import _mount_imported
from raven.session.manager import SessionManager
from raven.trajectory import imported
from raven.trajectory import imported_package as ipkg
from raven.trajectory import index as tidx
from raven.trajectory import policy as tpol
from tests import imported_package_builders as builders
from tests.test_trajectory_imported import _export_bytes


@pytest.fixture(autouse=True)
def runner(monkeypatch) -> Any:
    tpol._reset_for_tests()
    tpol.arm(True)
    tidx._reset_for_tests()
    fresh = imported.ImportRunner()
    monkeypatch.setattr(imported, "RUNNER", fresh)
    yield fresh
    assert fresh.stop(timeout=10)
    tidx._reset_for_tests()
    tpol._reset_for_tests()


async def _sink(_frame: dict[str, Any]) -> None:
    return None


@pytest.fixture
def dispatcher() -> Dispatcher:
    """Every method production registers, the brokered and streaming ones included."""
    made = Dispatcher()
    register_aligned_methods_except_system(
        made,
        emitter=SubscriptionEmitter(send_frame=_sink),
        approval_broker=ApprovalBroker(send_frame=_sink),
        credential_broker=CredentialBroker(send_frame=_sink),
        confirm_broker=ConfirmBroker(send_frame=_sink),
        question_broker=QuestionBroker(send_frame=_sink),
    )
    register_system_methods(made)
    return made


async def _call(dispatcher: Dispatcher, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def _code(response: dict[str, Any]) -> int | None:
    error = response.get("error")
    return error["code"] if error else None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _upload(data: bytes, name: str = "pkg.tar.gz", **extra: Any) -> dict[str, Any]:
    return {"name": name, "content_b64": _b64(data), **extra}


def _staged() -> list[Path]:
    staging = imported.imports_root() / imported.STAGING_DIR
    return sorted(staging.iterdir()) if staging.is_dir() else []


async def _until_idle(runner: imported.ImportRunner) -> None:
    deadline = time.monotonic() + 10
    while runner.busy and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not runner.busy


class _Gate:
    """Holds every import inside ``import_bytes`` until released, counting how many run at once."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()
        real = imported.import_bytes

        def held(*args: Any, **kwargs: Any) -> imported.ImportOutcome:
            with self._lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
            try:
                self.entered.set()
                assert self.release.wait(10)
                return real(*args, **kwargs)
            finally:
                with self._lock:
                    self.running -= 1

        monkeypatch.setattr(imported, "import_bytes", held)


# ── imported.upload ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "build,kind",
    [
        (builders.build_export, "export"),
        (builders.build_report, "report"),
        (builders.build_bug_report, "bugreport"),
    ],
)
async def test_upload_imports_a_package(tmp_path, dispatcher, build, kind):
    package, _ = build(tmp_path / "src")
    response = await _call(dispatcher, "imported.upload", _upload(package.read_bytes(), "C:\\pkgs\\" + package.name))
    result = response["result"]
    assert result["status"] == "imported"
    assert result["kind"] == kind
    assert result["source_name"] == package.name
    assert imported.parse_key(result["session_key"]) is not None
    assert result["title"]
    assert result["imported_at"].endswith("+00:00")
    assert [c.key for c in imported.list_conversations()] == [result["session_key"]]


async def test_the_same_package_again_is_a_duplicate_unless_forced(tmp_path, dispatcher):
    data = _export_bytes()
    first = (await _call(dispatcher, "imported.upload", _upload(data)))["result"]
    again = (await _call(dispatcher, "imported.upload", _upload(data, "renamed.tar.gz")))["result"]
    assert again["status"] == "duplicate"
    assert (again["session_key"], again["title"], again["source_name"]) == (
        first["session_key"],
        first["title"],
        first["source_name"],
    )
    assert len(imported.list_conversations()) == 1
    forced = (await _call(dispatcher, "imported.upload", _upload(data, force=True)))["result"]
    assert forced["status"] == "imported" and forced["session_key"] != first["session_key"]
    assert len(imported.list_conversations()) == 2


@pytest.mark.parametrize(
    "params",
    [
        {"content_b64": "AAAA"},
        {"name": "x.tar.gz"},
        {"name": "x.tar.gz", "content_b64": "AAAA", "force": "yes"},
        {"name": "x.tar.gz", "content_b64": "AAAA", "extra": 1},
        {"name": 7, "content_b64": "AAAA"},
    ],
)
async def test_bad_params(dispatcher, params):
    assert _code(await _call(dispatcher, "imported.upload", params)) == -32602


async def test_a_refused_package_says_why(dispatcher):
    response = await _call(dispatcher, "imported.upload", _upload(b"this is not a package"))
    assert _code(response) == -32030
    assert response["error"]["data"]["reason"] == "not_importable"
    assert response["error"]["data"]["detail"]
    assert imported.list_conversations() == []
    assert _staged() == []


async def test_text_that_is_not_base64(dispatcher):
    response = await _call(dispatcher, "imported.upload", {"name": "x", "content_b64": "not base64!"})
    assert (_code(response), response["error"]["data"]["reason"]) == (-32030, ipkg.CODE_BAD_ENCODING)


async def test_too_large_is_refused_before_anything_is_decoded(dispatcher, runner, monkeypatch):
    submitted: list[str] = []
    monkeypatch.setattr(runner, "try_submit", lambda *a, **k: submitted.append("called"))
    monkeypatch.setattr(imported_rpc, "MAX_UPLOAD_BYTES", 5)
    response = await _call(dispatcher, "imported.upload", _upload(b"123456"))
    assert (_code(response), response["error"]["data"]["reason"]) == (-32030, "too_large")
    assert submitted == []
    await _call(dispatcher, "imported.upload", _upload(b"12345"))
    assert submitted == ["called"]


@pytest.mark.parametrize("size", range(0, 9))
def test_decoded_size_is_exact(size):
    assert imported_rpc._decoded_size(_b64(b"x" * size)) == size


# ── one import at a time, owned by its thread ──────────────────────────


async def test_a_caller_that_goes_away_does_not_free_the_slot(dispatcher, runner, monkeypatch):
    gate = _Gate(monkeypatch)
    data = _export_bytes()
    first = asyncio.ensure_future(_call(dispatcher, "imported.upload", _upload(data)))
    assert await asyncio.to_thread(gate.entered.wait, 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    busy = await _call(dispatcher, "imported.upload", _upload(data))
    assert _code(busy) == -32031
    assert len(_staged()) <= 1

    gate.release.set()
    await _until_idle(runner)
    assert len(imported.list_conversations()) == 1
    third = (await _call(dispatcher, "imported.upload", _upload(data)))["result"]
    assert third["status"] == "duplicate"
    assert gate.peak == 1


async def test_a_failed_import_frees_the_slot(dispatcher, runner):
    failed = await _call(dispatcher, "imported.upload", _upload(b"broken"))
    assert _code(failed) == -32030
    await _until_idle(runner)
    assert _staged() == []
    ok = await _call(dispatcher, "imported.upload", _upload(_export_bytes()))
    assert ok["result"]["status"] == "imported"


async def test_requests_arriving_together_get_one_slot(dispatcher, runner, monkeypatch):
    gate = _Gate(monkeypatch)
    data = _export_bytes()
    first = asyncio.ensure_future(_call(dispatcher, "imported.upload", _upload(data)))
    assert await asyncio.to_thread(gate.entered.wait, 5)
    others = await asyncio.gather(*(_call(dispatcher, "imported.upload", _upload(data, force=True)) for _ in range(4)))
    assert [_code(r) for r in others] == [-32031] * 4
    gate.release.set()
    assert (await first)["result"]["status"] == "imported"
    assert len(imported.list_conversations()) == 1
    assert gate.peak == 1


def _page() -> web.Application:
    """One generation of the gateway's page, as far as imports go."""
    app = web.Application()
    _mount_imported(app)
    app.freeze()
    return app


def _waits_for_stop(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    entered = threading.Event()
    real_unpack = ipkg.unpack

    def slow_unpack(package: Path, dest: Path, **kwargs: Any) -> ipkg.Unpacked:
        entered.set()
        deadline = time.monotonic() + 10
        while not kwargs["should_stop"]() and time.monotonic() < deadline:
            time.sleep(0.01)
        return real_unpack(package, dest, **kwargs)

    monkeypatch.setattr(ipkg, "unpack", slow_unpack)
    return entered


async def test_closing_the_page_ends_a_running_upload(dispatcher, runner, monkeypatch):
    page = _page()
    await page.startup()
    entered = _waits_for_stop(monkeypatch)
    upload = asyncio.ensure_future(_call(dispatcher, "imported.upload", _upload(_export_bytes())))
    assert await asyncio.to_thread(entered.wait, 5)
    await page.shutdown()
    response = await upload
    assert _code(response) == -32031
    assert _staged() == []
    assert imported.list_conversations() == []
    await page.cleanup()


async def test_a_reloaded_page_takes_imports_again(tmp_path, dispatcher, runner):
    data = _export_bytes()
    first = _page()
    await first.startup()
    assert (await _call(dispatcher, "imported.upload", _upload(data)))["result"]["status"] == "imported"
    await first.shutdown()
    await first.cleanup()

    second = _page()
    await second.startup()
    assert (await _call(dispatcher, "imported.upload", _upload(data)))["result"]["status"] == "duplicate"
    package, _ = builders.build_report(tmp_path / "src")
    assert (await _call(dispatcher, "imported.upload", _upload(package.read_bytes())))["result"]["status"] == "imported"
    await second.shutdown()
    await second.cleanup()


async def test_a_new_page_waits_for_an_import_the_old_one_could_not_stop(dispatcher, runner, monkeypatch):
    gate = _Gate(monkeypatch)
    real_stop = runner.stop
    monkeypatch.setattr(runner, "stop", lambda timeout: real_stop(0.05))
    first = _page()
    await first.startup()
    upload = asyncio.ensure_future(_call(dispatcher, "imported.upload", _upload(_export_bytes())))
    assert await asyncio.to_thread(gate.entered.wait, 5)
    await first.shutdown()
    await first.cleanup()

    second = _page()
    await second.startup()
    assert _code(await _call(dispatcher, "imported.upload", _upload(_export_bytes(), force=True))) == -32031
    gate.release.set()
    assert _code(await upload) == -32031
    await _until_idle(runner)
    assert _staged() == []
    again = await _call(dispatcher, "imported.upload", _upload(_export_bytes()))
    assert again["result"]["status"] == "imported"
    assert gate.peak == 1
    await second.shutdown()
    await second.cleanup()


# ── the guard ──────────────────────────────────────────────────────────

KEY = "imported:" + "a" * 32


async def test_every_method_but_the_reads_is_refused_on_an_imported_key(dispatcher):
    refused_elsewhere = []
    for method in dispatcher.methods():
        if method in imported_rpc.ALLOWED or method == "session.set_mode":
            continue
        for name in imported_rpc.SESSION_PARAMS:
            response = await _call(dispatcher, method, {name: KEY})
            if _code(response) != -32029 or response["error"]["data"]["method"] != method:
                refused_elsewhere.append((method, name, response.get("error")))
        response = await _call(dispatcher, method, {"channel": "imported", "chat_id": "x"})
        if _code(response) != -32029:
            refused_elsewhere.append((method, "channel", response.get("error")))
    assert refused_elsewhere == []


@pytest.mark.parametrize(
    "method,params",
    [
        ("turn.send", {"session_key": KEY, "text": "hello"}),
        ("session.branch", {"session_id": KEY}),
        ("session.clear", {"session_id": KEY}),
        ("session.undo", {"session_id": KEY}),
        ("session.compress", {"session_id": KEY}),
        ("session.pin", {"session_id": KEY, "pinned": True}),
        ("session.archive", {"session_id": KEY, "archived": True}),
        ("session.set_harness", {"session_key": KEY, "harness": "x"}),
        ("session.set_knowledge", {"session_id": KEY, "knowledge_bases": []}),
        ("trajectory.turns", {"session_key": KEY}),
        ("trajectory.export", {"session_key": KEY, "trace_ids": ["t"], "file_name": "x"}),
        ("bugreport.start", {"session_key": KEY, "trace_ids": ["t"]}),
        ("fs.upload", {"name": "a", "content_b64": "", "session": KEY}),
        ("config.set", {"session_id": KEY, "key": "model", "value": "x"}),
    ],
)
async def test_the_writes_named_by_the_requirement_are_refused(dispatcher, method, params):
    response = await _call(dispatcher, method, params)
    assert _code(response) == -32029
    assert response["error"]["message"] == "imported_read_only"


async def test_set_mode_may_report_but_not_change(dispatcher):
    assert _code(await _call(dispatcher, "session.set_mode", {"session_key": KEY})) != -32029
    assert _code(await _call(dispatcher, "session.set_mode", {"session_key": KEY, "mode": "max"})) == -32029
    assert _code(await _call(dispatcher, "session.set_mode", {"session_key": KEY, "clear": True})) == -32029


def test_ordinary_keys_pass_the_guard():
    for method, params in [
        ("session.branch", {"session_id": "tui:x"}),
        ("session.set_mode", {"session_key": "tui:x", "mode": "max"}),
        ("turn.send", {"session_key": "tui:imported:x", "text": "imported: a word the user typed"}),
        ("turn.send", {"channel": "tui", "chat_id": "imported:x"}),
    ]:
        imported_rpc.guard(method, params)


async def test_opening_an_imported_conversation_creates_no_session(tmp_path, dispatcher, monkeypatch):
    package, _ = builders.build_export(tmp_path / "src")
    key = (await _call(dispatcher, "imported.upload", _upload(package.read_bytes())))["result"]["session_key"]
    created: list[str] = []
    real = SessionManager.get_or_create

    def spy(self: SessionManager, session_key: str) -> Any:
        created.append(session_key)
        return real(self, session_key)

    monkeypatch.setattr(SessionManager, "get_or_create", spy)
    sessions = load_config().workspace_path / "sessions"
    before = sorted(p.relative_to(sessions) for p in sessions.rglob("*")) if sessions.exists() else []
    opening = [
        ("model.options", {"session_id": key}),
        ("session.set_mode", {"session_key": key}),
        ("config.get", {"keys": ["permissions.mode"], "session_id": key}),
        ("session.resume", {"session_id": key}),
        ("deliverables.list", {"session_key": key}),
        ("turn.subscribe", {"session_key": key}),
        ("tasks.list", {"session_key": key}),
        ("subagents.instances", {"session_key": key}),
        ("dag.get", {"run_id": "run-1", "session_key": key}),
        ("trajectory.list", {"session_key": key}),
        ("session.usage", {"session_id": key}),
    ]
    for method, params in opening:
        response = await _call(dispatcher, method, params)
        assert _code(response) != -32029, (method, response)
    assert [k for k in created if imported.is_imported_key(k)] == []
    after = sorted(p.relative_to(sessions) for p in sessions.rglob("*")) if sessions.exists() else []
    assert after == before
