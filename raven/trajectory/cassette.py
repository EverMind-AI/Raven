"""Cassette minimizer — a bundle shrunk and redacted into a committable form.

A Trajectory Cassette is a Trajectory Bundle reduced to exactly what replay
consumes, then redacted. The regression suite (``tests/trajectories/``) checks
trajectories into git, where the repository forbids large files and secrets
are unacceptable — a raw bundle (multi-MB spans + artifacts, full model I/O)
qualifies for neither. The cassette keeps the bundle directory layout, so
:func:`raven.trajectory.replay.load_recording` and ``raven trajectory replay``
consume it unchanged.

What survives minimization (the ``load_recording`` consumption surface):

- spans carrying ``llm.*`` / ``tool.*`` / ``turn.input`` artifact references —
  every other span (memory, skills, channel plumbing) is dropped — with span
  records and attributes stripped to the consumed keys;
- the referenced artifacts, each payload stripped to the fields replay feeds
  or compares. Recorded system-prompt content is replaced with a placeholder:
  replay never compares it, and it is both the bulk of a recorded request and
  the part that leaks recording-machine paths. Payloads are never truncated —
  a truncated payload would silently change what the divergence comparison
  sees, so a field is either kept whole or dropped whole;
- the ``session.jsonl`` slice that seeds pre-attempt history: everything up to
  and including the attempt's opening record, verified to reseed exactly what
  the full file would (falling back to a full copy when it would not);
- the manifest keys replay reads, plus a ``minimized`` stats block.

The minimized tree is then passed through :func:`redact_bundle` — the cassette
is the redacted copy, ``redaction.json`` included. Minimization refuses a
bundle that is not fully replayable (a missing ``llm.input``/``llm.output``
payload or tool result): an incomplete recording cannot guard a regression.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from raven.trajectory.redact import KnownSecret, RedactionReport, redact_bundle
from raven.trajectory.replay import (
    _history_cut,
    _load_artifact,
    _pre_attempt_messages,
    _session_records,
    load_recording,
)

_SYSTEM_PLACEHOLDER = "<system prompt: not compared>"

_ARTIFACTS_DIR = "artifacts"

_SPAN_ATTR_KEYS = (
    "attempt.id",
    "session.key",
    "llm.stream",
    "llm.input.artifact_path",
    "llm.output.artifact_path",
    "tool.input.artifact_path",
    "tool.output.artifact_path",
    "turn.input.artifact_path",
)

_PAYLOAD_KEYS = {
    "llm.input.artifact_path": ("model", "messages", "tools"),
    "llm.output.artifact_path": ("content", "tool_calls", "finish_reason", "usage", "reasoning_content"),
    "tool.input.artifact_path": ("name", "params"),
    "tool.output.artifact_path": ("result",),
    "turn.input.artifact_path": ("content", "channel", "chat_id"),
}


@dataclass
class CassetteReport:
    """What one :func:`minimize_bundle` run kept, dropped, and redacted."""

    bundle_dir: Path
    cassette_dir: Path
    original_bytes: int
    cassette_bytes: int
    span_count: int
    source_span_count: int
    artifact_count: int
    llm_calls: int
    tool_calls: int
    turns: int
    session: str  # "sliced" | "copied" | "omitted"
    redaction: RedactionReport


def _tree_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _deduped_spans(spans_path: Path) -> list[dict[str, Any]]:
    """Spans deduplicated by id, last record winning — the exact view
    ``load_recording`` iterates, so the cassette preserves replay order."""
    deduped: dict[str, dict[str, Any]] = {}
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
        span_id = span.get("spanId") or f"anon-{len(deduped)}"
        if span_id in deduped:
            del deduped[span_id]
        deduped[span_id] = span
    return list(deduped.values())


def _consumed(span: dict[str, Any]) -> bool:
    attrs = span.get("attributes") or {}
    if attrs.get("llm.output.artifact_path") or span.get("name") == "llm.call":
        return True
    return bool(
        attrs.get("tool.input.artifact_path")
        or attrs.get("tool.output.artifact_path")
        or attrs.get("turn.input.artifact_path")
    )


def _strip_payload(payload: Any, ref_key: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    kept = {k: payload[k] for k in _PAYLOAD_KEYS[ref_key] if k in payload}
    if ref_key == "llm.input.artifact_path" and isinstance(kept.get("messages"), list):
        # Message internals stay untouched otherwise: cache-control block
        # rewrites must survive verbatim for prompt_cache.strip to undo them
        # at compare time.
        kept["messages"] = [
            {**m, "content": _SYSTEM_PLACEHOLDER} if isinstance(m, dict) and m.get("role") == "system" else m
            for m in kept["messages"]
        ]
    return kept


def _check_replayable(recording) -> None:
    problems: list[str] = []
    if not recording.turns:
        problems.append("no recorded turn inputs")
    for i, call in enumerate(recording.llm_calls):
        if call.input is None:
            problems.append(f"llm call #{i + 1} has no llm.input payload")
        if call.output is None:
            problems.append(f"llm call #{i + 1} has no llm.output payload")
    for i, call in enumerate(recording.tool_calls):
        if call.name is None:
            problems.append(f"tool call #{i + 1} has no tool.input payload")
        if call.result is None:
            problems.append(f"tool call #{i + 1} has no usable tool.output payload")
    if problems:
        raise ValueError("bundle is not fully replayable, refusing to minimize: " + "; ".join(problems))


def _validated_ref(bundle_dir: Path, ref: str) -> Path | None:
    """The bundle file a span's artifact reference names, traversal-checked.

    ``None`` for an absolute reference (a source already missing at pack time
    keeps its original absolute path — a dangling ref, not an error). Anything
    that would leave the bundle's ``artifacts/`` directory — ``..``/``.``
    segments, a path outside ``artifacts/``, or a symlink resolving out of the
    bundle — raises: references come from span records (mintable through
    recorded data), and an escaping one would both read a foreign file into
    the cassette and write outside the staging tree.
    """
    rel = PurePosixPath(ref)
    if rel.is_absolute():
        return None
    escapes = ValueError(f"artifact reference {ref!r} escapes the bundle's {_ARTIFACTS_DIR}/ directory")
    if rel.parts[:1] != (_ARTIFACTS_DIR,) or any(part in ("..", ".") for part in rel.parts):
        raise escapes
    source = bundle_dir / ref
    if source.exists() and not source.resolve().is_relative_to(bundle_dir.resolve()):
        raise escapes
    return source


def _check_dest_replaceable(dest_dir: Path) -> None:
    """Refuse a destination the swap-in step must not delete.

    The swap replaces ``dest_dir`` whole; only a fresh path, an empty
    directory, or a previous cassette (a ``manifest.json`` with a
    ``minimized`` block) may stand there — never an arbitrary directory
    (a stray ``--out .`` must not delete the working directory).
    """
    if not dest_dir.exists():
        return
    if not dest_dir.is_dir():
        raise ValueError(f"destination {dest_dir} exists and is not a directory")
    if not any(dest_dir.iterdir()):
        return
    try:
        manifest = json.loads((dest_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = None
    if not (isinstance(manifest, dict) and isinstance(manifest.get("minimized"), dict)):
        raise ValueError(
            f"destination {dest_dir} exists and is not a cassette; refusing to delete it — choose a new or empty path"
        )


def _leading_metadata(session_path: Path) -> str | None:
    for line in session_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        return line if isinstance(data, dict) and data.get("_type") == "metadata" else None
    return None


def minimize_bundle(
    bundle_dir: Path,
    dest_dir: Path,
    *,
    secrets: list[KnownSecret] | None = None,
    config_path: Path | None = None,
) -> CassetteReport:
    """Write the cassette form of ``bundle_dir`` at ``dest_dir``.

    The original bundle is never touched. ``secrets`` / ``config_path`` are
    passed through to :func:`redact_bundle`. An existing ``dest_dir`` is
    replaced whole (built in staging, swapped in), so a re-minimize never
    leaves stale files behind — but only an empty directory or a previous
    cassette is ever replaced (see :func:`_check_dest_replaceable`). Raises
    ``ValueError`` for a bundle that is not fully replayable.
    """
    bundle_dir = Path(bundle_dir).resolve()
    recording = load_recording(bundle_dir)
    _check_replayable(recording)
    original_manifest = recording.manifest

    dest_dir = Path(dest_dir).resolve()
    if bundle_dir in (dest_dir, *dest_dir.parents):
        raise ValueError("destination must be outside the source bundle")
    if dest_dir in bundle_dir.parents:
        raise ValueError("destination must not contain the source bundle")
    _check_dest_replaceable(dest_dir)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)

    source_spans = _deduped_spans(bundle_dir / "spans.jsonl")
    work = Path(tempfile.mkdtemp(prefix=f".{dest_dir.name}-", dir=dest_dir.parent))
    try:
        minimized = work / "minimized"
        artifacts_dir = minimized / "artifacts"
        artifacts_dir.mkdir(parents=True)

        kept_spans: list[dict[str, Any]] = []
        written: set[str] = set()
        for span in source_spans:
            if not _consumed(span):
                continue
            attrs = span.get("attributes") or {}
            out_attrs: dict[str, Any] = {}
            for key in _SPAN_ATTR_KEYS:
                value = attrs.get(key)
                if value is None or value == "":
                    continue
                if key in _PAYLOAD_KEYS:
                    if _validated_ref(bundle_dir, value) is None:
                        continue
                    payload = _load_artifact(bundle_dir, value)
                    if payload is None:
                        # A dangling reference the replayability check
                        # tolerated (e.g. a turn.input whose source was
                        # missing at pack time) carries nothing to replay.
                        continue
                    if value not in written:
                        # value is validated to stay under artifacts/, so this
                        # join cannot leave the staging tree.
                        target = minimized / value
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(
                            json.dumps(_strip_payload(payload, key), ensure_ascii=False), encoding="utf-8"
                        )
                        written.add(value)
                out_attrs[key] = value
            if not any(k in _PAYLOAD_KEYS for k in out_attrs):
                continue
            kept_spans.append(
                {
                    "traceId": span.get("traceId"),
                    "spanId": span.get("spanId"),
                    "name": span.get("name"),
                    "attributes": out_attrs,
                }
            )
        artifact_count = len(written)
        (minimized / "spans.jsonl").write_text(
            "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in kept_spans), encoding="utf-8"
        )

        session_src = bundle_dir / "session.jsonl"
        session_mode = "omitted"
        records: list[dict[str, Any]] = []
        cut: int | None = None
        if session_src.is_file():
            records = _session_records(session_src)
            cut = _history_cut(recording, records)
            if cut is not None and cut > 0:
                head = _leading_metadata(session_src)
                lines = ([head] if head else []) + [json.dumps(r, ensure_ascii=False) for r in records[: cut + 1]]
                (minimized / "session.jsonl").write_text("".join(x + "\n" for x in lines), encoding="utf-8")
                session_mode = "sliced"

        manifest = {
            "format_version": original_manifest.get("format_version"),
            "attempt_id": original_manifest.get("attempt_id"),
            "session_key": original_manifest.get("session_key"),
            "time_range": original_manifest.get("time_range"),
            "raven_version": original_manifest.get("raven_version"),
            "minimized": {
                "source_span_count": len(source_spans),
                "span_count": len(kept_spans),
                "artifact_count": artifact_count,
                "llm_calls": len(recording.llm_calls),
                "tool_calls": len(recording.tool_calls),
                "turns": len(recording.turns),
                "session": session_mode,
            },
        }
        (minimized / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        if session_mode == "sliced":
            # The slice must reseed exactly what the full file would; on any
            # disagreement (e.g. clock skew interacting differently with the
            # sliced timestamps) ship the whole file rather than a wrong cut.
            if _pre_attempt_messages(load_recording(minimized)) != records[:cut]:
                shutil.copyfile(session_src, minimized / "session.jsonl")
                session_mode = "copied"
                manifest["minimized"]["session"] = session_mode
                (minimized / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )

        redacted = work / "cassette"
        redaction = redact_bundle(minimized, redacted, secrets=secrets, config_path=config_path)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        redacted.rename(dest_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return CassetteReport(
        bundle_dir=bundle_dir,
        cassette_dir=dest_dir,
        original_bytes=_tree_bytes(bundle_dir),
        cassette_bytes=_tree_bytes(dest_dir),
        span_count=len(kept_spans),
        source_span_count=len(source_spans),
        artifact_count=artifact_count,
        llm_calls=len(recording.llm_calls),
        tool_calls=len(recording.tool_calls),
        turns=len(recording.turns),
        session=session_mode,
        redaction=redaction,
    )
