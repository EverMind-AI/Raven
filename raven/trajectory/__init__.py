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
- ``entries``  — the structured projection. Turns a logical-span collection
  into trajectory entries with a stable ``trace:span:slot`` identity, an
  operation status with evidence, integrity codes, and one Timing Owner per
  span — the data layer behind the Web trajectory view, sharing per-span
  expansion with ``conversation``.
- ``index``    — the per-session incremental index: scans the span log chain
  under a byte/time budget, decides trace membership (own session key or a
  proven sub-agent dispatch link), projects member spans into entries and
  serves consistent list snapshots plus per-change revisions.
- ``policy``   — whether this process serves the trajectory view at all; a
  launch flag today, replaceable without touching the readers.
- ``details``  — the detail pane's data: a descriptor (status, notes, one
  bounded summary per block) and per-block bodies for one entry, read from a
  single consistent capture of the index under byte and read budgets.

The address unit is the **attempt**: one task try, possibly spanning several
turns. At read time an attempt id equals the trace id unless a definition in
``attempts.json`` groups several traces under one minted id — so every trace
is addressable as an attempt with zero ceremony. Old logs may carry a legacy
span-level ``attempt.id`` attribute, which read paths keep resolving.
"""

from __future__ import annotations

from raven.trajectory.bundle import BUNDLE_FORMAT_VERSION, collect_bundle
from raven.trajectory.cassette import CassetteReport, minimize_bundle
from raven.trajectory.details import (
    BlockBody,
    BlockDescriptor,
    Descriptor,
    describe,
    read_block,
)
from raven.trajectory.entries import (
    Projection,
    TrajectoryEntry,
    TurnInfo,
    merge_snapshots,
    project_entries,
)
from raven.trajectory.index import EntryView, SessionIndex, TrajectoryIndexer, indexer_for
from raven.trajectory.policy import TrajectoryPolicy
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
    read_verdicts,
    record_verdict,
)

__all__ = [
    "BUNDLE_FORMAT_VERSION",
    "VERDICT_STATUSES",
    "BlockBody",
    "BlockDescriptor",
    "CassetteReport",
    "Check",
    "Descriptor",
    "Divergence",
    "DivergenceExpectation",
    "EntryView",
    "KnownSecret",
    "LocalTarballUploader",
    "Mismatch",
    "Projection",
    "Recording",
    "RedactionReport",
    "RegressionExpectation",
    "ReplayProvider",
    "ReplayReport",
    "ReplayState",
    "ReplayToolRegistry",
    "ResidualFinding",
    "SessionIndex",
    "TrajectoryEntry",
    "TrajectoryIndexer",
    "TrajectoryPolicy",
    "TurnInfo",
    "Uploader",
    "Verdict",
    "attempt_alias_ids",
    "attempt_members",
    "check_report",
    "collect_bundle",
    "collect_known_secrets",
    "definitions",
    "describe",
    "get_uploader",
    "indexer_for",
    "is_pinned",
    "iter_spans",
    "load_expectation",
    "load_recording",
    "merge_attempts",
    "merge_snapshots",
    "minimize_bundle",
    "new_attempt_id",
    "owning_attempt",
    "pack_report",
    "pin",
    "pin_attempt",
    "pins",
    "project_entries",
    "read_block",
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
