"""Completeness evaluation for bug report packages.

Answers, for the sanitized redacted tree that becomes the embedded Trajectory
Report, "can the recipient replay and diagnose this evidence?" — three states:

- ``complete``: a warn-mode mock replay of the deliverable runs to the end
  with no fatal divergence and no diagnostic gaps;
- ``degraded``: still diagnosable, but comparison material is missing (LLM or
  tool inputs, the session record, referenced artifacts, excluded binaries, or
  an unlocatable history cut);
- ``unreplayable``: a replay is guaranteed to fail — no turns, corrupt
  records, an unsupported format, a payload shape the replay code cannot
  consume (:func:`raven.trajectory.replay.validate_recording`), or a fatal
  ``exhausted`` / ``missing output`` divergence found by actually driving the
  replay probe.

The unreplayable verdict is anchored to the real replay implementation: after
explicit contract checks, the evaluator executes one warn-mode
:func:`run_replay` (fully mocked feed inside ``trace.suppress()`` with a
throwaway workspace — no side effects) instead of maintaining a parallel model
of AgentLoop semantics. A probe failure that is *not* an explicitly recognized
recording defect (an OSError, an event-loop conflict, a harness regression)
must not masquerade as broken evidence: it propagates to the caller, whose
job is to degrade the whole evaluation to ``unknown``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from raven.trajectory.bundle import BUNDLE_FORMAT_VERSION
from raven.trajectory.redact import RedactionReport
from raven.trajectory.replay import (
    Recording,
    diagnose_history_cut,
    load_recording,
    run_replay,
    validate_recording,
)

STATUS_COMPLETE = "complete"
STATUS_DEGRADED = "degraded"
STATUS_UNREPLAYABLE = "unreplayable"
STATUS_UNKNOWN = "unknown"

REASON_CUT_UNLOCATABLE = "the attempt's starting point in the session history could not be located"


def _corrupt_span_lines(tree: Path) -> int:
    """Non-empty ``spans.jsonl`` lines that are not JSON objects.

    ``load_recording`` silently skips these, so the probe never sees them —
    but a skipped record means the recording is not the trajectory that ran.
    """
    spans_path = tree / "spans.jsonl"
    if not spans_path.is_file():
        return 0
    corrupt = 0
    for line in spans_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if not isinstance(json.loads(line), dict):
                corrupt += 1
        except json.JSONDecodeError:
            corrupt += 1
    return corrupt


def _turn_span_count(tree: Path) -> int:
    """Turn-input spans after the same spanId last-write-wins dedup replay does.

    A turn's checkpoint and close record share a spanId; counting raw lines
    would call every healthy turn a load failure.
    """
    spans_path = tree / "spans.jsonl"
    if not spans_path.is_file():
        return 0
    deduped: dict[str, dict] = {}
    anon = 0
    for line in spans_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            span = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(span, dict):
            continue
        span_id = span.get("spanId")
        if not isinstance(span_id, str) or not span_id:
            anon += 1
            span_id = f"anon-{anon}"
        deduped[span_id] = span
    count = 0
    for span in deduped.values():
        attrs = span.get("attributes")
        # Key presence, not truthiness: sanitization nulls a missing artifact's
        # reference, and that turn is exactly the loss this count must surface.
        if isinstance(attrs, dict) and "turn.input.artifact_path" in attrs:
            count += 1
    return count


def _session_state(tree: Path, manifest: dict) -> tuple[str | None, str | None]:
    """(degraded reason, unreplayable reason) from the session record's file level."""
    session_path = tree / "session.jsonl"
    if not manifest.get("session_included"):
        return "the session conversation record is missing", None
    if not session_path.is_file():
        return "the session conversation record is missing", None
    try:
        lines = session_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "the session conversation record is missing", None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            if not isinstance(json.loads(line), dict):
                return None, "the session record contains corrupt entries"
        except json.JSONDecodeError:
            return None, "the session record contains corrupt entries"
    return None, None


def _probe(tree: Path) -> list[str]:
    """Unreplayable reasons from one warn-mode mock replay of the tree.

    Only explicitly recognized outcomes are classified here: the entry
    rejection (``ValueError``) and fatal divergences. Anything else raises to
    the caller — an unexplained probe failure is an evaluation failure, not
    evidence damage.
    """
    try:
        report = asyncio.run(run_replay(tree, mode="warn"))
    except ValueError:
        return ["replay rejected the recording"]
    fatal = [d for d in report.divergences if d.fatal]
    if fatal:
        return [f"a replay cannot complete: {fatal[0].kind} {fatal[0].field}"]
    return []


def evaluate_completeness(tree: Path, redaction: RedactionReport | None = None) -> tuple[str, list[str]]:
    """(status, reasons) for the sanitized redacted tree that will ship.

    Raises only on evaluation-environment failures (the caller degrades those
    to ``unknown``); every deterministic recording defect is returned as an
    ``unreplayable`` or ``degraded`` reason.
    """
    tree = Path(tree)
    try:
        recording: Recording = load_recording(tree)
    except Exception as exc:
        return STATUS_UNREPLAYABLE, [f"the recording could not be parsed for replay ({type(exc).__name__})"]

    unreplayable: list[str] = []
    degraded: list[str] = []

    corrupt = _corrupt_span_lines(tree)
    if corrupt:
        unreplayable.append(f"{corrupt} span record(s) are corrupt and could not be parsed")

    manifest = recording.manifest if isinstance(recording.manifest, dict) else {}
    version = manifest.get("format_version")
    if version != BUNDLE_FORMAT_VERSION:
        unreplayable.append(f"the bundle format version {version!r} is not supported by this build")

    if not recording.turns:
        # The same condition run_replay rejects on; stated here so the reason
        # is a stable category rather than a caught exception.
        unreplayable.append("no recorded turn inputs; nothing to drive a replay with")

    for category in validate_recording(recording):
        unreplayable.append(f"the recording violates the replay contract: {category}")

    turn_spans = _turn_span_count(tree)
    turn_gap = max(0, turn_spans - len(recording.turns))
    if recording.turns and turn_gap:
        degraded.append(f"{turn_gap} recorded turn input(s) could not be loaded")

    llm_inputs_missing = sum(1 for call in recording.llm_calls if call.input is None)
    if llm_inputs_missing:
        degraded.append(f"{llm_inputs_missing} model call input artifact(s) are missing or unreadable")
    tool_inputs_missing = sum(1 for call in recording.tool_calls if call.name is None and call.params is None)
    if tool_inputs_missing:
        degraded.append(f"{tool_inputs_missing} tool call input artifact(s) are missing or unreadable")

    session_degraded, session_corrupt = _session_state(tree, manifest)
    if session_corrupt:
        unreplayable.append(session_corrupt)
    if session_degraded:
        degraded.append(session_degraded)

    # Role-attributed gaps already carry their own reasons; the generic line
    # covers only what none of them accounted for — settled by count, never by
    # a boolean "some role was missing" that would swallow the whole list.
    missing = manifest.get("missing_artifacts")
    if isinstance(missing, list) and missing:
        role_attributed = (
            turn_gap
            + llm_inputs_missing
            + tool_inputs_missing
            + sum(1 for call in recording.llm_calls if call.output is None)
            + sum(1 for call in recording.tool_calls if call.result is None)
        )
        leftover = len(missing) - role_attributed
        if leftover > 0:
            degraded.append(f"{leftover} referenced artifact(s) are missing")

    if redaction is not None and redaction.skipped_binaries:
        degraded.append(f"{len(redaction.skipped_binaries)} non-UTF-8 file(s) were excluded from the redacted copy")

    if not unreplayable:
        unreplayable.extend(_probe(tree))

    # The cut diagnosis dereferences manifest/session shapes; a tree already
    # judged unreplayable (validate_recording included) must not re-crash here.
    if not unreplayable and not session_corrupt and not session_degraded and recording.turns:
        if diagnose_history_cut(recording) == "unlocatable":
            degraded.append(REASON_CUT_UNLOCATABLE)

    if unreplayable:
        return STATUS_UNREPLAYABLE, unreplayable + degraded
    if degraded:
        return STATUS_DEGRADED, degraded
    return STATUS_COMPLETE, []


__all__ = [
    "REASON_CUT_UNLOCATABLE",
    "STATUS_COMPLETE",
    "STATUS_DEGRADED",
    "STATUS_UNKNOWN",
    "STATUS_UNREPLAYABLE",
    "evaluate_completeness",
]
