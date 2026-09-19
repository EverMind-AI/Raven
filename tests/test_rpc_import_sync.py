"""Tests for the ``import.*`` RPC handlers (raven.rpc.methods.import_sync)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from raven import home as raven_home
from raven.contracts.memory import BackendHealth, HealthCheck
from raven.importer.state import ImportState
from raven.importer.types import ImportMessage, ImportSession, Platform, ScanResult, SourceKind
from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods import import_sync

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_task_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts, and ends, with no import in flight in this module."""
    monkeypatch.setattr(import_sync, "_TASK", None)
    monkeypatch.setattr(import_sync, "_STARTING", False)


@pytest.fixture()
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated config file plus workspace, the way ``_load_workspace_and_config``
    expects to find them. ``memory`` is left unset here -- it defaults to the
    shipped backend name -- so a test that wants ``None`` overwrites the file
    itself before calling a handler."""
    path = tmp_path / "config.json"
    ws = tmp_path / "ws"
    ws.mkdir()
    path.write_text(json.dumps({"agents": {"defaults": {"workspace": str(ws)}}}), encoding="utf-8")
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    raven_home.set_config_path(path)
    return path


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ImportState:
    """Route the module's own state factory at a state file this test owns."""
    st = ImportState(path=tmp_path / "import_state.json")
    monkeypatch.setattr(import_sync, "_state", lambda: st)
    return st


def _scan_result(
    key: str,
    platform: Platform,
    kind: SourceKind = SourceKind.MEMORY_FILE,
    size: int = 10,
) -> ScanResult:
    return ScanResult(
        source_key=key,
        platform=platform,
        kind=kind,
        file_paths=(Path("/fake"),),
        estimated_size=size,
        mtime=1.0,
    )


class _FakeScanner:
    """A scanner stub that answers ``read`` with one importable message."""

    def __init__(self, platform: Platform) -> None:
        self.platform = platform

    async def scan(self) -> list[ScanResult]:
        return []

    async def read(self, result: ScanResult) -> ImportSession:
        return ImportSession(
            session_id=result.source_key,
            messages=(ImportMessage(role="user", content="hi", timestamp=1),),
        )


class _FakeBackend:
    """Records the lifecycle calls ``import_run`` and ``run_import`` make."""

    def __init__(self, *, health: BackendHealth | None = None) -> None:
        self.started = False
        self.stopped = False
        self._health = health if health is not None else BackendHealth(ready=True, checks=[])
        self.stored: list[tuple[str, list[dict]]] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def health(self) -> BackendHealth:
        return self._health

    async def recall(self, query: str, *, user_id=None, agent_id=None, top_k=10) -> list:
        return []

    async def store(self, session_id: str, messages: list[dict], *, metadata=None) -> bool:
        self.stored.append((session_id, messages))
        return True

    async def feedback(self, signals: dict) -> None:
        pass


async def _running_task() -> asyncio.Task:
    """A task that stays alive (and not done) until the caller cancels it."""

    async def _never() -> None:
        await asyncio.Event().wait()

    return asyncio.ensure_future(_never())


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# import.scan
# ---------------------------------------------------------------------------


async def test_scan_lists_every_platform_with_counts_and_scannable_flags(
    cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    results = [
        _scan_result("a", Platform.CLAUDE_CODE, SourceKind.MEMORY_FILE, size=10),
        _scan_result("b", Platform.CLAUDE_CODE, SourceKind.CONVERSATION, size=20),
        _scan_result("c", Platform.HERMES, SourceKind.MEMORY_FILE, size=5),
    ]
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=results))
    monkeypatch.setattr(import_sync, "memory_enabled", lambda *_a: True)

    out = await import_sync.import_scan({})

    assert out["ready"] is True
    assert out["reason"] == ""
    by_platform = {p["platform"]: p for p in out["platforms"]}
    assert set(by_platform) == {p.value for p in Platform}

    assert by_platform["claude_code"] == {
        "platform": "claude_code",
        "scannable": True,
        "memory_files": 1,
        "conversations": 1,
        "estimated_size": 30,
    }
    assert by_platform["hermes"] == {
        "platform": "hermes",
        "scannable": False,
        "memory_files": 1,
        "conversations": 0,
        "estimated_size": 5,
    }
    # A platform with no results and no scanner still gets a zeroed row.
    assert by_platform["codex"] == {
        "platform": "codex",
        "scannable": False,
        "memory_files": 0,
        "conversations": 0,
        "estimated_size": 0,
    }


async def test_scan_ready_false_when_backend_never_selected(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg.write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(cfg.parent / "ws")}}, "memory": {"backend": None}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [])
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[]))

    out = await import_sync.import_scan({})

    assert out["ready"] is False
    assert out["reason"] == "no memory backend is configured; finish the memory step of onboarding first"


async def test_scan_ready_false_when_everos_not_installed(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The default config leaves memory.backend at the shipped default ("everos").
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [])
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(import_sync, "everos_plugin_installed", lambda: False)

    out = await import_sync.import_scan({})

    assert out["ready"] is False
    assert out["reason"] == import_sync.everos_plugin_missing_note()


# ---------------------------------------------------------------------------
# import.run -- validation and refusal branches
# ---------------------------------------------------------------------------


async def test_run_rejects_unknown_tier() -> None:
    with pytest.raises(ConfigValidationError):
        await import_sync.import_run({"platforms": ["claude_code"], "tier": "bogus"})


async def test_run_rejects_unknown_platform() -> None:
    with pytest.raises(ConfigValidationError):
        await import_sync.import_run({"platforms": ["not_a_platform"], "tier": "full"})


async def test_run_refuses_when_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    task = await _running_task()
    monkeypatch.setattr(import_sync, "_TASK", task)
    try:
        out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})
        assert out == {"started": False, "total": 0, "detail": "an import is already running"}
    finally:
        await _cancel(task)


async def test_run_no_backend_generic_reason(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg.write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(cfg.parent / "ws")}}, "memory": {"backend": None}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: None)

    out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})

    assert out == {
        "started": False,
        "total": 0,
        "detail": "no memory backend is configured; finish the memory step of onboarding first",
    }


async def test_run_no_backend_missing_distribution_reason(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Default config: memory.backend stays at the shipped default ("everos").
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: None)
    monkeypatch.setattr(import_sync, "everos_plugin_installed", lambda: False)

    out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})

    assert out["started"] is False
    assert out["total"] == 0
    assert out["detail"] == import_sync.everos_plugin_missing_note()


async def test_run_stops_backend_when_health_is_not_ready(cfg: Path, state: ImportState, monkeypatch) -> None:
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[_scan_result("a", Platform.CLAUDE_CODE)]))
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    backend = _FakeBackend(
        health=BackendHealth(ready=False, checks=[HealthCheck(label="db", status="missing", hint="not running")])
    )
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: backend)

    out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})

    assert out == {"started": False, "total": 0, "detail": "db: not running"}
    assert backend.started is True
    assert backend.stopped is True
    assert import_sync._TASK is None


async def test_run_reports_nothing_to_import_without_starting_the_backend(
    cfg: Path, state: ImportState, monkeypatch
) -> None:
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    backend = _FakeBackend()
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: backend)

    out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})

    assert out == {"started": False, "total": 0, "detail": "nothing to import"}
    assert backend.started is False


# ---------------------------------------------------------------------------
# import.run -- the happy path, end to end
# ---------------------------------------------------------------------------


async def test_run_starts_and_completes_in_the_background(cfg: Path, state: ImportState, monkeypatch) -> None:
    result = _scan_result("k1", Platform.CLAUDE_CODE, SourceKind.MEMORY_FILE, size=10)
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[result]))
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    backend = _FakeBackend()
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: backend)

    out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "memory_files"})

    assert out == {"started": True, "total": 1, "detail": ""}
    task = import_sync._TASK
    assert task is not None
    await task

    assert state.is_submitted("claude_code", "k1")
    assert backend.started is True
    assert backend.stopped is True
    assert import_sync._TASK is None


async def test_run_dispatched_twice_at_once_starts_one_import(cfg: Path, state: ImportState, monkeypatch) -> None:
    """The rpc server dispatches frames concurrently and the handler awaits a
    scan and a backend start before it has a task to hold. Two frames in the
    same tick used to both pass the guard and submit every source twice --
    duplicates EverOS has no endpoint to delete."""
    result = _scan_result("k1", Platform.CLAUDE_CODE)

    async def _scan_that_yields(*a, **k):
        # A real suspension point: an AsyncMock returns without yielding, and
        # the two calls would then run one after the other, never overlapping.
        await asyncio.sleep(0)
        return [result]

    monkeypatch.setattr(import_sync, "scan_all", _scan_that_yields)
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    backends: list[_FakeBackend] = []

    def _build(*a, **k):
        backends.append(_FakeBackend())
        return backends[-1]

    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", _build)
    params = {"platforms": ["claude_code"], "tier": "full"}

    first, second = await asyncio.gather(import_sync.import_run(params), import_sync.import_run(params))

    assert sorted([first["started"], second["started"]]) == [False, True]
    refused = first if not first["started"] else second
    assert refused["detail"] == "an import is already running"
    task = import_sync._TASK
    if task is not None:
        await task
    assert [b.started for b in backends] == [True]
    assert [len(b.stored) for b in backends] == [1]


async def test_run_records_a_background_failure_and_frees_the_slot(cfg: Path, state: ImportState, monkeypatch) -> None:
    """Nothing awaits the background task, so a crash inside it has to be
    logged by the task itself or it is only ever seen at garbage collection."""
    result = _scan_result("k1", Platform.CLAUDE_CODE)
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[result]))
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    backend = _FakeBackend()
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: backend)
    monkeypatch.setattr(import_sync, "run_import", AsyncMock(side_effect=OSError("state file unwritable")))
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="ERROR")
    try:
        out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})
        assert out["started"] is True
        task = import_sync._TASK
        assert task is not None
        await task
    finally:
        logger.remove(sink)

    assert backend.stopped is True
    assert import_sync._TASK is None
    assert (await import_sync.import_status({}))["running"] is False
    assert any("background import failed" in line and "state file unwritable" in line for line in lines)


async def test_run_frees_the_slot_when_the_backend_will_not_stop(cfg: Path, state: ImportState, monkeypatch) -> None:
    result = _scan_result("k1", Platform.CLAUDE_CODE)
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[result]))
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])

    class _Stuck(_FakeBackend):
        async def stop(self) -> None:
            raise RuntimeError("service hung on shutdown")

    backend = _Stuck()
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: backend)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="ERROR")
    try:
        out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})
        assert out["started"] is True
        task = import_sync._TASK
        assert task is not None
        await task
    finally:
        logger.remove(sink)

    assert state.is_submitted("claude_code", "k1")
    assert import_sync._TASK is None
    assert any("did not stop cleanly" in line and "service hung" in line for line in lines)


async def test_run_clears_a_stale_cancel_file_on_a_fresh_start(cfg: Path, state: ImportState, monkeypatch) -> None:
    state.cancel_path.parent.mkdir(parents=True, exist_ok=True)
    state.cancel_path.touch()
    result = _scan_result("k1", Platform.CLAUDE_CODE)
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[result]))
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    backend = _FakeBackend()
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: backend)

    await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})
    task = import_sync._TASK
    assert task is not None
    await task

    assert state.is_submitted("claude_code", "k1")


# ---------------------------------------------------------------------------
# import.status
# ---------------------------------------------------------------------------


async def test_status_before_during_and_after_a_run(state: ImportState, monkeypatch: pytest.MonkeyPatch) -> None:
    out = await import_sync.import_status({})
    assert out == {"running": False, "total": 0, "submitted": 0, "failed": 0, "by_platform": {}}

    task = await _running_task()
    monkeypatch.setattr(import_sync, "_TASK", task)
    state.set_total(2)
    state.mark_submitted("claude_code", "a")
    state.mark_failed("hermes", "b", "boom")

    out = await import_sync.import_status({})
    assert out["running"] is True
    assert out["total"] == 2
    assert out["submitted"] == 1
    assert out["failed"] == 1
    assert out["by_platform"] == {
        "claude_code": {"total": 1, "submitted": 1, "failed": 0},
        "hermes": {"total": 1, "submitted": 0, "failed": 1},
    }

    await _cancel(task)
    out = await import_sync.import_status({})
    assert out["running"] is False
    assert out["total"] == 2


# ---------------------------------------------------------------------------
# import.stop
# ---------------------------------------------------------------------------


async def test_stop_touches_the_cancel_file_only_while_running(
    state: ImportState, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = await import_sync.import_stop({})
    assert out == {"stopped": False}
    assert not state.cancel_path.exists()

    task = await _running_task()
    monkeypatch.setattr(import_sync, "_TASK", task)
    out = await import_sync.import_stop({})
    assert out == {"stopped": True}
    assert state.cancel_path.exists()

    await _cancel(task)
