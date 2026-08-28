"""Trajectory layer over the tracing store.

Tracing records *what happened* (``audit.span.v1`` spans + artifacts, see
:mod:`raven.tracing`); this package adds the semantics that turn those records
into trajectories — addressable, labeled, retained units of agent work:

- ``verdict``  — the label sidecar. Task success/failure is not tracing's
  business (``status.code`` answers "did the code crash", not "did the task
  succeed"), so verdicts live in a separate append-only file keyed by
  attempt id, written by whoever can judge (user command, eval judge).
- ``store``    — pin registry, attempt definitions, and the rotation-
  transparent span reader. A pinned attempt is corpus, not diagnostics: any
  purge tooling must consult :func:`store.pins` before deleting spans or the
  artifacts they reference. Attempt definitions (``attempts.json``) are the
  mutable merge/split sidecar: an attempt id equals the trace id unless a
  definition groups several traces under one minted id.
- ``bundle``   — the bundle collector. Packs one attempt's spans, artifacts,
  session record, and verdicts into a self-contained offline directory (and
  pins the id, since bundling declares the trajectory corpus).
- ``redact``   — the sanitizer. Produces a redacted **copy** of a bundle
  (known secret values, credential patterns, residual scan); the original
  bundle is never modified.
- ``report``   — the shippable form: the redacted copy packed into a
  ``.tar.gz``, delivered through the pluggable :class:`report.Uploader`
  (v1: local file only).
- ``replay``   — deterministic replay. Feeds a bundle's recorded model
  replies and tool results back through the live harness (mock replay: no
  real tool ever executes), with strict/warn divergence policies.
- ``cassette`` — the minimizer. Shrinks a bundle to the exact surface replay
  consumes and redacts the result: the committable Trajectory Cassette the
  regression suite replays.
- ``regression`` — the regression-case layer: ``expect.yaml`` expectations
  (where the replay must diverge, what the live side must do there) evaluated
  against a cassette replay, driving ``tests/trajectories/``.

The address unit is the **attempt**: one task try, possibly spanning several
turns. At read time an attempt id equals the trace id unless a definition in
``attempts.json`` groups several traces under one minted id — so every trace
is addressable as an attempt with zero ceremony. Old logs may carry a legacy
span-level ``attempt.id`` attribute, which read paths keep resolving.
"""

from __future__ import annotations

from raven.trajectory.bundle import BUNDLE_FORMAT_VERSION, collect_bundle
from raven.trajectory.cassette import CassetteReport, minimize_bundle
from raven.trajectory.redact import (
    KnownSecret,
    RedactionReport,
    ResidualFinding,
    collect_known_secrets,
    redact_bundle,
    scan_residuals,
)
from raven.trajectory.regression import (
    Check,
    DivergenceExpectation,
    RegressionExpectation,
    check_report,
    load_expectation,
    run_regression_case,
)
from raven.trajectory.replay import (
    Divergence,
    Mismatch,
    Recording,
    ReplayProvider,
    ReplayReport,
    ReplayState,
    ReplayToolRegistry,
    load_recording,
    run_replay,
)
from raven.trajectory.report import LocalTarballUploader, Uploader, get_uploader, pack_report
from raven.trajectory.store import (
    attempt_alias_ids,
    attempt_members,
    definitions,
    is_pinned,
    iter_spans,
    merge_attempts,
    new_attempt_id,
    owning_attempt,
    pin,
    pin_attempt,
    pins,
    resolve_attempt_id,
    span_log_paths,
    split_attempt,
    unpin,
    unpin_attempt,
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
    "CassetteReport",
    "Check",
    "Divergence",
    "DivergenceExpectation",
    "KnownSecret",
    "LocalTarballUploader",
    "Mismatch",
    "Recording",
    "RedactionReport",
    "RegressionExpectation",
    "ReplayProvider",
    "ReplayReport",
    "ReplayState",
    "ReplayToolRegistry",
    "ResidualFinding",
    "Uploader",
    "Verdict",
    "attempt_alias_ids",
    "attempt_members",
    "check_report",
    "collect_bundle",
    "collect_known_secrets",
    "definitions",
    "get_uploader",
    "is_pinned",
    "iter_spans",
    "latest_verdict",
    "load_expectation",
    "load_recording",
    "merge_attempts",
    "minimize_bundle",
    "new_attempt_id",
    "owning_attempt",
    "pack_report",
    "pin",
    "pin_attempt",
    "pins",
    "read_verdicts",
    "record_verdict",
    "redact_bundle",
    "resolve_attempt_id",
    "run_regression_case",
    "run_replay",
    "scan_residuals",
    "span_log_paths",
    "split_attempt",
    "unpin",
    "unpin_attempt",
]
