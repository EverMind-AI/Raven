"""Reading a package back in: the bounded unpack and the normalization behind an Imported Conversation.

Three packages are accepted, each told apart by what its single root directory
holds:

- a Trajectory Export (``export.json`` naming ``raven_trajectory_export``),
- a Bug Report Package (``bugreport.json`` beside ``trajectory/<attempt>.tar.gz``),
- a Trajectory Report (a redacted bundle: ``manifest.json`` and ``redaction.json``).

The archive is untrusted: the formats are public and a package may be crafted.
Every layer is walked once through :func:`raven.trajectory.package_check.walk_archive`,
which bounds what the gzip stream may produce and refuses anything but plain
files and directories under safe relative names; the nested tarball of a Bug
Report Package spends what the outer layer left of the same budget. Files are
created exclusively under directories this module made itself, so no member
can write through a link.

Normalization then lays the package out the way the trajectory index and the
detail layer already read a trace store -- ``state/logs/audit-spans.log`` and
``state/logs/audit-artifacts/`` -- with artifact pointers rewritten relative to
that state directory, and the session record beside it. A bundle's model inputs
were resolved to v1 when it was packed; each is split back into a v2 shell and
its message blobs so a long conversation reads whole instead of stopping at the
per-artifact read cap.

Every refusal is a :class:`~raven.trajectory.package_check.PackageError` whose
``code`` is stable; callers clean up the destination themselves.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from raven.tracing import artifact_v2
from raven.tracing.store import TraceStore
from raven.trajectory import package_check as pcheck
from raven.trajectory.bundle import BUNDLE_FORMAT_VERSION
from raven.trajectory.export import EXPORT_FORMAT_VERSION, EXPORT_ID, EXPORT_SCHEMA
from raven.trajectory.index import MAX_LINE_BYTES
from raven.trajectory.package_check import ArchiveMember, PackageError, PackageLimits

KIND_EXPORT = "export"
KIND_BUGREPORT = "bugreport"
KIND_REPORT = "report"

MAX_PACKAGE_BYTES = 100 * 2**20

CODE_NOT_IMPORTABLE = "not_importable"
CODE_UNSUPPORTED_VERSION = "unsupported_version"
CODE_MANIFEST_INVALID = "manifest_invalid"
CODE_BAD_ENCODING = "bad_encoding"

EXPORT_MANIFEST = "export.json"
REPORT_METADATA = "bugreport.json"
BUNDLE_MANIFEST = "manifest.json"
REDACTION_FILE = "redaction.json"
SPANS_FILE = "spans.jsonl"
SESSION_FILE = "session.jsonl"
ARTIFACTS_DIR = "artifacts"
MESSAGES_DIR = "messages"

STATE_DIR = "state"
SPAN_LOG = "logs/audit-spans.log"
ARTIFACT_ROOT = "logs/audit-artifacts"

_RESERVED_ARTIFACT_DIRS = frozenset(name.casefold() for name in (artifact_v2.MESSAGES_DIR_NAME, "_blobs"))
_BLOB_NAME = re.compile(r"^([0-9a-f]{2})/([0-9a-f]{40})\.json$")
_LLM_INPUT_POINTER = "llm.input.artifact_path"
_POINTER_SUFFIX = ".artifact_path"


@dataclass(frozen=True)
class ImportLimits:
    max_package_bytes: int = MAX_PACKAGE_BYTES
    max_read: int = 64 * 1024
    max_members: int = 100_000
    max_unpacked_export: int = 2**30
    max_unpacked_report: int = 256 * 2**20
    max_metadata_bytes: int = 4 * 2**20
    max_convert_bytes: int = 32 * 2**20


class ImportCancelledError(Exception):
    """The import was asked to stop; whatever it wrote is the caller's to remove."""


@dataclass(frozen=True)
class Unpacked:
    kind: str
    root: Path
    bundle: Path
    metadata: dict[str, Any]
    manifest: dict[str, Any] | None
    unpacked_bytes: int
    members: int


@dataclass(frozen=True)
class Normalized:
    original_session_key: str | None
    transcript_included: bool
    converted_shells: int
    skipped_shells: int


def _never() -> bool:
    return False


def _check_stop(should_stop: Callable[[], bool]) -> None:
    if should_stop():
        raise ImportCancelledError()


def _walk_into(
    package: BinaryIO,
    dest: Path,
    *,
    max_output: int,
    max_members: int,
    limits: ImportLimits,
    should_stop: Callable[[], bool],
) -> tuple[str, pcheck.ArchiveStats]:
    """Extract one archive layer into ``dest``; return its root name and what the walk spent."""
    root: list[str] = []
    chunk = min(64 * 1024, limits.max_read)

    def visit(member: ArchiveMember, handle: BinaryIO | None) -> None:
        segments = member.segments
        if not root:
            root.append(segments[0])
        elif segments[0] != root[0]:
            raise PackageError(pcheck.CODE_BAD_MEMBER_NAME, f"member outside the archive root: {member.name!r}")
        target = dest.joinpath(*segments)
        try:
            if handle is None:
                target.mkdir(parents=True, exist_ok=True)
                return
            if len(segments) == 1:
                raise PackageError(pcheck.CODE_BAD_MEMBER_NAME, f"the archive root is a file: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as out:
                while piece := handle.read(chunk):
                    out.write(piece)
                    _check_stop(should_stop)
        except FileExistsError:
            # Two names a case-insensitive file system folds together.
            raise PackageError(pcheck.CODE_DUPLICATE_MEMBER, f"member collides with another: {member.name!r}") from None
        except OSError as exc:
            raise PackageError(
                pcheck.CODE_BAD_MEMBER_NAME, f"member cannot be written: {member.name!r}: {exc}"
            ) from None

    try:
        stats = pcheck.walk_archive(
            package,
            max_output=max_output,
            max_read=limits.max_read,
            max_members=max_members,
            visit=visit,
            should_stop=should_stop,
        )
    except pcheck.InspectionStoppedError:
        raise ImportCancelledError() from None
    if not root:
        raise PackageError(CODE_NOT_IMPORTABLE, "the archive is empty")
    return root[0], stats


def _read_json_object(path: Path, limits: ImportLimits) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PackageError(CODE_MANIFEST_INVALID, f"{path.name} cannot be read: {exc}") from None
    if size > limits.max_metadata_bytes:
        raise PackageError(
            CODE_MANIFEST_INVALID, f"{path.name} is {size} bytes; the limit is {limits.max_metadata_bytes}"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise PackageError(CODE_MANIFEST_INVALID, f"{path.name} is not UTF-8 JSON: {exc}") from None
    if not isinstance(value, dict):
        raise PackageError(CODE_MANIFEST_INVALID, f"{path.name} is not a JSON object")
    return value


def _is_version(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


def _check_bundle(bundle: Path, limits: ImportLimits) -> dict[str, Any]:
    manifest = _read_json_object(bundle / BUNDLE_MANIFEST, limits)
    if not _is_version(manifest.get("format_version"), BUNDLE_FORMAT_VERSION):
        raise PackageError(
            CODE_UNSUPPORTED_VERSION,
            f"bundle format_version {manifest.get('format_version')!r} is not {BUNDLE_FORMAT_VERSION}",
        )
    if not (bundle / SPANS_FILE).is_file():
        raise PackageError(CODE_MANIFEST_INVALID, f"the bundle has no {SPANS_FILE}")
    return manifest


def _export_manifest(root: Path, limits: ImportLimits) -> dict[str, Any] | None:
    path = root / EXPORT_MANIFEST
    if not path.is_file():
        return None
    manifest = _read_json_object(path, limits)
    if manifest.get("schema") != EXPORT_SCHEMA:
        return None
    if not _is_version(manifest.get("format_version"), EXPORT_FORMAT_VERSION):
        raise PackageError(
            CODE_UNSUPPORTED_VERSION,
            f"export format_version {manifest.get('format_version')!r} is not {EXPORT_FORMAT_VERSION}",
        )
    export_id = manifest.get("export_id")
    if not isinstance(export_id, str) or export_id != root.name or not EXPORT_ID.fullmatch(export_id):
        raise PackageError(CODE_MANIFEST_INVALID, f"export_id {export_id!r} does not name the archive root")
    if not (root / SPANS_FILE).is_file():
        raise PackageError(CODE_MANIFEST_INVALID, f"the export has no {SPANS_FILE}")
    return manifest


def unpack(
    package: Path,
    dest: Path,
    *,
    limits: ImportLimits | None = None,
    should_stop: Callable[[], bool] = _never,
) -> Unpacked:
    """Extract ``package`` into ``dest`` (which must exist) and tell what it is, or raise :class:`PackageError`."""
    limits = limits or ImportLimits()
    size = package.stat().st_size
    if size == 0:
        raise PackageError(pcheck.CODE_NOT_GZIP_TAR, "empty file")
    if size > limits.max_package_bytes:
        raise PackageError(pcheck.CODE_TOO_LARGE, f"package is {size} bytes; the limit is {limits.max_package_bytes}")
    with package.open("rb") as raw:
        root_name, outer = _walk_into(
            raw,
            dest,
            max_output=limits.max_unpacked_export,
            max_members=limits.max_members,
            limits=limits,
            should_stop=should_stop,
        )
    root = dest / root_name
    unpacked_bytes, members = outer.unpacked_bytes, outer.members

    export = _export_manifest(root, limits)
    if export is not None:
        return Unpacked(KIND_EXPORT, root, root, export, None, unpacked_bytes, members)

    if (root / REPORT_METADATA).is_file():
        try:
            summary = pcheck.inspect_package(
                package, limits=PackageLimits(max_package_bytes=limits.max_package_bytes), should_stop=should_stop
            )
        except pcheck.InspectionStoppedError:
            raise ImportCancelledError() from None
        trajectory = root / "trajectory"
        inner_tars = sorted(p for p in trajectory.glob("*.tar.gz") if p.is_file()) if trajectory.is_dir() else []
        if len(inner_tars) != 1:
            raise PackageError(
                CODE_NOT_IMPORTABLE, f"a bug report package embeds one trajectory/*.tar.gz, found {len(inner_tars)}"
            )
        room = limits.max_unpacked_export - unpacked_bytes
        if room <= 0:
            raise PackageError(
                pcheck.CODE_UNPACKED_TOO_LARGE, "nothing is left of the unpack budget for the trajectory"
            )
        slots = limits.max_members - members
        if slots <= 0:
            raise PackageError(pcheck.CODE_TOO_MANY_MEMBERS, "nothing is left of the member budget for the trajectory")
        with inner_tars[0].open("rb") as raw:
            inner_name, inner = _walk_into(
                raw, trajectory, max_output=room, max_members=slots, limits=limits, should_stop=should_stop
            )
        inner_tars[0].unlink()
        unpacked_bytes += inner.unpacked_bytes
        members += inner.members
        bundle = trajectory / inner_name
        manifest = _check_bundle(bundle, limits)
        kind, metadata = KIND_BUGREPORT, summary.metadata
    elif (root / BUNDLE_MANIFEST).is_file() and (root / REDACTION_FILE).is_file():
        bundle = root
        manifest = _check_bundle(bundle, limits)
        kind, metadata = KIND_REPORT, manifest
    else:
        raise PackageError(CODE_NOT_IMPORTABLE, "not a trajectory export, bug report package or trajectory report")

    if unpacked_bytes > limits.max_unpacked_report:
        raise PackageError(
            pcheck.CODE_UNPACKED_TOO_LARGE,
            f"the package expands to {unpacked_bytes} bytes, past the {limits.max_unpacked_report}-byte report limit",
        )
    return Unpacked(kind, root, bundle, metadata, manifest, unpacked_bytes, members)


# ── normalization ──────────────────────────────────────────────────────


def _artifact_rel(value: Any, artifacts: Path) -> str | None:
    """The store-relative path a package pointer maps to, or None when it maps to no packed file."""
    if not isinstance(value, str) or not value.startswith(f"{ARTIFACTS_DIR}/"):
        return None
    rel = value[len(ARTIFACTS_DIR) + 1 :]
    if not pcheck._safe_relative(rel) or _reserved(rel):
        return None
    candidate = artifacts.joinpath(*rel.split("/"))
    return rel if candidate.is_file() and not candidate.is_symlink() else None


def _reserved(rel: str) -> bool:
    """Whether ``rel`` would land in the message store or the blob store.

    Folded, because a case-insensitive file system makes ``_Messages`` the
    same directory as ``_messages``.
    """
    return rel.split("/", 1)[0].casefold() in _RESERVED_ARTIFACT_DIRS


def _collision(dst: Path) -> PackageError:
    return PackageError(pcheck.CODE_DUPLICATE_MEMBER, f"two package files land on one store path: {dst.name!r}")


def _place(src: Path, dst: Path) -> None:
    """Link ``src`` to ``dst``, or copy it where a link cannot be made; never onto a file already there.

    The link shares its inode with the unpacked original, so writing over an
    existing target -- which a case-insensitive file system can make of two
    differently spelled names -- would rewrite the package itself.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return
    except FileExistsError:
        raise _collision(dst) from None
    except OSError:
        pass
    try:
        with src.open("rb") as source, dst.open("xb") as target:
            shutil.copyfileobj(source, target)
    except FileExistsError:
        raise _collision(dst) from None


def _write_new(dst: Path, text: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dst.open("x", encoding="utf-8") as out:
            out.write(text)
    except FileExistsError:
        raise _collision(dst) from None


def shell_from_v1(payload: Any, store: TraceStore) -> dict[str, Any] | None:
    """A v1 model-input payload split back into a v2 shell, or None when it is not one.

    The aliases follow the Artifact Shell envelope -- ``systemPrompt`` names the
    first system message, ``prompt`` the last user message -- but only where the
    v1 text is exactly what that message resolves to, so resolving the shell
    hands back the payload it came from. A v1 record written under the older
    "first non-empty system message" rule keeps its text inline.
    """
    if not isinstance(payload, dict) or "artifactFormat" in payload:
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    refs = store.address_items(messages)

    def alias(field: str, role: str, order: range) -> Any:
        original = payload[field]
        for index in order:
            message = messages[index]
            if isinstance(message, dict) and message.get("role") == role:
                ref = refs[index]
                if (
                    artifact_v2.ref_sha1(ref) is not None
                    and artifact_v2.coerce_text(message.get("content")) == original
                ):
                    return ref
                return original
        return original

    shell: dict[str, Any] = {"artifactFormat": artifact_v2.ARTIFACT_FORMAT}
    for key, value in payload.items():
        if key == "messages":
            shell[key] = refs
        elif key == "systemPrompt":
            shell[key] = alias(key, "system", range(len(messages)))
        elif key == "prompt":
            shell[key] = alias(key, "user", range(len(messages) - 1, -1, -1))
        else:
            shell[key] = value
    return shell


def _rewrite_spans(
    source: Path,
    target: Path,
    artifacts: Path,
    convert: set[str],
    *,
    collect_inputs: bool,
    should_stop: Callable[[], bool],
) -> str | None:
    """Copy the span log, re-pointing artifact pointers; return the first session key seen.

    A line the index would skip as oversized is copied through in pieces
    rather than parsed, so one enormous line never sits in memory whole.
    """
    first_key: str | None = None
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, target.open("wb") as out:
        while raw := src.readline(MAX_LINE_BYTES + 1):
            if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                out.write(raw)
                while (rest := src.readline(MAX_LINE_BYTES)) and not rest.endswith(b"\n"):
                    out.write(rest)
                    _check_stop(should_stop)
                out.write(rest if rest else b"\n")
                _check_stop(should_stop)
                continue
            try:
                record = json.loads(raw)
            except (ValueError, RecursionError):
                record = None
            if not isinstance(record, dict):
                if raw.strip():
                    out.write(raw if raw.endswith(b"\n") else raw + b"\n")
                _check_stop(should_stop)
                continue
            attrs = record.get("attributes")
            if isinstance(attrs, dict):
                if first_key is None and isinstance(attrs.get("session.key"), str) and attrs["session.key"]:
                    first_key = attrs["session.key"]
                for key in [k for k in attrs if isinstance(k, str) and k.endswith(_POINTER_SUFFIX)]:
                    rel = _artifact_rel(attrs[key], artifacts)
                    attrs[key] = f"{ARTIFACT_ROOT}/{rel}" if rel is not None else None
                    if rel is not None and collect_inputs and key == _LLM_INPUT_POINTER:
                        convert.add(rel)
            out.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            _check_stop(should_stop)
    return first_key


def _place_artifacts(
    artifacts: Path,
    store_root: Path,
    convert: set[str],
    store: TraceStore,
    limits: ImportLimits,
    should_stop: Callable[[], bool],
) -> tuple[int, int]:
    converted = skipped = 0
    if not artifacts.is_dir():
        return converted, skipped
    for dirpath, _dirnames, filenames in os.walk(artifacts):
        for name in sorted(filenames):
            src = Path(dirpath) / name
            rel = src.relative_to(artifacts).as_posix()
            if _reserved(rel):
                raise PackageError(pcheck.CODE_BAD_MEMBER_NAME, f"artifact collides with the message store: {rel!r}")
            dst = store_root.joinpath(*rel.split("/"))
            if rel in convert:
                if src.stat().st_size > limits.max_convert_bytes:
                    skipped += 1
                else:
                    try:
                        payload = json.loads(src.read_text(encoding="utf-8"))
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        payload = None
                    shell = shell_from_v1(payload, store)
                    if shell is not None:
                        _write_new(dst, json.dumps(shell, ensure_ascii=False, indent=2, default=str))
                        converted += 1
                        _check_stop(should_stop)
                        continue
            _place(src, dst)
            _check_stop(should_stop)
    return converted, skipped


def _place_blobs(messages: Path, store_root: Path, should_stop: Callable[[], bool]) -> None:
    if not messages.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(messages):
        for name in sorted(filenames):
            src = Path(dirpath) / name
            rel = src.relative_to(messages).as_posix()
            match = _BLOB_NAME.fullmatch(rel)
            if match is None or match.group(2)[:2] != match.group(1):
                raise PackageError(pcheck.CODE_BAD_MEMBER_NAME, f"not a message blob name: {rel!r}")
            _place(src, artifact_v2.message_path(store_root, match.group(2)))
            _check_stop(should_stop)


def normalize(
    unpacked: Unpacked,
    staging: Path,
    *,
    limits: ImportLimits | None = None,
    should_stop: Callable[[], bool] = _never,
) -> Normalized:
    """Lay ``unpacked`` out under ``staging`` as a trace store plus the session record."""
    limits = limits or ImportLimits()
    bundle = unpacked.bundle
    state = staging / STATE_DIR
    store_root = state / ARTIFACT_ROOT
    store_root.mkdir(parents=True, exist_ok=True)
    artifacts = bundle / ARTIFACTS_DIR
    convert: set[str] = set()
    first_key = _rewrite_spans(
        bundle / SPANS_FILE,
        state / SPAN_LOG,
        artifacts,
        convert,
        collect_inputs=unpacked.kind != KIND_EXPORT,
        should_stop=should_stop,
    )
    converted, skipped = _place_artifacts(artifacts, store_root, convert, TraceStore(state), limits, should_stop)
    if unpacked.kind == KIND_EXPORT:
        _place_blobs(bundle / MESSAGES_DIR, store_root, should_stop)

    transcript = bundle / SESSION_FILE
    included = transcript.is_file() and not transcript.is_symlink()
    if included:
        shutil.copyfile(transcript, staging / SESSION_FILE)

    if unpacked.kind == KIND_EXPORT:
        source = unpacked.metadata.get("source")
        declared = source.get("session_key") if isinstance(source, dict) else None
    else:
        declared = (unpacked.manifest or {}).get("session_key")
    original = declared if isinstance(declared, str) and declared else first_key
    return Normalized(
        original_session_key=original,
        transcript_included=included,
        converted_shells=converted,
        skipped_shells=skipped,
    )


__all__ = [
    "CODE_BAD_ENCODING",
    "CODE_MANIFEST_INVALID",
    "CODE_NOT_IMPORTABLE",
    "CODE_UNSUPPORTED_VERSION",
    "KIND_BUGREPORT",
    "KIND_EXPORT",
    "KIND_REPORT",
    "MAX_PACKAGE_BYTES",
    "ImportCancelledError",
    "ImportLimits",
    "Normalized",
    "Unpacked",
    "normalize",
    "shell_from_v1",
    "unpack",
]
