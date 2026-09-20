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


@pytest.fixture(autouse=True)
def _no_real_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    """The phases read the developer's own ~/.claude and copy skill trees; a
    test that wants them says so by replacing these two again."""

    async def _phases_done(items, workspace, st, **_kwargs):
        st.set_phases("done")
        return None

    monkeypatch.setattr(import_sync, "run_phases", _phases_done)
    monkeypatch.setattr(import_sync, "_skill_count", AsyncMock(return_value=0))


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
        "skills": 0,
    }
    assert by_platform["hermes"] == {
        "platform": "hermes",
        "scannable": False,
        "memory_files": 1,
        "conversations": 0,
        "estimated_size": 5,
        "skills": 0,
    }
    # A platform with no results and no scanner still gets a zeroed row.
    assert by_platform["codex"] == {
        "platform": "codex",
        "scannable": False,
        "memory_files": 0,
        "conversations": 0,
        "estimated_size": 0,
        "skills": 0,
    }


async def test_scan_counts_the_skills_a_platform_would_install(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skills never travel as scan results, and a platform can hold nothing but
    them; the wizard has to be able to show that there is something to import."""
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [])
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(import_sync, "memory_enabled", lambda *_a: True)

    async def _count(platform: Platform) -> int:
        return 3 if platform is Platform.CLAUDE_CODE else 0

    monkeypatch.setattr(import_sync, "_skill_count", _count)

    out = await import_sync.import_scan({})

    rows = {p["platform"]: p["skills"] for p in out["platforms"]}
    assert rows["claude_code"] == 3
    assert rows["hermes"] == 0


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
    assert out == {
        "running": False,
        "total": 0,
        "submitted": 0,
        "failed": 0,
        "by_platform": {},
        "phase": None,
        "phases": None,
        "tier": None,
        "platforms": [],
    }

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


async def test_status_counts_only_the_run_import_run_started(cfg: Path, state: ImportState, monkeypatch) -> None:
    """A subset run after a wider one: the entries the wider run left stay in
    the file (they are what lets this run skip a source), but they are not
    this run's, and a total measured against them contradicted itself."""
    state.mark_submitted("claude_code", "c1")
    state.mark_submitted("hermes", "h1")
    state.mark_failed("hermes", "h2", "boom")
    results = [_scan_result("c1", Platform.CLAUDE_CODE), _scan_result("c2", Platform.CLAUDE_CODE)]
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=results))
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    backend = _FakeBackend()
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: backend)

    out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})
    assert out["total"] == 2
    task = import_sync._TASK
    assert task is not None
    await task

    status = await import_sync.import_status({})
    assert status == {
        "running": False,
        "total": 2,
        "submitted": 2,
        "failed": 0,
        "by_platform": {"claude_code": {"total": 2, "submitted": 2, "failed": 0}},
        "phase": None,
        "phases": {"status": "done", "errors": []},
        "tier": "full",
        "platforms": ["claude_code"],
    }
    # The wider run's entries are still there for the next run to skip on.
    assert state.is_submitted("hermes", "h1")


async def test_status_counts_every_entry_when_the_cli_started_the_run(state: ImportState) -> None:
    """``raven import run`` records a total and no keys; then the file's every
    entry is the scope, as ``raven import status`` has always counted it."""
    state.mark_submitted("claude_code", "c1")
    state.mark_submitted("hermes", "h1")
    state.set_total(3)

    out = await import_sync.import_status({})
    assert out["total"] == 3
    assert out["submitted"] == 2
    assert sorted(out["by_platform"]) == ["claude_code", "hermes"]


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


# ---------------------------------------------------------------------------
# the post-import phases, and what a client needs to resume
# ---------------------------------------------------------------------------


def _one_source_run(monkeypatch: pytest.MonkeyPatch) -> _FakeBackend:
    result = _scan_result("k1", Platform.CLAUDE_CODE)
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[result]))
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    backend = _FakeBackend()
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: backend)
    return backend


async def test_run_lands_the_phases_after_the_message_pass(cfg: Path, state: ImportState, monkeypatch) -> None:
    """The web path used to stop at the message pass; the profile mirror and the
    skill install are the same two phases the CLI lands, on the same items."""
    backend = _one_source_run(monkeypatch)
    calls: list[tuple[str, object, object, object]] = []

    async def _fake_phases(items, workspace, st, *, provider, model, platforms=None, on_phase=None, cancel_path=None):
        calls.append(("phases", [r.source_key for _s, r in items], set(platforms or ()), cancel_path))
        return None

    monkeypatch.setattr(import_sync, "run_phases", _fake_phases)

    out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "memory_files"})
    assert out["started"] is True
    await import_sync._TASK

    # The run's own scope and its stop file travel with the items, so the
    # phases install a platform's skills without a scan result to go on and
    # notice a stop asked for while they run.
    assert calls == [("phases", ["k1"], {Platform.CLAUDE_CODE}, state.cancel_path)]
    assert backend.stopped is True


async def test_a_platform_with_only_skills_still_gets_a_run(cfg: Path, state: ImportState, monkeypatch) -> None:
    """No scan result, nothing for the memory backend, and still something to
    import: the skills. The backend is left alone -- there is nothing to send
    it -- and the phases get the requested platform as their scope."""
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    backend = _FakeBackend()
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: backend)
    monkeypatch.setattr(import_sync, "_skill_count", AsyncMock(return_value=2))
    seen: list[set[Platform]] = []

    async def _fake_phases(items, workspace, st, *, platforms=None, **_kwargs):
        seen.append(set(platforms or ()))
        st.set_phases("done")
        return None

    monkeypatch.setattr(import_sync, "run_phases", _fake_phases)

    out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "memory_files"})
    assert out == {"started": True, "total": 0, "detail": ""}
    await import_sync._TASK

    assert seen == [{Platform.CLAUDE_CODE}]
    assert backend.started is False
    status = await import_sync.import_status({})
    assert status["phases"] == {"status": "done", "errors": []}
    assert (status["tier"], status["platforms"]) == ("memory_files", ["claude_code"])


async def test_nothing_at_all_to_import_is_refused(cfg: Path, state: ImportState, monkeypatch) -> None:
    monkeypatch.setattr(import_sync, "scan_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(import_sync, "build_scanners", lambda: [_FakeScanner(Platform.CLAUDE_CODE)])
    monkeypatch.setattr(import_sync, "maybe_build_memory_backend", lambda *a, **k: _FakeBackend())

    out = await import_sync.import_run({"platforms": ["claude_code"], "tier": "memory_files"})

    assert out == {"started": False, "total": 0, "detail": "nothing to import"}
    assert import_sync._TASK is None


async def test_a_cancelled_run_skips_the_phases(cfg: Path, state: ImportState, monkeypatch) -> None:
    from raven.importer.orchestrator import ImportSummary

    _one_source_run(monkeypatch)
    cancelled = ImportSummary(total=1, submitted=0, skipped=0, failed=0, errors=(), cancelled=True)
    monkeypatch.setattr(import_sync, "run_import", AsyncMock(return_value=cancelled))
    phases = AsyncMock(return_value=None)
    monkeypatch.setattr(import_sync, "run_phases", phases)

    await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})
    await import_sync._TASK

    phases.assert_not_awaited()


async def test_status_reports_the_phase_in_flight_and_forgets_it_after(
    cfg: Path, state: ImportState, monkeypatch
) -> None:
    _one_source_run(monkeypatch)
    release = asyncio.Event()

    async def _fake_phases(items, workspace, st, *, on_phase=None, **_kwargs):
        on_phase("profile", 1, 3)
        await release.wait()
        return None

    monkeypatch.setattr(import_sync, "run_phases", _fake_phases)

    await import_sync.import_run({"platforms": ["claude_code"], "tier": "full"})
    for _ in range(50):
        await asyncio.sleep(0)
        if import_sync._PHASE is not None:
            break

    during = await import_sync.import_status({})
    assert during["running"] is True
    assert during["phase"] == {"kind": "profile", "current": 1, "total": 3}

    release.set()
    await import_sync._TASK
    after = await import_sync.import_status({})
    assert after["running"] is False
    assert after["phase"] is None


async def test_status_names_the_request_a_stopped_run_was_asked_for(state: ImportState) -> None:
    """The gateway restarted under a run: the task is gone, the file is not, and
    a client that reads this can start the same request again."""
    state.set_total(
        3, keys=["claude_code:a", "claude_code:b", "hermes:c"], tier="memory_files", platforms=["claude_code", "hermes"]
    )
    state.mark_submitted("claude_code", "a")

    out = await import_sync.import_status({})

    assert out["running"] is False
    assert (out["total"], out["submitted"], out["failed"]) == (3, 1, 0)
    assert out["tier"] == "memory_files"
    assert out["platforms"] == ["claude_code", "hermes"]
    assert out["phase"] is None
    # No verdict on the phases: the run never reached them.
    assert out["phases"] is None


async def test_status_carries_how_the_phases_ended(state: ImportState) -> None:
    """Every source is settled before the phases begin, so the counts alone
    would call a run whose phases failed, or were lost, finished."""
    state.set_total(1, keys=["claude_code:a"], tier="memory_files", platforms=["claude_code"])
    state.mark_submitted("claude_code", "a")
    state.set_phases("failed", ["profile: bad byte"])

    out = await import_sync.import_status({})

    assert (out["total"], out["submitted"]) == (1, 1)
    assert out["phases"] == {"status": "failed", "errors": ["profile: bad byte"]}


async def test_profile_provider_is_none_without_credentials() -> None:
    from raven.config.schema import Config

    assert import_sync._profile_provider(Config()) is None
