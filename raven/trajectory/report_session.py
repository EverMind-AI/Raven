"""Report Sessions: web bug reports in progress, held by the gateway between page requests.

A web bug report walks the same pipeline as ``raven trajectory report-bug``
-- collect, redact, decide, freeze, package, upload -- but across several
requests from a page that polls for each step. A :class:`ReportSessions`
registry keeps one entry per report: the preparation, the Review Items with
their plaintext tokens, what the redaction history decided, and the phase::

    collecting -> deciding -> finishing -> ready -> uploading -> uploaded
              \\----------------> finishing        \\-> upload_failed -> uploading

(``collecting`` goes straight to ``finishing`` when the history or an empty
review leaves nothing to ask; any phase may end in ``cancelled``, and a
collecting or finishing failure in ``failed``.)

Every method is safe to call from the event loop: the registry lock guards
in-memory state only, and anything touching the disk or a file lock -- the
snapshot, the redaction history, packaging, the upload -- runs on the entry's
worker thread. Each entry has at most one worker. An entry holds one of the
``limit`` slots from ``start`` until it is failed, uploaded or cancelled *and*
its worker has cleaned up and exited, so a cancelled worker blocked on the
history's file lock still counts, and cancelling and starting again cannot
pile threads up. Entries waiting on the reader (``deciding``, ``ready``,
``upload_failed``) hold their slot too; an entry nobody asked about for
``idle`` seconds is cancelled. A failed or uploaded entry gives its slot and
its plaintext up as its worker exits, leaving only what ``status`` answered
for the page to read for ``keep_ended`` seconds.

A worker checks for a cancel after every blocking step and starts nothing
further once it sees one: an upload already sent is not taken back, but a
report cancelled while it was packaging is never sent. Cancelling, or
failing, before the report landed removes its staging directory; after it
landed the record and package stay under ``<state>/bugreports/<id>/`` for
``raven trajectory upload-bug-report``.

Decisions made on the page -- defaults, bulk buttons and single choices alike
-- are recorded in the redaction history as human decisions from ``web``:
the page shows every value it asks about.
"""

from __future__ import annotations

import copy
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from raven.trajectory import bugreport as breport
from raven.trajectory import redaction_history as rhistory
from raven.trajectory import review as treview
from raven.trajectory.package_check import PackageLimits
from raven.trajectory.selection import SelectionPlan
from raven.trajectory.upload import UploadConfigError, UploadError, resolve_upload_url, upload_record

PHASE_COLLECTING = "collecting"
PHASE_DECIDING = "deciding"
PHASE_FINISHING = "finishing"
PHASE_READY = "ready"
PHASE_UPLOADING = "uploading"
PHASE_UPLOADED = "uploaded"
PHASE_UPLOAD_FAILED = "upload_failed"
PHASE_FAILED = "failed"
PHASE_CANCELLED = "cancelled"
_ENDED_PHASES = (PHASE_FAILED, PHASE_UPLOADED)

HISTORY_ON = "on"
HISTORY_OFF = "off"
HISTORY_CORRUPT = "corrupt"
HISTORY_UNAVAILABLE = "unavailable"

ITEM_GROUP_LIMIT = 10
THREAD_PREFIX = "bug-report"
_ERROR_LIMIT = 300
_ENDED_LIMIT = 16


class ReportNotFoundError(LookupError):
    def __init__(self, report_id: str) -> None:
        self.report_id = report_id
        super().__init__(f"no bug report {report_id!r} in progress")


class ReportLimitError(RuntimeError):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"{limit} bug reports are already in progress")


class ReportStateError(RuntimeError):
    """The request does not fit the report's phase, or its content is incomplete."""

    def __init__(self, phase: str, reason: str, message: str, item_ids: list[str] | None = None) -> None:
        self.phase = phase
        self.reason = reason
        self.item_ids = item_ids or []
        super().__init__(message)


class ReportConflictError(RuntimeError):
    """Linked items received different decisions; ``item_ids`` are the items involved."""

    def __init__(self, item_ids: list[str]) -> None:
        self.item_ids = item_ids
        super().__init__("linked items overlap and must share one decision")


@dataclass
class _Report:
    report_id: str
    plan: SelectionPlan
    problem: dict[str, str]
    last_seen: float
    workspace: Path | None = None
    session_path: Path | None = None
    phase: str = PHASE_COLLECTING
    error: dict[str, Any] | None = None
    prep: breport.ExportPreparation | None = None
    stage: breport.ReviewStage | None = None
    resolution: rhistory.HistoryResolution | None = None
    history_state: str = HISTORY_OFF
    history_warning: str | None = None
    remaining: list[treview.ReviewItem] = field(default_factory=list)
    decisions: list[treview.ReviewDecision] = field(default_factory=list)
    landed: Path | None = None
    size_bytes: int | None = None
    receipt: dict[str, Any] | None = None
    worker_alive: bool = False
    cancelled: bool = False


def _error(message: str, *, retryable: bool = False, status: int | None = None) -> dict[str, Any]:
    return {"message": message[:_ERROR_LIMIT], "retryable": retryable, "status": status}


class ReportSessions:
    def __init__(
        self,
        *,
        limit: int = 2,
        idle: float = 1800.0,
        keep_ended: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        history: Callable[[], rhistory.RedactionHistory] = rhistory.RedactionHistory,
        history_enabled: Callable[[], bool] = rhistory.enabled,
        upload_url: Callable[[], str] = resolve_upload_url,
        workspace: Callable[[], Path | None] = lambda: None,
        config_path: Callable[[], Path | None] = lambda: None,
        state_dir: Callable[[], Path | None] = lambda: None,
    ) -> None:
        self.limit = limit
        self.idle = idle
        self.keep_ended = keep_ended
        self._clock = clock
        self._history = history
        self._history_enabled = history_enabled
        self._upload_url = upload_url
        self._workspace = workspace
        self._config_path = config_path
        self._state_dir = state_dir
        self._lock = threading.Lock()
        self._idle_cond = threading.Condition(self._lock)
        self._reports: dict[str, _Report] = {}
        self._ended: dict[str, tuple[float, dict[str, Any]]] = {}

    # -- slots and workers ------------------------------------------------

    def _spawn(
        self,
        report: _Report,
        target: Callable[[], None],
        kind: str,
        undo: Callable[[], None] | None = None,
    ) -> None:
        """Run ``target`` as the report's worker; the caller marked ``worker_alive`` under the lock.

        An exception no step expected still ends the report in a phase the
        page can read. On the way out the worker removes the staging of a
        report cancelled or failed before it landed, then, in the same locked
        step as its last look at the report, releases the slot of a report
        that is over. When no thread starts, ``undo`` puts back under the lock
        what the caller changed in order to start one.
        """

        def body() -> None:
            try:
                target()
            except Exception as exc:  # noqa: BLE001 - a worker never takes the registry down
                logger.opt(exception=True).warning("bug report {}: worker failed", report.report_id)
                self._crashed(report, exc)
            finally:
                while True:
                    with self._lock:
                        doomed = (
                            report.prep
                            if (report.cancelled or report.phase == PHASE_FAILED) and report.landed is None
                            else None
                        )
                        if doomed is None:
                            report.worker_alive = False
                            if report.cancelled:
                                self._reports.pop(report.report_id, None)
                            elif report.phase in _ENDED_PHASES:
                                self._end_locked(report)
                            self._idle_cond.notify_all()
                            return
                        report.prep = None
                    doomed.cleanup()

        thread = threading.Thread(target=body, name=f"{THREAD_PREFIX}-{kind}-{report.report_id}", daemon=True)
        try:
            thread.start()
        except BaseException:
            with self._lock:
                report.worker_alive = False
                if undo is not None:
                    undo()
                if report.cancelled:
                    # Nothing is left to remove its staging; the stale-staging
                    # sweep at the next start does.
                    self._reports.pop(report.report_id, None)
                self._idle_cond.notify_all()
            raise

    def _occupied_locked(self) -> int:
        return len(self._reports)

    def _end_locked(self, report: _Report) -> None:
        """Release a failed or uploaded report's slot, keeping only what ``status`` answers."""
        self._reports.pop(report.report_id, None)
        self._ended[report.report_id] = (self._clock() + self.keep_ended, self._view_locked(report))
        while len(self._ended) > _ENDED_LIMIT:
            del self._ended[min(self._ended, key=lambda key: self._ended[key][0])]

    def _retire_locked(self, report: _Report) -> bool:
        """Cancel ``report`` in memory; True when a cleaning worker must be started."""
        if report.cancelled:
            return False
        report.cancelled = True
        report.phase = PHASE_CANCELLED
        if report.worker_alive:
            return False
        if report.landed is None and report.prep is not None:
            report.worker_alive = True
            return True
        self._reports.pop(report.report_id, None)
        self._idle_cond.notify_all()
        return False

    def _sweep_locked(self) -> list[_Report]:
        now = self._clock()
        for report_id in [key for key, (expires, _view) in self._ended.items() if expires <= now]:
            del self._ended[report_id]
        to_clean: list[_Report] = []
        for report in list(self._reports.values()):
            if not report.cancelled and report.last_seen + self.idle <= now and self._retire_locked(report):
                to_clean.append(report)
        return to_clean

    def _clean(self, reports: list[_Report]) -> None:
        for report in reports:
            try:
                self._spawn(report, lambda: None, "clean")
            except RuntimeError:
                logger.warning("bug report {}: no thread to clean up; the stale-staging sweep will", report.report_id)

    def _get_locked(self, report_id: str) -> _Report:
        report = self._reports.get(report_id)
        if report is None:
            ended = self._ended.get(report_id)
            if ended is not None:
                raise ReportStateError(ended[1]["phase"], "wrong_phase", "this report has ended")
            raise ReportNotFoundError(report_id)
        report.last_seen = self._clock()
        return report

    # -- worker steps -----------------------------------------------------

    def _cancelled(self, report: _Report) -> bool:
        with self._lock:
            return report.cancelled

    def _fail(self, report: _Report, exc: BaseException) -> None:
        with self._lock:
            if not report.cancelled:
                report.phase = PHASE_FAILED
                report.error = _error(f"{type(exc).__name__}: {exc}")

    def _crashed(self, report: _Report, exc: BaseException) -> None:
        """End a report whose step raised what it did not expect, unless it was cancelled meanwhile."""
        if self._cancelled(report):
            return
        with self._lock:
            phase = report.phase
        if phase == PHASE_UPLOADING:
            self._note_landed(report)
            self._upload_failed(report, _error(f"{type(exc).__name__}: {exc}"))
        elif phase in (PHASE_COLLECTING, PHASE_FINISHING):
            self._fail(report, exc)

    def _note_landed(self, report: _Report) -> None:
        """Remember a record that landed before its step failed, so a retry starts from it."""
        with self._lock:
            prep, landed = report.prep, report.landed
        if landed is not None or prep is None:
            return
        record_dir = breport.bugreports_root(prep.state_dir) / prep.report_id
        try:
            landed_now = record_dir.is_dir()
        except OSError:
            return
        if landed_now:
            with self._lock:
                report.landed = record_dir

    def _collect(self, report: _Report) -> None:
        try:
            prep = breport.prepare_selection(
                report.plan,
                workspace=report.workspace if report.workspace is not None else self._workspace(),
                session_path=report.session_path,
                config_path=self._config_path(),
                state_dir=self._state_dir(),
            )
            with self._lock:
                report.prep = prep
                if report.cancelled:
                    return
            stage = breport.stage_review(prep, **report.problem)
            if self._cancelled(report):
                return
            resolution, state, warning = self._resolve(stage.items)
        except Exception as exc:  # noqa: BLE001 - becomes the report's error, read by the page
            logger.opt(exception=True).warning("bug report {}: collecting failed", report.report_id)
            self._fail(report, exc)
            return
        remaining = list(resolution.remaining) if resolution is not None else list(stage.items)
        confirmed_first = sorted(remaining, key=lambda item: item.kind != treview.KIND_CONFIRMED)
        with self._lock:
            report.stage, report.resolution = stage, resolution
            report.history_state, report.history_warning = state, warning
            report.remaining = confirmed_first
            if report.cancelled:
                return
            if confirmed_first:
                report.phase = PHASE_DECIDING
                return
            report.decisions = list(resolution.auto) if resolution is not None else []
            report.phase = PHASE_FINISHING
        self._finish(report)

    def _resolve(self, items: list[treview.ReviewItem]) -> tuple[rhistory.HistoryResolution | None, str, str | None]:
        if not self._history_enabled():
            return None, HISTORY_OFF, None
        try:
            resolution = self._history().resolve(items)
        except rhistory.HistoryStorageError as exc:
            return None, HISTORY_UNAVAILABLE, f"the redaction history is unavailable ({exc})"
        if resolution.corrupt:
            return resolution, HISTORY_CORRUPT, f"the redaction history could not be read ({resolution.corrupt})"
        return resolution, HISTORY_ON, None

    def _finish(self, report: _Report) -> None:
        with self._lock:
            if report.cancelled:
                return
            stage, prep, decisions = report.stage, report.prep, list(report.decisions)
            resolution, state = report.resolution, report.history_state
        if stage is None or prep is None:
            self._fail(report, RuntimeError("the report has no prepared snapshot"))
            return
        if state in (HISTORY_ON, HISTORY_CORRUPT):
            try:
                result = self._history().remember(
                    stage.items,
                    decisions,
                    source="web",
                    replayed=resolution.replayed if resolution is not None else None,
                )
                warning = (
                    f"{result.stale} remembered decision(s) changed elsewhere during this report"
                    if result.stale
                    else None
                )
            except rhistory.HistoryStorageError as exc:
                warning = f"your decisions were not remembered ({exc})"
            if warning:
                with self._lock:
                    report.history_warning = warning
            if self._cancelled(report):
                return
        try:
            breport.finish_export(prep, stage, decisions)
        except Exception as exc:  # noqa: BLE001 - becomes the report's error, read by the page
            logger.opt(exception=True).warning("bug report {}: freezing failed", report.report_id)
            self._fail(report, exc)
            return
        with self._lock:
            if not report.cancelled:
                report.phase = PHASE_READY

    def _deliver(self, report: _Report) -> None:
        with self._lock:
            if report.cancelled:
                return
            prep, landed = report.prep, report.landed
        try:
            if landed is None:
                if prep is None:
                    raise breport.BugReportError("the report has no prepared snapshot")
                record_dir, record = breport.confirm_and_package(prep, state_dir=prep.state_dir)
                with self._lock:
                    report.landed = record_dir
            else:
                record_dir = landed
                record = breport.load_record(record_dir)
                if record["status"] == breport.STATUS_FAILED:
                    record = breport.retry_packaging(record_dir)
        except breport.PackagingError as exc:
            # The record landed before packaging failed; a retry repackages it
            # from there instead of landing it again.
            self._note_landed(report)
            self._upload_failed(report, _error(str(exc), retryable=exc.retryable))
            return
        except (breport.StaleAttemptError, breport.BugReportError, OSError, ValueError) as exc:
            self._upload_failed(report, _error(f"{type(exc).__name__}: {exc}"))
            return
        if self._cancelled(report):
            return
        size = int(record["package"]["size_bytes"])
        with self._lock:
            report.size_bytes = size
        limit = PackageLimits().max_package_bytes
        if size > limit:
            self._upload_failed(
                report,
                _error(
                    f"the package is {size} bytes, over the receiver's {limit}-byte limit; "
                    f"it is kept at {record['package']['path']}"
                ),
            )
            return
        try:
            _record, receipt = upload_record(record_dir, base_url=self._upload_url())
        except UploadError as exc:
            self._upload_failed(report, _error(exc.reason, retryable=exc.retryable, status=exc.status))
            return
        except UploadConfigError as exc:
            self._upload_failed(report, _error(str(exc)))
            return
        except OSError as exc:
            # Reading the package or writing the receipt into the record; the
            # package stays, and a retry that finds it already delivered is
            # answered as a duplicate.
            self._upload_failed(report, _error(f"{type(exc).__name__}: {exc}", retryable=True))
            return
        except ValueError as exc:
            self._upload_failed(report, _error(f"{type(exc).__name__}: {exc}"))
            return
        with self._lock:
            if not report.cancelled:
                report.phase = PHASE_UPLOADED
                report.error = None
                report.receipt = {"receipt_id": receipt.receipt_id, "duplicate": receipt.duplicate}

    def _upload_failed(self, report: _Report, error: dict[str, Any]) -> None:
        with self._lock:
            if not report.cancelled:
                report.phase = PHASE_UPLOAD_FAILED
                report.error = error

    # -- the surface ------------------------------------------------------

    def start(
        self,
        plan: SelectionPlan,
        problem: dict[str, str],
        *,
        workspace: Path | None = None,
        session_path: Path | None = None,
    ) -> str:
        if not isinstance(plan, SelectionPlan):
            raise TypeError("start takes the SelectionPlan that plan_selection returned")
        with self._lock:
            to_clean = self._sweep_locked()
            report = None
            if self._occupied_locked() < self.limit:
                report = _Report(
                    report_id=f"rs-{secrets.token_hex(8)}",
                    plan=plan,
                    problem=dict(problem),
                    last_seen=self._clock(),
                    workspace=workspace,
                    session_path=session_path,
                    worker_alive=True,
                )
                self._reports[report.report_id] = report
        self._clean(to_clean)
        if report is None:
            raise ReportLimitError(self.limit)
        self._spawn(report, lambda: self._collect(report), "collect", lambda: self._reports.pop(report.report_id, None))
        return report.report_id

    def status(self, report_id: str) -> dict[str, Any]:
        with self._lock:
            to_clean = self._sweep_locked()
            report = self._reports.get(report_id)
            view = None
            if report is not None:
                report.last_seen = self._clock()
                view = self._view_locked(report)
            elif report_id in self._ended:
                view = copy.deepcopy(self._ended[report_id][1])
        self._clean(to_clean)
        if view is None:
            raise ReportNotFoundError(report_id)
        return view

    def _view_locked(self, report: _Report) -> dict[str, Any]:
        prep, resolution = report.prep, report.resolution
        auto = resolution.auto if resolution is not None else []
        items = None
        if report.phase == PHASE_DECIDING:
            hints = resolution.hints if resolution is not None else {}
            items = [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "category": item.category,
                    # A confirmed item keeps no value of its own -- every
                    # private key is replaced whatever the reader says -- so
                    # what it shows is the replacement.
                    "value": item.token or item.masked_sample,
                    "masked_sample": item.masked_sample,
                    "count": len(item.occurrences),
                    "sources": [{"label": s.get("label", ""), "count": s.get("count", 0)} for s in item.sources],
                    "linked": list(item.linked),
                    "hint": hints[item.id].action if item.id in hints else None,
                }
                for item in report.remaining
            ]
        summary = None
        if prep is not None and report.phase in (PHASE_READY, PHASE_UPLOADING, PHASE_UPLOADED, PHASE_UPLOAD_FAILED):
            reports = [prep.trajectory_report] + ([prep.problem_report] if prep.problem_report is not None else [])
            summary = {
                "classification": prep.classification,
                "warnings": treview.review_warnings(prep.reasons),
                "size_bytes": report.size_bytes,
                "auto_replaced": sum(r.total_replacements for r in reports),
                **treview.decision_counts(prep.user_decisions),
                "completeness": prep.completeness[0],
            }
        return {
            "report_id": report.report_id,
            "bug_report_id": prep.report_id if prep is not None else None,
            "phase": report.phase,
            "error": dict(report.error) if report.error else None,
            "history": {
                "state": report.history_state,
                "auto_kept": sum(1 for d in auto if d.action == treview.ACTION_KEPT),
                "auto_redacted": sum(1 for d in auto if d.action == treview.ACTION_REDACTED),
                "warning": report.history_warning,
            },
            "items": items,
            "summary": summary,
            "receipt": dict(report.receipt) if report.receipt else None,
            "record_dir": str(report.landed) if report.landed is not None else None,
        }

    def item(self, report_id: str, item_id: str) -> dict[str, Any]:
        with self._lock:
            report = self._get_locked(report_id)
            if report.phase != PHASE_DECIDING:
                raise ReportStateError(report.phase, "wrong_phase", "contexts are shown only while deciding")
            item = next((i for i in report.remaining if i.id == item_id), None)
            if item is None:
                raise ReportStateError(report.phase, "unknown_item", f"no review item {item_id!r}", [item_id])
            groups, total = treview.grouped_occurrences(item, limit=ITEM_GROUP_LIMIT)
        return {
            "groups": [
                {
                    "label": g["label"],
                    "text": g["text"],
                    "spans": [list(s) for s in g["spans"]],
                    "count": len(g["occurrences"]),
                }
                for g in groups
            ],
            "total_groups": total,
        }

    def decide(self, report_id: str, decisions: list[tuple[str, str]], acknowledged: list[str]) -> None:
        with self._lock:
            report = self._get_locked(report_id)
            if report.phase != PHASE_DECIDING or report.worker_alive:
                raise ReportStateError(report.phase, "wrong_phase", "this report is not waiting for decisions")
            stage, resolution = report.stage, report.resolution
            if stage is None:
                raise ReportStateError(report.phase, "wrong_phase", "this report has nothing to decide")
            remaining = {item.id: item for item in report.remaining}
            chosen = dict(decisions)
            unknown = [i for i in list(chosen) + list(acknowledged) if i not in remaining]
            if unknown:
                raise ReportStateError(report.phase, "unknown_item", "decisions name unknown items", unknown)
            confirmed = [i for i, item in remaining.items() if item.kind == treview.KIND_CONFIRMED]
            missing_ack = [i for i in confirmed if i not in set(acknowledged)]
            if missing_ack:
                raise ReportStateError(
                    report.phase, "not_acknowledged", "every confirmed item must be acknowledged", missing_ack
                )
            suspected = [i for i, item in remaining.items() if item.kind == treview.KIND_SUSPECTED]
            undecided = [i for i in suspected if i not in chosen]
            if undecided:
                raise ReportStateError(report.phase, "undecided", "every suspected item needs a decision", undecided)
            full = list(resolution.auto) if resolution is not None else []
            full += [treview.ReviewDecision(i, chosen[i]) for i in suspected]
            full += [treview.ReviewDecision(i, treview.ACTION_ACKNOWLEDGED) for i in confirmed]
            try:
                treview.validate_review_decisions(stage.items, full)
            except treview.ReviewConflictError:
                action = {d.item_id: d.action for d in full}
                clash = sorted(
                    {
                        side
                        for item in stage.items
                        for other in item.linked
                        if action.get(item.id) != action.get(other)
                        for side in (item.id, other)
                    }
                )
                raise ReportConflictError(clash) from None
            except breport.BugReportError as exc:
                raise ReportStateError(report.phase, "invalid", str(exc)) from None
            report.decisions = full
            report.phase = PHASE_FINISHING
            report.worker_alive = True

        def undo() -> None:
            if not report.cancelled:
                report.phase = PHASE_DECIDING
                report.decisions = []

        self._spawn(report, lambda: self._finish(report), "finish", undo)

    def upload(self, report_id: str, *, risk_accepted: bool) -> None:
        with self._lock:
            report = self._get_locked(report_id)
            if report.phase != PHASE_READY or report.worker_alive:
                raise ReportStateError(report.phase, "wrong_phase", "this report is not ready to upload")
            prep = report.prep
            if prep is not None and prep.classification == breport.CLASSIFICATION_NEEDS_REVIEW and not risk_accepted:
                raise ReportStateError(report.phase, "risk_not_accepted", "this report needs the risk accepted")
            report.phase = PHASE_UPLOADING
            report.error = None
            report.worker_alive = True

        def undo() -> None:
            if not report.cancelled:
                report.phase = PHASE_READY

        self._spawn(report, lambda: self._deliver(report), "upload", undo)

    def retry_upload(self, report_id: str) -> None:
        with self._lock:
            report = self._get_locked(report_id)
            if report.phase != PHASE_UPLOAD_FAILED or report.worker_alive or not (report.error or {}).get("retryable"):
                raise ReportStateError(report.phase, "not_retryable", "this report cannot be retried")
            failure = report.error
            report.phase = PHASE_UPLOADING
            report.error = None
            report.worker_alive = True

        def undo() -> None:
            if not report.cancelled:
                report.phase = PHASE_UPLOAD_FAILED
                report.error = failure

        self._spawn(report, lambda: self._deliver(report), "upload", undo)

    def cancel(self, report_id: str) -> None:
        """Stop a report; for one that already failed or uploaded this only forgets its result."""
        with self._lock:
            to_clean = self._sweep_locked()
            report = self._reports.get(report_id)
            ended = self._ended.pop(report_id, None)
            if report is not None and self._retire_locked(report):
                to_clean.append(report)
        self._clean(to_clean)
        if report is None and ended is None:
            raise ReportNotFoundError(report_id)

    def shutdown(self, *, timeout: float = 10.0) -> bool:
        """Cancel every report and wait until their workers have cleaned up; blocks, so run it off the loop."""
        with self._lock:
            self._ended.clear()
            to_clean = [report for report in list(self._reports.values()) if self._retire_locked(report)]
        self._clean(to_clean)
        with self._lock:
            return self._idle_cond.wait_for(lambda: not self._reports, timeout)


REPORTS = ReportSessions()
"""The process-wide registry the ``bugreport.*`` handlers share."""


__all__ = [
    "REPORTS",
    "ReportConflictError",
    "ReportLimitError",
    "ReportNotFoundError",
    "ReportSessions",
    "ReportStateError",
]
