"""Trajectory layer over the tracing store.

Tracing records *what happened* (``audit.span.v1`` spans + artifacts, see
:mod:`raven.tracing`); this package adds the semantics that turn those records
into trajectories — addressable, labeled, retained units of agent work:

- ``verdict``  — the label sidecar. Task success/failure is not tracing's
  business (``status.code`` answers "did the code crash", not "did the task
  succeed"), so verdicts live in a separate append-only file keyed by
  ``attempt.id``, written by whoever can judge (user command, eval judge).
- ``store``    — pin registry + rotation-transparent span reader. A pinned
  attempt is corpus, not diagnostics: any purge tooling must consult
  :func:`store.pins` before deleting spans or the artifacts they reference.
- ``bundle``   — the bundle collector. Packs one attempt's spans, artifacts,
  session record, and verdicts into a self-contained offline directory (and
  pins the id, since bundling declares the trajectory corpus).
- ``redact``   — the sanitizer. Produces a redacted **copy** of a bundle
  (known secret values, credential patterns, residual scan); the original
  bundle is never modified.
- ``report``   — the shippable form: the redacted copy packed into a
  ``.tar.gz``, delivered through the pluggable :class:`report.Uploader`
  (v1: local file only).

The address unit is the **attempt** (``attempt.id`` on every span): one task
try, possibly spanning several turns. Without an explicitly opened attempt
(:func:`raven.tracing.trace.begin_attempt`) each turn is its own single-turn
attempt whose id equals the trace id — so every trace is addressable as an
attempt with zero ceremony.
"""

from __future__ import annotations

from raven.trajectory.bundle import BUNDLE_FORMAT_VERSION, collect_bundle
from raven.trajectory.redact import (
    KnownSecret,
    RedactionReport,
    ResidualFinding,
    collect_known_secrets,
    redact_bundle,
    scan_residuals,
)
from raven.trajectory.report import LocalTarballUploader, Uploader, get_uploader, pack_report
from raven.trajectory.store import (
    is_pinned,
    iter_spans,
    pin,
    pins,
    resolve_attempt_id,
    span_log_paths,
    unpin,
)
from raven.trajectory.verdict import (
    VERDICT_STATUSES,
    Verdict,
    latest_verdict,
    read_verdicts,
    record_verdict,
)

__all__ = [
    "BUNDLE_FORMAT_VERSION",
    "VERDICT_STATUSES",
    "KnownSecret",
    "LocalTarballUploader",
    "RedactionReport",
    "ResidualFinding",
    "Uploader",
    "Verdict",
    "collect_bundle",
    "collect_known_secrets",
    "get_uploader",
    "is_pinned",
    "iter_spans",
    "latest_verdict",
    "pack_report",
    "pin",
    "pins",
    "read_verdicts",
    "record_verdict",
    "redact_bundle",
    "resolve_attempt_id",
    "scan_residuals",
    "span_log_paths",
    "unpin",
]
