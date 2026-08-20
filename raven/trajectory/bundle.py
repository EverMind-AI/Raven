"""Bundle collector — one trajectory packed into a self-contained directory.

A bundle is the offline form of a trajectory: everything needed to inspect
(and later report or replay) one attempt, collected out of the live tracing
store into a single directory that survives log rotation, artifact cleanup,
and being copied to another machine::

    <out>/<attempt_id>/
      manifest.json     # format version, ids, time range, counts, raven version
      spans.jsonl       # every span of the attempt, in write order
      artifacts/        # every file the spans' *.artifact_path attrs referenced
      session.jsonl     # the session's conversation record (omitted if missing)
      verdicts.jsonl    # every verdict recorded for the attempt

Artifact references inside ``spans.jsonl`` are rewritten to bundle-relative
paths (``artifacts/<name>``) so the bundle reads offline; missing artifact
files are skipped and listed in the manifest rather than failing the pack.
A trace id addressing a multi-turn attempt is resolved to the canonical
attempt id first, so the bundle always holds the whole trajectory. The
bundle is built in a staging directory and swapped in whole, so a re-pack
never leaves stale files from a previous pack behind. Bundling declares the
trajectory corpus, so the id is auto-pinned.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from raven import __version__
from raven.config.paths import get_workspace_path
from raven.tracing import config as tracing_config
from raven.trajectory.store import iter_spans, pin
from raven.trajectory.verdict import read_verdicts

BUNDLE_FORMAT_VERSION = 1

_ARTIFACTS_DIR = "artifacts"


def _import_artifact(
    source: str,
    artifacts_dir: Path,
    copied: dict[str, str],
    missing: list[str],
    names: dict[str, str],
) -> str | None:
    """Copy one referenced file into the bundle; return its relative path.

    Deduped per source path. Distinct sources sharing a basename get a
    counter prefix so neither overwrites the other. Returns None (and records
    the source in ``missing``) when the file no longer exists.
    """
    if source in copied:
        return copied[source]
    if source in missing:
        return None
    src = Path(source)
    if not src.is_file():
        missing.append(source)
        return None
    name = src.name
    counter = 1
    while name in names and names[name] != source:
        name = f"{counter}-{src.name}"
        counter += 1
    names[name] = source
    shutil.copy2(src, artifacts_dir / name)
    rel = f"{_ARTIFACTS_DIR}/{name}"
    copied[source] = rel
    return rel


def _default_workspace() -> Path:
    """The configured agent workspace; the stock default when config is unreadable."""
    try:
        from raven.config.loader import load_config

        return load_config().workspace_path
    except Exception:
        return get_workspace_path()


def _session_source(session_key: str, workspace: Path | None) -> Path:
    from raven.session.manager import SessionManager

    return SessionManager(workspace or _default_workspace())._get_session_path(session_key)


def collect_bundle(
    id_: str,
    out_dir: Path | None = None,
    state_dir: Path | None = None,
    *,
    workspace: Path | None = None,
) -> Path:
    """Pack the trajectory addressed by ``id_`` (attempt id or trace id).

    Reads spans through :func:`raven.trajectory.store.iter_spans` (rotation-
    transparent, both id kinds). A trace id belonging to a multi-turn attempt
    is resolved to that attempt's canonical id, and the whole attempt (every
    turn, its verdicts, the pin) is collected under that id. Writes the bundle
    under ``out_dir`` (default: ``<state_dir>/bundles``) and pins the id.
    Raises ``LookupError`` when no span matches ``id_``.
    """
    if not id_:
        raise ValueError("id is required")
    resolved_state = state_dir or tracing_config.state_dir()
    spans = list(iter_spans(resolved_state, attempt_id=id_))
    if not spans:
        raise LookupError(f"no spans found for id {id_!r}")

    # A turn's trace id may address a multi-turn attempt; the canonical
    # attempt id owns the whole trajectory, so collect and name by it.
    attempt_id = id_
    for span in spans:
        span_attempt = (span.get("attributes") or {}).get("attempt.id")
        if span_attempt:
            attempt_id = span_attempt
            break
    if attempt_id != id_:
        spans = list(iter_spans(resolved_state, attempt_id=attempt_id))

    out_root = (out_dir or resolved_state / "bundles").resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    # Attempt ids come from span records (user-mintable via begin_attempt), so
    # refuse any id that would escape out_root as a directory name.
    bundle_dir = (out_root / attempt_id).resolve()
    if bundle_dir.parent != out_root:
        raise ValueError(f"id {attempt_id!r} cannot be used as a bundle directory name")

    # Build in a staging dir and swap in whole, so a re-pack never inherits
    # stale files (e.g. an artifact whose source was purged since last pack).
    staging = Path(tempfile.mkdtemp(prefix=f".{bundle_dir.name}-", dir=out_root))
    try:
        artifacts_dir = staging / _ARTIFACTS_DIR
        artifacts_dir.mkdir()

        copied: dict[str, str] = {}
        missing: list[str] = []
        names: dict[str, str] = {}
        rewritten = 0
        out_spans: list[dict[str, Any]] = []
        session_key: str | None = None
        start: str | None = None
        end: str | None = None
        for span in spans:
            attrs = dict(span.get("attributes") or {})
            if session_key is None and attrs.get("session.key"):
                session_key = attrs["session.key"]
            span_start, span_end = span.get("startTime"), span.get("endTime")
            if span_start and (start is None or span_start < start):
                start = span_start
            if span_end and (end is None or span_end > end):
                end = span_end
            for key, value in attrs.items():
                if not key.endswith(".artifact_path") or not isinstance(value, str) or not value:
                    continue
                rel = _import_artifact(value, artifacts_dir, copied, missing, names)
                if rel is not None:
                    attrs[key] = rel
                    rewritten += 1
            out_spans.append({**span, "attributes": attrs})

        (staging / "spans.jsonl").write_text(
            "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in out_spans), encoding="utf-8"
        )

        verdicts = read_verdicts(resolved_state, attempt_id=attempt_id)
        (staging / "verdicts.jsonl").write_text(
            "".join(json.dumps(asdict(v), ensure_ascii=False) + "\n" for v in verdicts), encoding="utf-8"
        )

        session_included = False
        if session_key:
            source = _session_source(session_key, workspace)
            if source.is_file():
                shutil.copyfile(source, staging / "session.jsonl")
                session_included = True

        manifest = {
            "format_version": BUNDLE_FORMAT_VERSION,
            "attempt_id": attempt_id,
            "session_key": session_key,
            "time_range": {"start": start, "end": end},
            "span_count": len(out_spans),
            "artifact_count": len(copied),
            "rewritten_artifact_paths": rewritten,
            "missing_artifacts": missing,
            "session_included": session_included,
            "verdict_count": len(verdicts),
            "raven_version": __version__,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        staging.rename(bundle_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    pin(attempt_id, reason="bundled", state_dir=resolved_state)
    return bundle_dir
