"""Report Sessions: web bug reports in progress, from collecting to the upload receipt."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from raven.trajectory import bugreport as breport
from raven.trajectory import redaction_history as rhistory
from raven.trajectory import report_session as rs
from raven.trajectory import selection as tsel
from raven.trajectory.upload import UploadError, UploadReceipt

SESSION = "cli:web"
TOKEN = "aB3xK9mQ7pL2vR8sT4wZ6yN1"
OTHER = "qT7zK9mP4vX2sW8dQ5nRfJ3y"
# Assembled at runtime: the detect-private-key pre-commit hook scans source
# bytes for the marker substring and cannot tell this fake fixture apart.
PEM = "-----BEGIN PRIVATE " + "KEY-----\nMIIabcdef\n-----END PRIVATE " + "KEY-----"


@pytest.fixture(autouse=True)
def _isolated_secrets(monkeypatch):
    monkeypatch.setattr(breport, "collect_known_secrets", lambda _p: ([], True))


@pytest.fixture
def state(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "traces"
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(root))
    log = root / "logs" / "audit-spans.log"
    log.parent.mkdir(parents=True)
    record = {
        "traceId": "trace-a",
        "spanId": "span-a",
        "name": "session.turn",
        "startTime": "2026-10-01T10:01:00+00:00",
        "endTime": "2026-10-01T10:01:30+00:00",
        "attributes": {"session.key": SESSION, "turn.in_progress": False, "turn.input_preview": "hi"},
    }
    log.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return root


@pytest.fixture
def history_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / "redaction_history.json"


class _Uploads:
    """Stands in for upload_record; scripted outcomes, one per call."""

    def __init__(self) -> None:
        self.outcomes: list[BaseException | None] = []
        self.calls: list[Path] = []

    def __call__(self, record_dir: Path, *, base_url: str):
        self.calls.append(record_dir)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        record = breport.load_record(record_dir)
        receipt = UploadReceipt("rcpt-1", record["report_id"], record["package"]["sha256"], 1, "now", False)
        return record, receipt


@pytest.fixture
def uploads(monkeypatch) -> _Uploads:
    fake = _Uploads()
    monkeypatch.setattr(rs, "upload_record", fake)
    return fake


@pytest.fixture
def make(state: Path, tmp_path: Path, history_path: Path) -> Iterator[Callable[..., rs.ReportSessions]]:
    built: list[rs.ReportSessions] = []

    def build(**overrides: Any) -> rs.ReportSessions:
        options: dict[str, Any] = {
            "history": lambda: rhistory.RedactionHistory(history_path),
            "history_enabled": lambda: True,
            "upload_url": lambda: "https://receiver.example",
            "workspace": lambda: tmp_path / "ws",
            "state_dir": lambda: state,
        }
        options.update(overrides)
        built.append(rs.ReportSessions(**options))
        return built[-1]

    yield build
    # A worker still running would call into the next test's stand-ins.
    for reports in built:
        assert reports.shutdown(timeout=10)


def _plan(state: Path) -> tsel.SelectionPlan:
    return tsel.plan_selection(SESSION, ["trace-a"], answering=False, state_dir=state)


def _problem(description: str = f"the token {TOKEN} leaked") -> dict[str, str]:
    return {"description": description, "expected": "", "actual": "", "severity": "medium", "steps": "", "reporter": ""}


def _wait(reports: rs.ReportSessions, report_id: str, phase: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        view = reports.status(report_id)
        if view["phase"] == phase:
            return view
        assert view["phase"] not in ("failed",) or phase == "failed", view
        assert time.monotonic() < deadline, f"still {view['phase']}, wanted {phase}"
        time.sleep(0.02)


def _quiet(reports: rs.ReportSessions, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while any(r.worker_alive for r in list(reports._reports.values())):
        assert time.monotonic() < deadline
        time.sleep(0.02)


def _decide_all(view: dict[str, Any], action: str = "kept") -> tuple[list[tuple[str, str]], list[str]]:
    decisions = [(item["id"], action) for item in view["items"] if item["kind"] == "suspected"]
    acknowledged = [item["id"] for item in view["items"] if item["kind"] == "confirmed"]
    return decisions, acknowledged


def _staging(state: Path) -> list[Path]:
    root = breport.bugreports_root(state) / breport.STAGING_DIR
    return sorted(root.iterdir()) if root.is_dir() else []


# -- the happy path ------------------------------------------------------------


def test_a_report_runs_from_collecting_to_its_receipt(make, state, uploads):
    reports = make()
    rid = reports.start(_plan(state), _problem())
    view = _wait(reports, rid, "deciding")
    assert view["bug_report_id"].startswith("br-")
    [item] = view["items"]
    assert item["kind"] == "suspected" and TOKEN not in json.dumps(view), "the page never sees the plain value"
    assert item["count"] >= 1 and item["sources"]
    contexts = reports.item(rid, item["id"])
    assert contexts["total_groups"] >= 1 and TOKEN in contexts["groups"][0]["text"]
    reports.decide(rid, *_decide_all(view, "redacted"))
    view = _wait(reports, rid, "ready")
    assert view["summary"]["redacted"] == 1 and view["summary"]["classification"] == "needs_review"
    reports.upload(rid, risk_accepted=True)
    view = _wait(reports, rid, "uploaded")
    assert view["receipt"] == {"receipt_id": "rcpt-1", "duplicate": False}
    assert view["record_dir"] and view["summary"]["size_bytes"] > 0
    reports.cancel(rid)
    _quiet(reports)
    assert Path(view["record_dir"]).is_dir(), "a landed report keeps its record"
    with pytest.raises(rs.ReportNotFoundError):
        reports.status(rid)


def test_a_clean_report_skips_deciding(make, state, uploads):
    reports = make()
    rid = reports.start(_plan(state), _problem("plain words only"))
    view = _wait(reports, rid, "ready")
    assert view["items"] is None and view["summary"]["classification"] == "clean"
    reports.upload(rid, risk_accepted=False)
    _wait(reports, rid, "uploaded")


# -- the redaction history (R7) --------------------------------------------------


def test_every_decision_made_on_the_page_is_remembered_as_web(make, state, uploads, history_path):
    reports = make()
    rid = reports.start(_plan(state), _problem(f"{TOKEN} and {OTHER}"))
    view = _wait(reports, rid, "deciding")
    decisions, acknowledged = _decide_all(view, "kept")
    reports.decide(rid, decisions, acknowledged)
    _wait(reports, rid, "ready")
    entries = rhistory.RedactionHistory(history_path).read().entries
    assert len(entries) == 2 and {e.source for e in entries} == {"web"} and {e.action for e in entries} == {"kept"}


def test_a_second_report_is_decided_by_the_history_and_counts_the_reuse(make, state, uploads, history_path):
    first = make()
    rid = first.start(_plan(state), _problem())
    view = _wait(first, rid, "deciding")
    first.decide(rid, *_decide_all(view, "redacted"))
    _wait(first, rid, "ready")
    first.cancel(rid)
    _quiet(first)

    second = make()
    rid = second.start(_plan(state), _problem())
    view = _wait(second, rid, "ready")
    assert view["history"]["auto_redacted"] == 1 and view["history"]["state"] == "on"
    [entry] = rhistory.RedactionHistory(history_path).read().entries
    assert entry.applied == 1


@pytest.mark.parametrize("mode", ["off", "corrupt", "unwritable"])
def test_a_report_completes_whatever_state_the_history_is_in(make, state, uploads, history_path, mode, monkeypatch):
    overrides: dict[str, Any] = {}
    if mode == "off":
        overrides["history_enabled"] = lambda: False
    elif mode == "corrupt":
        history_path.parent.mkdir(parents=True)
        history_path.write_text("{not json", encoding="utf-8")
    else:

        def refuse(self, *a, **k):
            raise rhistory.HistoryStorageError("disk full")

        monkeypatch.setattr(rhistory.RedactionHistory, "remember", refuse)
    reports = make(**overrides)
    rid = reports.start(_plan(state), _problem())
    view = _wait(reports, rid, "deciding")
    expected_state = {"off": "off", "corrupt": "corrupt", "unwritable": "on"}[mode]
    assert view["history"]["state"] == expected_state
    reports.decide(rid, *_decide_all(view))
    view = _wait(reports, rid, "ready")
    if mode == "unwritable":
        assert "not remembered" in view["history"]["warning"]
    if mode == "corrupt":
        assert view["history"]["warning"]


# -- what decide refuses ---------------------------------------------------------


def test_a_confirmed_item_must_be_acknowledged(make, state, uploads):
    reports = make()
    rid = reports.start(_plan(state), _problem(f"key {PEM}"))
    view = _wait(reports, rid, "deciding")
    assert view["items"][0]["kind"] == "confirmed"
    decisions, _ack = _decide_all(view)
    with pytest.raises(rs.ReportStateError) as excinfo:
        reports.decide(rid, decisions, [])
    assert excinfo.value.reason == "not_acknowledged" and excinfo.value.item_ids == [view["items"][0]["id"]]
    reports.decide(rid, *_decide_all(view))
    _wait(reports, rid, "ready")


def test_linked_items_that_disagree_are_named_and_can_be_decided_again(make, state, uploads):
    inner = "Hj5tR8uE3iO7pA1sD4fGk9lZ"
    outer = inner + "W6xC2v"
    reports = make()
    rid = reports.start(_plan(state), _problem(f"{inner} then {outer}"))
    view = _wait(reports, rid, "deciding")
    ids = [item["id"] for item in view["items"]]
    with pytest.raises(rs.ReportConflictError) as excinfo:
        reports.decide(rid, [(ids[0], "kept"), (ids[1], "redacted")], [])
    assert sorted(excinfo.value.item_ids) == sorted(ids)
    assert reports.status(rid)["phase"] == "deciding"
    reports.decide(rid, [(ids[0], "kept"), (ids[1], "kept")], [])
    _wait(reports, rid, "ready")


def test_undecided_and_unknown_items_are_refused(make, state, uploads):
    reports = make()
    rid = reports.start(_plan(state), _problem())
    view = _wait(reports, rid, "deciding")
    with pytest.raises(rs.ReportStateError) as excinfo:
        reports.decide(rid, [], [])
    assert excinfo.value.reason == "undecided"
    with pytest.raises(rs.ReportStateError) as excinfo:
        reports.decide(rid, [("nope", "kept")], [])
    assert excinfo.value.reason == "unknown_item"
    with pytest.raises(rs.ReportStateError):
        reports.item(rid, "nope")
    reports.decide(rid, *_decide_all(view))
    _wait(reports, rid, "ready")


def test_a_report_needing_review_is_not_uploaded_without_the_risk_accepted(make, state, uploads):
    reports = make()
    rid = reports.start(_plan(state), _problem())
    view = _wait(reports, rid, "deciding")
    reports.decide(rid, *_decide_all(view))
    _wait(reports, rid, "ready")
    with pytest.raises(rs.ReportStateError) as excinfo:
        reports.upload(rid, risk_accepted=False)
    assert excinfo.value.reason == "risk_not_accepted" and uploads.calls == []
    reports.upload(rid, risk_accepted=True)
    _wait(reports, rid, "uploaded")


# -- delivering the package ---------------------------------------------------------


def test_a_failed_upload_can_be_retried(make, state, uploads):
    uploads.outcomes = [UploadError("receiver unreachable", retryable=True)]
    reports = make()
    rid = reports.start(_plan(state), _problem("plain words only"))
    _wait(reports, rid, "ready")
    reports.upload(rid, risk_accepted=False)
    view = _wait(reports, rid, "upload_failed")
    assert view["error"] == {"message": "receiver unreachable", "retryable": True, "status": None}
    reports.retry_upload(rid)
    assert _wait(reports, rid, "uploaded")["receipt"]["receipt_id"] == "rcpt-1"
    assert len(uploads.calls) == 2


def test_a_refused_upload_is_not_retryable(make, state, uploads):
    uploads.outcomes = [UploadError("bad request", status=400, retryable=False)]
    reports = make()
    rid = reports.start(_plan(state), _problem("plain words only"))
    _wait(reports, rid, "ready")
    reports.upload(rid, risk_accepted=False)
    assert _wait(reports, rid, "upload_failed")["error"]["status"] == 400
    with pytest.raises(rs.ReportStateError) as excinfo:
        reports.retry_upload(rid)
    assert excinfo.value.reason == "not_retryable"


def test_an_oversized_package_is_never_sent(make, state, uploads, monkeypatch):
    monkeypatch.setattr(rs, "PackageLimits", lambda: type("L", (), {"max_package_bytes": 1})())
    reports = make()
    rid = reports.start(_plan(state), _problem("plain words only"))
    _wait(reports, rid, "ready")
    reports.upload(rid, risk_accepted=False)
    view = _wait(reports, rid, "upload_failed")
    assert view["error"]["retryable"] is False and "kept at" in view["error"]["message"]
    assert uploads.calls == [] and Path(view["record_dir"]).is_dir()


def test_an_oversized_package_found_on_the_retry_path_is_never_sent(make, state, uploads, monkeypatch):
    """Packaging fails first, so the size check has not run; the retry repackages
    a package over the limit, and it must still not be sent."""
    calls = {"n": 0}
    real_verify = breport._verify_export

    def flaky_verify(record, export_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real_verify(record, export_dir)

    monkeypatch.setattr(breport, "_verify_export", flaky_verify)
    reports = make()
    rid = reports.start(_plan(state), _problem("plain words only"))
    _wait(reports, rid, "ready")
    reports.upload(rid, risk_accepted=False)
    view = _wait(reports, rid, "upload_failed")
    assert view["error"]["retryable"] is True
    monkeypatch.setattr(rs, "PackageLimits", lambda: type("L", (), {"max_package_bytes": 1})())
    reports.retry_upload(rid)
    view = _wait(reports, rid, "upload_failed")
    assert view["error"]["retryable"] is False and "kept at" in view["error"]["message"]
    assert uploads.calls == []
    record = breport.load_record(Path(view["record_dir"]))
    assert record["status"] == breport.STATUS_LOCAL_READY and Path(record["package"]["path"]).is_file()
    with pytest.raises(rs.ReportStateError):
        reports.retry_upload(rid)


# -- one worker at a time, slots held until it exits -----------------------------


class _Barrier:
    def __init__(self) -> None:
        self.open = threading.Event()
        self.entered = threading.Semaphore(0)
        self.calls = 0

    def wrap(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def blocked(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            self.entered.release()
            assert self.open.wait(10)
            return real(*args, **kwargs)

        return blocked


@pytest.fixture
def blocked_remember(monkeypatch) -> _Barrier:
    barrier = _Barrier()
    monkeypatch.setattr(rhistory.RedactionHistory, "remember", barrier.wrap(rhistory.RedactionHistory.remember))
    return barrier


def test_a_blocked_history_write_does_not_hold_up_status_or_cancel(make, state, uploads, blocked_remember):
    reports = make()
    first = reports.start(_plan(state), _problem())
    second = reports.start(_plan(state), _problem())
    view = _wait(reports, first, "deciding")
    _wait(reports, second, "deciding")
    reports.decide(first, *_decide_all(view))
    assert blocked_remember.entered.acquire(timeout=5)
    started = time.monotonic()
    assert reports.status(first)["phase"] == "finishing"
    assert reports.status(second)["phase"] == "deciding"
    reports.cancel(second)
    assert time.monotonic() - started < 1.0
    with pytest.raises(rs.ReportStateError):
        reports.decide(first, *_decide_all(view))
    blocked_remember.open.set()
    _wait(reports, first, "ready")
    assert blocked_remember.calls == 1, "the second decide started no worker and wrote nothing"


def test_cancelled_workers_keep_their_slots_until_they_exit(make, state, uploads, blocked_remember):
    reports = make()
    ids = [reports.start(_plan(state), _problem()) for _ in range(2)]
    for rid in ids:
        view = _wait(reports, rid, "deciding")
        reports.decide(rid, *_decide_all(view))
    assert blocked_remember.entered.acquire(timeout=5) and blocked_remember.entered.acquire(timeout=5)
    for rid in ids:
        reports.cancel(rid)
        assert reports.status(rid)["phase"] == "cancelled"
    staging_before = _staging(state)
    threads_before = len([t for t in threading.enumerate() if t.name.startswith(rs.THREAD_PREFIX)])
    with pytest.raises(rs.ReportLimitError):
        reports.start(_plan(state), _problem())
    assert len([t for t in threading.enumerate() if t.name.startswith(rs.THREAD_PREFIX)]) == threads_before
    assert _staging(state) == staging_before
    blocked_remember.open.set()
    _quiet(reports)
    assert _staging(state) == [] and reports._reports == {}
    rid = reports.start(_plan(state), _problem())
    # The cancelled workers still recorded their decisions before cleaning up.
    _wait(reports, rid, "ready")


def test_waiting_reports_hold_their_slots(make, state, uploads):
    reports = make()
    ids = [reports.start(_plan(state), _problem()) for _ in range(2)]
    for rid in ids:
        _wait(reports, rid, "deciding")
    with pytest.raises(rs.ReportLimitError):
        reports.start(_plan(state), _problem())
    reports.cancel(ids[0])
    _quiet(reports)
    assert reports.start(_plan(state), _problem())


@pytest.mark.parametrize("phase", ["deciding", "ready", "failed"])
def test_cancelling_before_landing_removes_the_staging(make, state, uploads, phase, monkeypatch):
    if phase == "failed":
        monkeypatch.setattr(breport, "finish_export", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    reports = make()
    rid = reports.start(_plan(state), _problem())
    view = _wait(reports, rid, "deciding")
    if phase != "deciding":
        reports.decide(rid, *_decide_all(view))
        _wait(reports, rid, phase)
    reports.cancel(rid)
    _quiet(reports)
    assert _staging(state) == [] and reports._reports == {}


def test_an_idle_report_is_cancelled(make, state, uploads):
    now = [100.0]
    reports = make(clock=lambda: now[0], idle=60.0)
    rid = reports.start(_plan(state), _problem())
    _wait(reports, rid, "deciding")
    now[0] += 61
    with pytest.raises(rs.ReportNotFoundError):
        reports.cancel("rs-other")
    _quiet(reports)
    assert _staging(state) == []
    with pytest.raises(rs.ReportNotFoundError):
        reports.status(rid)


def test_shutdown_waits_for_the_cleanup(make, state, uploads):
    reports = make()
    rid = reports.start(_plan(state), _problem())
    _wait(reports, rid, "deciding")
    assert reports.shutdown(timeout=10) is True
    assert _staging(state) == [] and reports._reports == {}


def test_a_failed_collection_is_reported_and_cleaned(make, state, uploads, monkeypatch):
    def broken(*a, **k):
        raise breport.StaleAttemptError("the selected turns changed")

    monkeypatch.setattr(breport, "prepare_selection", broken)
    reports = make()
    rid = reports.start(_plan(state), _problem())
    view = _wait(reports, rid, "failed")
    assert "selected turns changed" in view["error"]["message"] and view["error"]["retryable"] is False
    reports.cancel(rid)
    _quiet(reports)
    assert reports._reports == {}


def test_unknown_reports_are_reported(make):
    reports = make()
    for call in (lambda: reports.status("x"), lambda: reports.cancel("x"), lambda: reports.item("x", "y")):
        with pytest.raises(rs.ReportNotFoundError):
            call()


# -- a cancel stops the worker at the end of its current step ----------------------


def _spy(monkeypatch, owner: Any, name: str) -> list[int]:
    calls: list[int] = []
    real = getattr(owner, name)

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)
    return calls


def _flaky_first_package(monkeypatch) -> None:
    calls = {"n": 0}
    real_verify = breport._verify_export

    def flaky_verify(record, export_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real_verify(record, export_dir)

    monkeypatch.setattr(breport, "_verify_export", flaky_verify)


@pytest.mark.parametrize("path", ["first", "retry"])
def test_a_report_cancelled_while_packaging_is_never_sent(make, state, uploads, path, monkeypatch):
    barrier = _Barrier()
    reports = make()
    rid = reports.start(_plan(state), _problem("plain words only"))
    bug_report_id = _wait(reports, rid, "ready")["bug_report_id"]
    if path == "first":
        monkeypatch.setattr(breport, "confirm_and_package", barrier.wrap(breport.confirm_and_package))
        reports.upload(rid, risk_accepted=False)
    else:
        _flaky_first_package(monkeypatch)
        reports.upload(rid, risk_accepted=False)
        assert _wait(reports, rid, "upload_failed")["error"]["retryable"] is True
        monkeypatch.setattr(breport, "retry_packaging", barrier.wrap(breport.retry_packaging))
        reports.retry_upload(rid)
    assert barrier.entered.acquire(timeout=5)
    reports.cancel(rid)
    assert reports.status(rid)["phase"] == "cancelled"
    barrier.open.set()
    _quiet(reports)
    assert uploads.calls == []
    assert reports._reports == {} and rid not in reports._ended, "nothing replaced the cancel"
    record = breport.load_record(breport.bugreports_root(state) / bug_report_id)
    assert record["status"] == breport.STATUS_LOCAL_READY and Path(record["package"]["path"]).is_file()
    assert reports.start(_plan(state), _problem("plain words only"))


@pytest.mark.parametrize(
    ("blocked", "following"),
    [
        ((breport, "prepare_selection"), (breport, "stage_review")),
        ((breport, "stage_review"), (rhistory.RedactionHistory, "resolve")),
        ((rhistory.RedactionHistory, "remember"), (breport, "finish_export")),
    ],
    ids=["preparing", "staging", "remembering"],
)
def test_a_cancel_stops_the_worker_after_its_current_step(make, state, uploads, blocked, following, monkeypatch):
    barrier = _Barrier()
    monkeypatch.setattr(blocked[0], blocked[1], barrier.wrap(getattr(blocked[0], blocked[1])))
    after = _spy(monkeypatch, following[0], following[1])
    reports = make()
    rid = reports.start(_plan(state), _problem())
    if blocked[1] == "remember":
        reports.decide(rid, *_decide_all(_wait(reports, rid, "deciding")))
    assert barrier.entered.acquire(timeout=5)
    reports.cancel(rid)
    barrier.open.set()
    _quiet(reports)
    assert after == []
    assert _staging(state) == [] and reports._reports == {}


# -- a failed or uploaded report gives its slot up --------------------------------


@pytest.mark.parametrize("end", ["failed", "uploaded"])
def test_ended_reports_release_their_slots_and_keep_their_result_for_a_while(make, state, uploads, end, monkeypatch):
    if end == "failed":
        monkeypatch.setattr(
            breport, "prepare_selection", lambda *a, **k: (_ for _ in ()).throw(breport.PreparationError("gone"))
        )
    now = [100.0]
    reports = make(clock=lambda: now[0], keep_ended=60.0)
    ids = []
    for _ in range(2):
        rid = reports.start(_plan(state), _problem("plain words only"))
        if end == "uploaded":
            _wait(reports, rid, "ready")
            reports.upload(rid, risk_accepted=False)
        _wait(reports, rid, end)
        ids.append(rid)
    _quiet(reports)
    assert reports._reports == {}, "nothing that ended holds a slot or its plaintext"
    for rid in ids:
        view = reports.status(rid)
        assert view["phase"] == end and view["items"] is None
        if end == "failed":
            assert "gone" in view["error"]["message"]
        else:
            assert view["receipt"]["receipt_id"] == "rcpt-1" and view["summary"] is not None
        with pytest.raises(rs.ReportStateError) as excinfo:
            reports.upload(rid, risk_accepted=True)
        assert excinfo.value.phase == end
    assert _staging(state) == []
    third = reports.start(_plan(state), _problem("plain words only"))
    assert third not in ids
    reports.cancel(ids[0])
    with pytest.raises(rs.ReportNotFoundError):
        reports.status(ids[0])
    now[0] += 61
    with pytest.raises(rs.ReportNotFoundError):
        reports.status(ids[1])


# -- a worker that raises still ends the report ------------------------------------


def test_a_receipt_that_cannot_be_written_is_retried_from_the_landed_package(make, state, uploads, monkeypatch):
    uploads.outcomes = [OSError("receipt record write failed")]
    prepared = _spy(monkeypatch, breport, "prepare_selection")
    packaged = _spy(monkeypatch, breport, "confirm_and_package")
    reports = make()
    rid = reports.start(_plan(state), _problem("plain words only"))
    _wait(reports, rid, "ready")
    reports.upload(rid, risk_accepted=False)
    view = _wait(reports, rid, "upload_failed")
    assert view["error"]["retryable"] is True and "receipt record write failed" in view["error"]["message"]
    assert Path(view["record_dir"]).is_dir()
    reports.retry_upload(rid)
    assert _wait(reports, rid, "uploaded")["receipt"]["receipt_id"] == "rcpt-1"
    assert len(prepared) == 1 and len(packaged) == 1 and len(uploads.calls) == 2


@pytest.mark.parametrize("error", [ValueError("record is not json"), RuntimeError("nobody expected this")])
def test_an_upload_that_raises_otherwise_ends_without_a_retry(make, state, uploads, error):
    uploads.outcomes = [error]
    reports = make()
    rid = reports.start(_plan(state), _problem("plain words only"))
    _wait(reports, rid, "ready")
    reports.upload(rid, risk_accepted=False)
    view = _wait(reports, rid, "upload_failed")
    assert view["error"]["retryable"] is False and str(error) in view["error"]["message"]
    record = breport.load_record(Path(view["record_dir"]))
    assert Path(record["package"]["path"]).is_file()
    with pytest.raises(rs.ReportStateError):
        reports.retry_upload(rid)


def test_a_finishing_step_that_raises_fails_the_report_and_frees_its_slot(make, state, uploads, monkeypatch):
    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("history exploded")

    monkeypatch.setattr(rhistory.RedactionHistory, "remember", broken)
    reports = make(limit=1)
    rid = reports.start(_plan(state), _problem())
    reports.decide(rid, *_decide_all(_wait(reports, rid, "deciding")))
    assert "history exploded" in _wait(reports, rid, "failed")["error"]["message"]
    _quiet(reports)
    assert _staging(state) == [] and reports._reports == {}
    assert reports.start(_plan(state), _problem())


def test_a_cancel_is_not_overwritten_by_the_failure_that_follows_it(make, state, uploads, monkeypatch):
    barrier = _Barrier()
    uploads.outcomes = [OSError("too late")]
    monkeypatch.setattr(rs, "upload_record", barrier.wrap(uploads))
    reports = make()
    rid = reports.start(_plan(state), _problem("plain words only"))
    _wait(reports, rid, "ready")
    reports.upload(rid, risk_accepted=False)
    assert barrier.entered.acquire(timeout=5)
    reports.cancel(rid)
    barrier.open.set()
    _quiet(reports)
    assert len(uploads.calls) == 1, "an upload already under way is not taken back"
    assert reports._reports == {} and rid not in reports._ended


class _NoThreads:
    class Thread:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")


def test_a_thread_that_does_not_start_leaves_the_report_where_it_was(make, state, uploads, monkeypatch):
    reports = make()
    rid = reports.start(_plan(state), _problem())
    view = _wait(reports, rid, "deciding")
    monkeypatch.setattr(rs, "threading", _NoThreads)
    with pytest.raises(RuntimeError):
        reports.decide(rid, *_decide_all(view))
    assert reports.status(rid)["phase"] == "deciding"
    with pytest.raises(RuntimeError):
        reports.start(_plan(state), _problem())
    assert list(reports._reports) == [rid]
    monkeypatch.setattr(rs, "threading", threading)
    reports.decide(rid, *_decide_all(view))
    _wait(reports, rid, "ready")
