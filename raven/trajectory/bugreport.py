"""Bug report records and packaging — the tester-facing issue-filing pipeline.

A **Bug Report Record** (``record.json``) is the machine-local lifecycle
record of one filed problem: never exported, allowed to hold absolute paths.
A **Bug Report Package** (``<report-id>.tar.gz``) is the only artifact allowed
to leave the machine: a canonical ``bugreport.json`` (problem metadata, merged
redaction summary, environment, content manifest) plus an embedded Trajectory
Report in its existing format. Everything the package will contain is frozen
under ``snapshot/export/`` **before** the confirmation screen is shown, so the
text the user approves, the first packaging run, and any retry after a failure
all use the same bytes — a retry never re-collects, re-redacts, or re-derives
metadata (config changes after confirmation cannot alter the product).

Directory layout under the tracing state dir::

    bugreports/
      .staging/<report-id>/       # pre-confirmation; deleted on cancel/block
        snapshot/
          bundle/<attempt-id>/    # collect_bundle output (source identity)
          redacted/<attempt-id>/  # redact_bundle output, path-sanitized
          problem/, problem_redacted/   # user fields through the same redaction
          export/                 # frozen deliverable: bugreport.json +
                                  #   trajectory/<attempt-id>.tar.gz
      <report-id>/                # renamed whole from .staging on confirm
        record.json
        snapshot/                 # kept while failed; removed on local_ready
        <report-id>.tar.gz

Record status is ``draft`` -> ``local_ready`` | ``failed`` (retryable only
while the frozen snapshot verifies). A persisted ``draft`` can only be an
interrupted process — the normal flow reaches a terminal state in the same
call — so readers atomically convert it to ``failed`` (crash recovery).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets as _secrets
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from raven import __version__
from raven.tracing import config as tracing_config
from raven.trajectory.bundle import BUNDLE_FORMAT_VERSION, collect_bundle
from raven.trajectory.redact import KnownSecret, RedactionReport, collect_known_secrets, redact_bundle
from raven.trajectory.report import pack_report
from raven.trajectory.sanitize import sanitize_export_tree, sanitize_text, scan_absolute_paths, tree_digest
from raven.trajectory.store import member_traces

_log = logging.getLogger("raven.trajectory.bugreport")

BUGREPORTS_DIR = "bugreports"
STAGING_DIR = ".staging"
RECORD_FILE = "record.json"
PACKAGE_METADATA_FILE = "bugreport.json"
RECORD_SCHEMA = "bug_report_record"
PACKAGE_SCHEMA = "bug_report_package"
SCHEMA_VERSION = 1

CLASSIFICATION_CLEAN = "clean"
CLASSIFICATION_NEEDS_REVIEW = "needs_review"
CLASSIFICATION_BLOCKED = "blocked"

STATUS_DRAFT = "draft"
STATUS_LOCAL_READY = "local_ready"
STATUS_FAILED = "failed"

PROBLEM_FIELDS = ("description", "expected", "actual", "severity", "steps")
SEVERITIES = ("low", "medium", "high", "critical")

_STALE_STAGING_SECONDS = 24 * 3600
_ID_COLLISION_RETRIES = 16
_REPORT_ID = re.compile(r"^br-\d{8}-[0-9a-f]{6}$")

REASON_INTERRUPTED = "interrupted before the package was written"
REASON_INTERRUPTED_INCOMPLETE = "interrupted and the snapshot is incomplete; delete the report and file a new one"
REASON_SNAPSHOT_CORRUPTED = "snapshot corrupted; delete the report and file a new one"


class BugReportError(Exception):
    """Base for bug-report pipeline failures."""


class StaleAttemptError(BugReportError):
    """The attempt's member set changed while the report was being prepared."""


class ExportLeakError(BugReportError):
    """The export tree still carries content that must not leave the machine."""


class PackagingError(BugReportError):
    """Packaging failed after the record was created (record is now failed)."""


def bugreports_root(state_dir: Path | None = None) -> Path:
    return (state_dir or tracing_config.state_dir()) / BUGREPORTS_DIR


def new_report_id(root: Path) -> str:
    """A fresh ``br-<UTC date>-<6 hex>`` id whose directories don't exist yet."""
    for _ in range(_ID_COLLISION_RETRIES):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        report_id = f"br-{stamp}-{_secrets.token_hex(3)}"
        if not (root / report_id).exists() and not (root / STAGING_DIR / report_id).exists():
            return report_id
    raise BugReportError("could not allocate a unique report id")


def classify_redaction(*reports: RedactionReport) -> tuple[str, list[str]]:
    """The single classification rule, over the merged signals of ``reports``.

    ``blocked`` keys off the **original-content** private-key hit
    (``patterns``), not the residual scan — a trajectory that ever held a full
    private key is not fit to leave the machine even after replacement.
    """
    key_hits = sum(r.patterns.get("private-key-block", 0) for r in reports)
    if key_hits:
        return CLASSIFICATION_BLOCKED, ["the original trajectory contains a private key block"]
    reasons: list[str] = []
    finding_count = sum(len(r.findings) for r in reports)
    if finding_count:
        reasons.append(f"residual scan flagged {finding_count} suspicious token(s)")
    if not all(r.config_loaded for r in reports):
        reasons.append("config could not be fully read — known-value redaction may be incomplete")
    skipped = sum(len(r.skipped_binaries) for r in reports)
    if skipped:
        reasons.append(f"{skipped} non-UTF-8 file(s) excluded from the copy and not scanned")
    if reasons:
        return CLASSIFICATION_NEEDS_REVIEW, reasons
    return CLASSIFICATION_CLEAN, []


def _merged_redaction_metadata(reports: list[RedactionReport], roots: list[str]) -> dict[str, Any]:
    """The package's merged redaction view, keys as ``RedactionReport.metadata()``.

    Counts add up per key, findings concatenate (samples sanitized — they were
    captured from pre-sanitization text), ``config_secrets_loaded`` is the
    conjunction.
    """
    exact: dict[str, int] = {}
    patterns: dict[str, int] = {}
    findings: list[dict[str, Any]] = []
    skipped: list[str] = []
    for report in reports:
        for key, count in report.exact.items():
            exact[key] = exact.get(key, 0) + count
        for key, count in report.patterns.items():
            patterns[key] = patterns.get(key, 0) + count
        for finding in report.findings:
            findings.append(
                {
                    "category": finding.category,
                    "sample": sanitize_text(finding.sample, roots),
                    "file": finding.file,
                    "count": finding.count,
                }
            )
        skipped.extend(report.skipped_binaries)
    return {
        "exact_replacements": exact,
        "pattern_replacements": patterns,
        "residual_findings": findings,
        "skipped_binaries": skipped,
        "config_secrets_loaded": all(r.config_loaded for r in reports),
    }


def _sanitize_json_strings(value: Any, roots: list[str]) -> Any:
    """Every string in a JSON-shaped value through :func:`sanitize_text`."""
    if isinstance(value, str):
        return sanitize_text(value, roots)
    if isinstance(value, list):
        return [_sanitize_json_strings(item, roots) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_json_strings(item, roots) for key, item in value.items()}
    return value


def _local_roots(workspace: Path | None, config_path: Path | None, state_dir: Path) -> list[str]:
    """Known machine-local root prefixes the export must never contain."""
    roots = [str(state_dir.resolve()), str(Path.home())]
    if workspace is not None:
        roots.append(str(Path(workspace).resolve()))
    if config_path is not None:
        roots.append(str(Path(config_path).expanduser().resolve().parent))
    try:
        from raven.config.loader import get_config_path

        default_config = get_config_path()
        if default_config is not None:
            roots.append(str(Path(default_config).expanduser().resolve().parent))
    except Exception:
        pass
    return roots


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_record(record_dir: Path) -> dict[str, Any]:
    """The validated record payload of one report directory.

    Raises ``ValueError`` for a missing/corrupt file, a foreign schema, or an
    unknown major version (never guess at fields a future writer meant).
    """
    path = record_dir / RECORD_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable bug report record: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != RECORD_SCHEMA:
        raise ValueError(f"not a bug report record: {path}")
    version = payload.get("schema_version")
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise ValueError(f"unsupported bug report record version {version!r}: {path}")
    return payload


def save_record(record_dir: Path, payload: dict[str, Any]) -> None:
    _atomic_write_json(record_dir / RECORD_FILE, payload)


@dataclass
class ExportPreparation:
    """Everything frozen between entering ``Report a bug`` and packaging."""

    report_id: str
    state_dir: Path
    staging_dir: Path
    attempt_id: str
    session_key: str | None
    member_traces: tuple[str, ...]
    merged_definition: bool
    manifest: dict[str, Any]
    secrets: list[KnownSecret]
    config_loaded: bool
    roots: list[str]
    trajectory_report: RedactionReport
    classification: str
    reasons: list[str] = field(default_factory=list)
    problem: dict[str, str] = field(default_factory=dict)
    reporter: str = ""
    problem_report: RedactionReport | None = None
    package_metadata: dict[str, Any] | None = None
    source_digest: str = ""
    export_digest: str = ""

    @property
    def snapshot_dir(self) -> Path:
        return self.staging_dir / "snapshot"

    @property
    def export_dir(self) -> Path:
        return self.snapshot_dir / "export"

    def cleanup(self) -> None:
        shutil.rmtree(self.staging_dir, ignore_errors=True)


def prepare_trajectory(
    id_: str,
    *,
    expected_traces: tuple[str, ...] | None = None,
    workspace: Path | None = None,
    config_path: Path | None = None,
    state_dir: Path | None = None,
    on_collected: Callable[[], None] | None = None,
) -> ExportPreparation:
    """Collect and redact the trajectory snapshot (everything before user input).

    ``expected_traces`` is the member set the user saw when picking the row;
    a snapshot resolving to a different set means the attempt changed under
    them (``StaleAttemptError``). The returned preparation carries the
    trajectory-only classification — ``blocked`` here means the flow must stop
    before asking for a description.
    """
    resolved_state = (state_dir or tracing_config.state_dir()).resolve()
    root = bugreports_root(resolved_state)
    staging_root = root / STAGING_DIR
    staging_root.mkdir(parents=True, exist_ok=True)
    report_id = new_report_id(root)
    staging_dir = staging_root / report_id
    snapshot_dir = staging_dir / "snapshot"
    snapshot_dir.mkdir(parents=True)

    try:
        bundle_dir = collect_bundle(id_, out_dir=snapshot_dir / "bundle", state_dir=resolved_state, workspace=workspace)
        attempt_id = bundle_dir.name
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))

        bundled_traces = _bundle_traces(bundle_dir)
        if expected_traces is not None and set(expected_traces) != set(bundled_traces):
            raise StaleAttemptError("the attempt changed while the report was being prepared")
        if on_collected is not None:
            on_collected()

        secrets, config_loaded = collect_known_secrets(config_path)
        report = redact_bundle(bundle_dir, snapshot_dir / "redacted" / attempt_id, secrets=secrets)
        report.config_loaded = config_loaded
        classification, reasons = classify_redaction(report)

        return ExportPreparation(
            report_id=report_id,
            state_dir=resolved_state,
            staging_dir=staging_dir,
            attempt_id=attempt_id,
            session_key=manifest.get("session_key"),
            member_traces=bundled_traces,
            merged_definition=attempt_id in _definition_ids(resolved_state),
            manifest=manifest,
            secrets=secrets,
            config_loaded=config_loaded,
            roots=_local_roots(workspace, config_path, resolved_state),
            trajectory_report=report,
            classification=classification,
            reasons=reasons,
        )
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _definition_ids(state_dir: Path) -> set[str]:
    from raven.trajectory.store import definitions

    return set(definitions(state_dir))


def _bundle_traces(bundle_dir: Path) -> tuple[str, ...]:
    traces: set[str] = set()
    for line in (bundle_dir / "spans.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            span = json.loads(line)
        except json.JSONDecodeError:
            continue
        trace = span.get("traceId") if isinstance(span, dict) else None
        if isinstance(trace, str) and trace:
            traces.add(trace)
    return tuple(sorted(traces))


def freeze_export(
    prep: ExportPreparation,
    *,
    description: str,
    expected: str = "",
    actual: str = "",
    severity: str = "",
    steps: str = "",
    reporter: str = "",
) -> ExportPreparation:
    """Freeze the complete deliverable under ``snapshot/export/``.

    The user fields go through the same redaction as the trajectory (written
    as ``problem.<field>`` files and redacted with the same known secrets, so
    residual findings carry that name), then path sanitization; the merged
    classification decides the flow. On anything but ``blocked`` the embedded
    tarball and the canonical ``bugreport.json`` are produced, asserted clean,
    and digested — the confirmation screen and every later packaging run use
    exactly these bytes.
    """
    if not description:
        raise ValueError("a problem description is required")
    raw_fields = {
        "description": description,
        "expected": expected,
        "actual": actual,
        "severity": severity,
        "steps": steps,
        "reporter": reporter,
    }

    problem_dir = prep.snapshot_dir / "problem"
    problem_dir.mkdir(exist_ok=True)
    for name, value in raw_fields.items():
        if value:
            (problem_dir / f"problem.{name}").write_text(value, encoding="utf-8")
    problem_report = redact_bundle(problem_dir, prep.snapshot_dir / "problem_redacted", secrets=prep.secrets)
    problem_report.config_loaded = prep.config_loaded
    prep.problem_report = problem_report

    redacted_fields: dict[str, str] = {}
    for name, value in raw_fields.items():
        source = prep.snapshot_dir / "problem_redacted" / f"problem.{name}"
        text = source.read_text(encoding="utf-8") if value and source.is_file() else ""
        redacted_fields[name] = sanitize_text(text, prep.roots)

    classification, reasons = classify_redaction(prep.trajectory_report, problem_report)
    prep.classification = classification
    prep.reasons = reasons
    prep.reporter = redacted_fields.pop("reporter")
    prep.problem = redacted_fields
    if classification == CLASSIFICATION_BLOCKED:
        return prep

    redacted_dir = prep.snapshot_dir / "redacted" / prep.attempt_id
    sanitize_export_tree(redacted_dir, prep.roots)
    leaks = scan_absolute_paths(redacted_dir, prep.roots)
    if leaks:
        raise ExportLeakError(f"absolute path leaked into the export: {leaks[0][0]}")

    prep.source_digest = tree_digest(prep.snapshot_dir / "bundle" / prep.attempt_id)

    export_dir = prep.export_dir
    (export_dir / "trajectory").mkdir(parents=True)
    tarball = pack_report(redacted_dir, export_dir / "trajectory" / f"{prep.attempt_id}.tar.gz")

    metadata = _build_package_metadata(prep, tarball)
    _atomic_write_json(export_dir / PACKAGE_METADATA_FILE, metadata)
    prep.package_metadata = metadata

    _assert_export_ready(prep, metadata)
    prep.export_digest = tree_digest(export_dir)
    return prep


def _build_package_metadata(prep: ExportPreparation, tarball: Path) -> dict[str, Any]:
    merged = _merged_redaction_metadata(
        [prep.trajectory_report, prep.problem_report] if prep.problem_report else [prep.trajectory_report],
        prep.roots,
    )
    metadata: dict[str, Any] = {
        "schema": PACKAGE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "report_id": prep.report_id,
        "created_at": _now_iso(),
        "raven_version": __version__,
        "reporter": prep.reporter,
        "attempt": {
            "attempt_id": prep.attempt_id,
            "member_traces": list(prep.member_traces),
            "merged_definition": prep.merged_definition,
        },
        "problem": dict(prep.problem),
        "completeness": {"status": "unknown", "reasons": ["not evaluated in this version"]},
        "redaction": {
            "classification": prep.classification,
            "reasons": list(prep.reasons),
            "risk_accepted": prep.classification == CLASSIFICATION_NEEDS_REVIEW,
            **merged,
        },
        "environment": {
            "raven_version": __version__,
            "bundle_format_version": BUNDLE_FORMAT_VERSION,
        },
        "contents": [
            {"path": PACKAGE_METADATA_FILE, "sha256": "", "size_bytes": 0},
            {
                "path": f"trajectory/{tarball.name}",
                "sha256": _file_sha256(tarball),
                "size_bytes": tarball.stat().st_size,
            },
        ],
    }
    return _sanitize_json_strings(metadata, prep.roots)


def _assert_export_ready(prep: ExportPreparation, metadata: dict[str, Any]) -> None:
    """The final gate before anything may be shown as shippable.

    Manifest, type, and metadata assertions over ``export/``: the files on
    disk match ``contents`` exactly, all are regular files, and the metadata
    text carries no absolute path.
    """
    export_dir = prep.export_dir
    expected = {entry["path"] for entry in metadata["contents"]}
    actual: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(export_dir):
        base = Path(dirpath)
        for name in dirnames + filenames:
            path = base / name
            if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                raise ExportLeakError(f"absolute path leaked into the export: unsupported entry {name}")
            if path.is_file():
                actual.add(str(path.relative_to(export_dir)))
    if actual != expected:
        raise ExportLeakError(
            f"export manifest mismatch: extra={sorted(actual - expected)} missing={sorted(expected - actual)}"
        )
    metadata_text = (export_dir / PACKAGE_METADATA_FILE).read_text(encoding="utf-8")
    hits = _metadata_leaks(metadata_text, prep.roots)
    if hits:
        raise ExportLeakError(f"absolute path leaked into the export: {PACKAGE_METADATA_FILE}: {hits[0]}")


def _metadata_leaks(text: str, roots: list[str]) -> list[str]:
    from raven.trajectory.sanitize import find_absolute_paths

    hits = [text[start:end][:120] for start, end in find_absolute_paths(text)]
    for root in roots:
        needle = root.rstrip("/\\")
        if needle and needle in text:
            hits.append(needle)
    return hits


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_record_payload(prep: ExportPreparation) -> dict[str, Any]:
    return {
        "schema": RECORD_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "report_id": prep.report_id,
        "created_at": _now_iso(),
        "raven_version": __version__,
        "reporter": prep.reporter,
        "attempt": {
            "attempt_id": prep.attempt_id,
            "session_key": prep.session_key,
            "member_traces": list(prep.member_traces),
            "merged_definition": prep.merged_definition,
        },
        "problem": dict(prep.problem),
        "status": STATUS_DRAFT,
        "failure": {"reason": "", "retryable": False, "retry_count": 0},
        "snapshot": {"source_digest": prep.source_digest, "export_digest": prep.export_digest, "kept": True},
        "package": {"path": "", "sha256": "", "size_bytes": 0},
        "completeness": {"status": "unknown", "reasons": ["not evaluated in this version"]},
        "redaction": {
            "classification": prep.classification,
            "reasons": list(prep.reasons),
            "reviewed_by_user": True,
        },
        "upload": {"state": "", "issue_url": "", "receipt": ""},
        "links": {"issue": "", "pr": "", "regression_case": ""},
    }


def confirm_and_package(prep: ExportPreparation, *, state_dir: Path | None = None) -> tuple[Path, dict[str, Any]]:
    """Land the record and produce the package (the user has confirmed).

    Re-checks the member set right before landing (a concurrent merge/split
    between the confirmation screen and this call must not bind the report to
    the wrong object), writes the ``draft`` record into staging, renames the
    staging directory whole into place, then packages. Packaging failure
    leaves a retryable ``failed`` record with the snapshot kept, raised as
    :class:`PackagingError`.
    """
    resolved_state = (state_dir or prep.state_dir).resolve()
    current = member_traces(prep.attempt_id, resolved_state)
    if current is None or set(current) != set(prep.member_traces):
        raise StaleAttemptError("the attempt changed while the report was being prepared")

    save_record(prep.staging_dir, _new_record_payload(prep))
    record_dir = bugreports_root(resolved_state) / prep.report_id
    os.replace(prep.staging_dir, record_dir)
    record = load_record(record_dir)
    return record_dir, _package(record_dir, record)


def _package(record_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    """``tar(export/)`` into the record directory; the only step a retry redoes."""
    report_id = record["report_id"]
    export_dir = record_dir / "snapshot" / "export"
    final = record_dir / f"{report_id}.tar.gz"
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{report_id}-", suffix=".tar.gz", dir=record_dir)
        os.close(fd)
        try:
            with tarfile.open(tmp, "w:gz") as tar:
                tar.add(export_dir, arcname=report_id)
            os.replace(tmp, final)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        record["status"] = STATUS_LOCAL_READY
        record["failure"]["reason"] = ""
        record["failure"]["retryable"] = False
        record["package"] = {
            "path": str(final),
            "sha256": _file_sha256(final),
            "size_bytes": final.stat().st_size,
        }
        record["snapshot"]["kept"] = False
        save_record(record_dir, record)
        shutil.rmtree(record_dir / "snapshot", ignore_errors=True)
        return record
    except BaseException as exc:
        record["status"] = STATUS_FAILED
        record["failure"]["reason"] = str(exc) or exc.__class__.__name__
        record["failure"]["retryable"] = True
        record["snapshot"]["kept"] = True
        save_record(record_dir, record)
        raise PackagingError(record["failure"]["reason"]) from exc


def retry_packaging(record_dir: Path) -> dict[str, Any]:
    """Re-run packaging from the frozen snapshot; never re-collect.

    Verifies ``export_digest`` first: a modified, added/removed, or
    link-injected entry means the frozen deliverable is gone — the report
    becomes permanently non-retryable rather than shipping unapproved bytes.
    """
    record = load_record(record_dir)
    if record["status"] != STATUS_FAILED or not record["failure"].get("retryable"):
        raise BugReportError(f"report {record['report_id']} is not retryable")
    record["failure"]["retry_count"] = int(record["failure"].get("retry_count", 0)) + 1
    export_dir = record_dir / "snapshot" / "export"
    try:
        digest = tree_digest(export_dir)
    except ValueError:
        digest = None
    if digest is None or digest != record["snapshot"].get("export_digest"):
        record["status"] = STATUS_FAILED
        record["failure"]["reason"] = REASON_SNAPSHOT_CORRUPTED
        record["failure"]["retryable"] = False
        save_record(record_dir, record)
        raise PackagingError(REASON_SNAPSHOT_CORRUPTED)
    record["status"] = STATUS_DRAFT
    save_record(record_dir, record)
    return _package(record_dir, record)


def recover_interrupted(record_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Convert a persisted ``draft`` (an interrupted process) to ``failed``.

    The normal flow reaches a terminal state in the same call stack, so a
    ``draft`` found on disk can only mean the process died between landing the
    record and writing the outcome. Retryable while the frozen snapshot still
    verifies; ``retry_count`` is untouched — recovery is not a user retry.
    """
    if record["status"] != STATUS_DRAFT:
        return record
    export_dir = record_dir / "snapshot" / "export"
    try:
        digest = tree_digest(export_dir)
    except ValueError:
        digest = None
    record["status"] = STATUS_FAILED
    if digest is not None and digest == record["snapshot"].get("export_digest"):
        record["failure"]["reason"] = REASON_INTERRUPTED
        record["failure"]["retryable"] = True
    else:
        record["failure"]["reason"] = REASON_INTERRUPTED_INCOMPLETE
        record["failure"]["retryable"] = False
    save_record(record_dir, record)
    return record


def list_reports(state_dir: Path | None = None) -> list[tuple[Path, dict[str, Any]]]:
    """Every readable report, newest first; persisted drafts are recovered."""
    root = bugreports_root(state_dir)
    if not root.is_dir():
        return []
    out: list[tuple[Path, dict[str, Any]]] = []
    for entry in sorted(root.iterdir(), reverse=True):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            record = load_record(entry)
        except ValueError:
            _log.debug("skipping unreadable bug report record", exc_info=True)
            continue
        if record["status"] == STATUS_DRAFT:
            record = recover_interrupted(entry, record)
        out.append((entry, record))
    return out


def reports_for_attempt(
    attempt_id: str, traces: tuple[str, ...], state_dir: Path | None = None
) -> list[tuple[Path, dict[str, Any]]]:
    """Reports whose frozen association matches this attempt.

    Matching is by the recorded attempt id or any member-trace overlap — a
    merge/split after filing must still surface the report on the rows that
    carry its traces.
    """
    matches = []
    wanted = set(traces)
    for record_dir, record in list_reports(state_dir):
        attempt = record.get("attempt") or {}
        recorded = set(attempt.get("member_traces") or [])
        if attempt.get("attempt_id") == attempt_id or (wanted and recorded & wanted):
            matches.append((record_dir, record))
    return matches


def cleanup_stale_staging(state_dir: Path | None = None, *, max_age_seconds: float = _STALE_STAGING_SECONDS) -> int:
    """Remove abandoned staging directories (crashed sessions), count removed."""
    staging_root = bugreports_root(state_dir) / STAGING_DIR
    if not staging_root.is_dir():
        return 0
    removed = 0
    cutoff = time.time() - max_age_seconds
    for entry in staging_root.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


__all__ = [
    "BUGREPORTS_DIR",
    "BugReportError",
    "CLASSIFICATION_BLOCKED",
    "CLASSIFICATION_CLEAN",
    "CLASSIFICATION_NEEDS_REVIEW",
    "ExportLeakError",
    "ExportPreparation",
    "PACKAGE_METADATA_FILE",
    "PACKAGE_SCHEMA",
    "PROBLEM_FIELDS",
    "PackagingError",
    "RECORD_FILE",
    "RECORD_SCHEMA",
    "SCHEMA_VERSION",
    "SEVERITIES",
    "STATUS_DRAFT",
    "STATUS_FAILED",
    "STATUS_LOCAL_READY",
    "StaleAttemptError",
    "bugreports_root",
    "classify_redaction",
    "cleanup_stale_staging",
    "confirm_and_package",
    "freeze_export",
    "list_reports",
    "load_record",
    "new_report_id",
    "prepare_trajectory",
    "recover_interrupted",
    "reports_for_attempt",
    "retry_packaging",
    "save_record",
]
