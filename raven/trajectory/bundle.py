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
files are skipped and listed in the manifest rather than failing the pack. An
``audit.artifact.v2`` artifact is resolved on the way in - its message
references are replaced by the messages themselves - so a bundle carries no
dependency on the live message store, and an address whose blob is gone
becomes a labelled placeholder listed under the manifest's
``missing_messages``.
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
from typing import Any, Callable, Sequence

from raven import __version__
from raven.config.paths import get_workspace_path
from raven.tracing import artifact_v2
from raven.tracing import config as tracing_config
from raven.trajectory.selection import read_store_file
from raven.trajectory.store import attempt_alias_ids, iter_spans, pin, pin_attempt, resolve_attempt_id
from raven.trajectory.verdict import Verdict, read_verdicts

BUNDLE_FORMAT_VERSION = 1

_ARTIFACTS_DIR = "artifacts"


def _copy_resolved(
    src: Path,
    target: Path,
    store_artifacts: Path,
    missing_messages: list[str],
    *,
    text: str | None = None,
    read: Callable[[Path], str | None] | None = None,
) -> bool:
    """Write ``src`` (or its already-read ``text``) to ``target`` resolved; False when it is not a v2 shell.

    Unreadable or unparseable input answers False so the caller falls back to
    a byte copy: a file this cannot understand is still worth packing.
    ``read`` bounds how the shell's blobs are read.
    """
    try:
        payload = json.loads(src.read_text(encoding="utf-8") if text is None else text)
    except (OSError, ValueError):
        return False
    if not artifact_v2.is_v2(payload):
        return False

    def record(sha1: str) -> dict[str, str]:
        if sha1 not in missing_messages:
            missing_messages.append(sha1)
        return artifact_v2.placeholder(sha1)

    resolved = artifact_v2.resolve_payload(payload, store_artifacts, on_missing=record, read=read)
    target.write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _confined_text(root: Path) -> Callable[[Path], str | None]:
    def read(path: Path) -> str | None:
        found = read_store_file(path, root)
        if found is None:
            return None
        try:
            return found[0].decode("utf-8")
        except UnicodeDecodeError:
            return None

    return read


def _import_artifact(
    source: str,
    artifacts_dir: Path,
    copied: dict[str, str],
    missing: list[str],
    names: dict[str, str],
    store_artifacts: Path,
    missing_messages: list[str],
    confine: Path | None = None,
) -> str | None:
    """Copy one referenced file into the bundle; return its relative path.

    Deduped per source path. Distinct sources sharing a basename get a
    counter prefix so neither overwrites the other. Returns None (and records
    the source in ``missing``) when the file no longer exists.

    A v2 shell is resolved on the way in, so the bundle holds v1-shaped
    artifacts and stays readable on a machine that has no message store. That
    is the bundle's whole promise; keeping references here would break it.

    With ``confine``, the file and every blob its shell references are read
    only when they resolve inside that root and are regular files, opened so
    that a FIFO cannot block; anything else is recorded as missing.
    """
    if source in copied:
        return copied[source]
    if source in missing:
        return None
    src = Path(source)
    data: bytes | None = None
    if confine is not None:
        found = read_store_file(src, confine)
        if found is None:
            missing.append(source)
            return None
        data = found[0]
    elif not src.is_file():
        missing.append(source)
        return None
    name = src.name
    counter = 1
    while name in names and names[name] != source:
        name = f"{counter}-{src.name}"
        counter += 1
    names[name] = source
    target = artifacts_dir / name
    if data is not None:
        try:
            text: str | None = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        read = _confined_text(confine) if confine is not None else None
        if text is None or not _copy_resolved(src, target, store_artifacts, missing_messages, text=text, read=read):
            target.write_bytes(data)
    elif not _copy_resolved(src, target, store_artifacts, missing_messages):
        shutil.copy2(src, target)
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

    return SessionManager(workspace or _default_workspace()).session_path(session_key)


def write_bundle_tree(
    dest: Path,
    spans: Sequence[dict[str, Any]],
    *,
    attempt_id: str,
    state_dir: Path,
    write_session: Callable[[Path, str | None], bool],
    verdicts: Sequence[Verdict],
    session_key: str | None = None,
    confine: Path | None = None,
) -> dict[str, Any]:
    """Write a bundle's files for ``spans`` into the existing directory ``dest``; return the manifest.

    The artifacts the spans reference are imported (v2 shells resolved) and
    their pointers rewritten. ``session_key`` defaults to the first one the
    spans carry, which is the attempt's own when the spans are one attempt's;
    ``write_session(target, session_key)`` writes ``session.jsonl`` and says
    whether it did. ``confine`` (the resolved artifact store) restricts every
    read to regular files inside it; see :func:`_import_artifact`.
    """
    artifacts_dir = dest / _ARTIFACTS_DIR
    artifacts_dir.mkdir()

    copied: dict[str, str] = {}
    missing: list[str] = []
    missing_messages: list[str] = []
    names: dict[str, str] = {}
    rewritten = 0
    out_spans: list[dict[str, Any]] = []
    derived_key: str | None = None
    start: str | None = None
    end: str | None = None
    for span in spans:
        # Historical records are unvalidated JSON: a truthy non-object
        # attributes value or non-string key/timestamp degrades per-field.
        raw_attrs = span.get("attributes")
        attrs = dict(raw_attrs) if isinstance(raw_attrs, dict) else {}
        if derived_key is None and isinstance(attrs.get("session.key"), str) and attrs["session.key"]:
            derived_key = attrs["session.key"]
        span_start, span_end = span.get("startTime"), span.get("endTime")
        if isinstance(span_start, str) and span_start and (start is None or span_start < start):
            start = span_start
        if isinstance(span_end, str) and span_end and (end is None or span_end > end):
            end = span_end
        for key, value in attrs.items():
            if not key.endswith(".artifact_path") or not isinstance(value, str) or not value:
                continue
            rel = _import_artifact(
                value,
                artifacts_dir,
                copied,
                missing,
                names,
                state_dir / "logs" / "audit-artifacts",
                missing_messages,
                confine,
            )
            if rel is not None:
                attrs[key] = rel
                rewritten += 1
        out_spans.append({**span, "attributes": attrs})

    (dest / "spans.jsonl").write_text(
        "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in out_spans), encoding="utf-8"
    )
    (dest / "verdicts.jsonl").write_text(
        "".join(json.dumps(asdict(v), ensure_ascii=False) + "\n" for v in verdicts), encoding="utf-8"
    )
    key = session_key if session_key is not None else derived_key
    session_included = write_session(dest / "session.jsonl", key)

    manifest = {
        "format_version": BUNDLE_FORMAT_VERSION,
        "attempt_id": attempt_id,
        "session_key": key,
        "time_range": {"start": start, "end": end},
        "span_count": len(out_spans),
        "artifact_count": len(copied),
        "rewritten_artifact_paths": rewritten,
        "missing_artifacts": missing,
        "missing_messages": missing_messages,
        "session_included": session_included,
        "verdict_count": len(verdicts),
        "raven_version": __version__,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


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
    # A turn's trace id may address a multi-turn attempt; the canonical
    # attempt id owns the whole trajectory, so collect and name by it.
    attempt_id = resolve_attempt_id(id_, resolved_state)
    if attempt_id is None:
        raise LookupError(f"no spans found for id {id_!r}")
    spans = list(iter_spans(resolved_state, attempt_id=attempt_id))
    if not spans:
        raise LookupError(f"no spans found for attempt {attempt_id!r}")

    out_root = (out_dir or resolved_state / "bundles").resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    # Attempt ids come from span records or the hand-editable attempts.json,
    # so refuse any id that would escape out_root as a directory name.
    bundle_dir = (out_root / attempt_id).resolve()
    if bundle_dir.parent != out_root:
        raise ValueError(f"id {attempt_id!r} cannot be used as a bundle directory name")

    def copy_session(target: Path, session_key: str | None) -> bool:
        if not session_key:
            return False
        source = _session_source(session_key, workspace)
        if not source.is_file():
            return False
        shutil.copyfile(source, target)
        return True

    # Build in a staging dir and swap in whole, so a re-pack never inherits
    # stale files (e.g. an artifact whose source was purged since last pack).
    staging = Path(tempfile.mkdtemp(prefix=f".{bundle_dir.name}-", dir=out_root))
    try:
        verdicts = read_verdicts(resolved_state, attempt_ids=attempt_alias_ids(attempt_id, resolved_state))
        write_bundle_tree(
            staging,
            spans,
            attempt_id=attempt_id,
            state_dir=resolved_state,
            write_session=copy_session,
            verdicts=verdicts,
        )

        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        staging.rename(bundle_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    try:
        pin_attempt(attempt_id, reason="bundled", state_dir=resolved_state)
    except LookupError:
        # The attempt address vanished mid-pack (a concurrent split/merge);
        # protect exactly the traces that were bundled instead of leaving
        # them purgeable behind a dangling id.
        for member in dict.fromkeys(s.get("traceId") for s in spans if s.get("traceId")):
            pin(member, reason="bundled", state_dir=resolved_state)
    return bundle_dir
