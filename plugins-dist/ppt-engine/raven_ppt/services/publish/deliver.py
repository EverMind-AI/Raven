from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from raven_ppt.contracts import Finding, Severity


class PublishRefusedError(RuntimeError):
    """Delivery declined. The message is addressed to the model."""


@dataclass(frozen=True)
class Staged:
    """A deck that has been measured, held where nothing else can rewrite it.

    The digest is taken here and checked again at delivery. Between the two
    moments the deck is measured, reviewed, and possibly redesigned and rebuilt;
    the check is what makes "this file is the one the findings describe" a fact
    rather than an assumption.

    On disk rather than in memory. The predecessor held the whole payload in a
    dict on the state store for the life of the process -- tens of megabytes per
    project once figures are placed, and one entry per project ever staged.
    """

    path: Path
    digest: str
    pages: int = 0


def stage(project, built: Path, *, pages: int = 0) -> Staged:
    """Take custody of a freshly built deck and fingerprint it."""
    if not built.is_file():
        raise PublishRefusedError(f"there is no deck at {built} to publish")
    staging_dir = project.state_dir / "staging"
    if staging_dir.is_symlink():
        raise PublishRefusedError("the staging directory must not be a symlink")
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / "candidate.pptx"
    # Copied, not moved: the build directory's copy is what a failed edit gets
    # repaired against, and a deck that reaches staging must not be the only one.
    shutil.copy2(built, staged)
    payload = staged.read_bytes()
    if not payload:
        raise PublishRefusedError("the built deck is empty")
    return Staged(path=staged, digest=hashlib.sha256(payload).hexdigest(), pages=pages)


def publish(
    project, staged: Staged, destination: Path, *, findings: Sequence[Finding], blocking_kinds: frozenset[str]
) -> Path:
    """Deliver the staged deck, or refuse and say what stands in the way.

    `findings` and `blocking_kinds` are required arguments rather than a check
    the caller performs first. Fail-closed that depends on being remembered is
    not fail-closed: the one route that forgot it shipped a deck with seventeen
    colour bars and an unanchored number in it.
    """
    refusals = [f for f in findings if f.severity is Severity.BLOCKING or f.kind in blocking_kinds]
    if refusals:
        raise PublishRefusedError(_refusal(refusals))

    resolved = _inside_workspace(project, destination)
    if not staged.path.is_file():
        raise PublishRefusedError("the staged deck is gone; build it again")
    current = hashlib.sha256(staged.path.read_bytes()).hexdigest()
    if not hmac.compare_digest(current, staged.digest):
        # The deck changed after it was measured, so the findings describe a
        # file that no longer exists and say nothing about this one.
        raise PublishRefusedError("the deck changed after it was checked; build it again so the checks describe it")

    resolved.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: a reader either sees the previous deck or the new one, never a
    # half-written file. A deck is opened by a human, often while it is being
    # rebuilt.
    temporary = resolved.parent / f".{resolved.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(staged.path, temporary)
        os.replace(temporary, resolved)
    except OSError as exc:
        raise PublishRefusedError(f"could not write the deck to {resolved}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    record_published(project, resolved, staged.digest, staged.pages)
    return resolved


PUBLISHED_RECORD = "published.json"
REFUSED_RECORD = "refused.json"


def record_published(project, path: Path, digest: str, pages: int) -> None:
    """Note what this route published, so the harness can tell it from a copy.

    Two live runs answered a refused build by `cp deck/build/deck.pptx out/...` and
    told the user the deck was delivered; the hook, which only knew "a valid deck under
    out/ newer than the turn started", confirmed it. What was published is what went
    through the gates, and this is the list of that.
    """
    import json

    target = project.state_dir / PUBLISHED_RECORD
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        held = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    except (OSError, ValueError):
        held = {}
    entries = [e for e in held.get("published", []) if isinstance(e, dict) and e.get("path") != str(path)]
    entries.append({"path": str(path), "sha256": digest, "pages": pages})
    target.write_text(json.dumps({"published": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    # What was refused before this publish no longer stands in anyone's way.
    (project.state_dir / REFUSED_RECORD).unlink(missing_ok=True)


def published_original(state_dir: Path, copy: Path) -> Path | None:
    """The published deck ``copy`` is a byte-for-byte copy of, or None.

    A model that wants the deliverable under a title of its own copies the
    published deck to that name; the copy passes the digest check, but its
    preview sat beside the original, under the original's stem.
    """
    import hashlib
    import json

    try:
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
        held = json.loads((state_dir / PUBLISHED_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in held.get("published", []):
        if not isinstance(entry, dict) or entry.get("sha256") != digest:
            continue
        original = Path(str(entry.get("path") or ""))
        if original.name and original.resolve() != copy.resolve() and original.is_file():
            return original
    return None


def published_digests(state_dir: Path) -> set[str]:
    """The sha256 of every deck this project has published; empty when none has been."""
    import json

    target = state_dir / PUBLISHED_RECORD
    try:
        held = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(e.get("sha256")) for e in held.get("published", []) if isinstance(e, dict) and e.get("sha256")}


def record_refused(project, note: str, blocking: Sequence[Finding]) -> None:
    """Note why this build was not published, for the hook to put back in front of the model.

    The reason reaches the model once, in the build tool's reply. Two live runs
    answered it with a `cp` of the refused build to a name of their own under `out/`
    and a reply that the deck was delivered; the hook that sends such a reply back has
    only the directory to go on, and the stage result the reason was in is gone by
    then. Kept until the next publish, which clears it.
    """
    import json

    target = project.state_dir / REFUSED_RECORD
    payload = {
        "note": note,
        "blocking": [{"page": f.page, "kind": f.kind, "message": f.message} for f in blocking],
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return


def last_refusal(state_dir: Path) -> str | None:
    """Why the last build was refused, as one passage; None once a build was published since."""
    import json

    try:
        held = json.loads((state_dir / REFUSED_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(held, dict):
        return None
    note = str(held.get("note") or "").strip().rstrip(".")
    listed: list[str] = []
    for row in held.get("blocking", []):
        if not isinstance(row, dict):
            continue
        where = f"page {row['page']}: " if row.get("page") is not None else ""
        listed.append(f"{where}{row.get('kind', '')} -- {row.get('message', '')}")
    if not note and not listed:
        return None
    if not listed:
        return note
    shown = "; ".join(listed[:8]) + (f" (and {len(listed) - 8} more)" if len(listed) > 8 else "")
    return f"{note}. Standing: {shown}" if note else f"Standing: {shown}"


def _inside_workspace(project, destination: Path) -> Path:
    """Where the deck may land: under the workspace, and nowhere else.

    Resolved first, so a relative walk out of the workspace is caught rather
    than described.
    """
    workspace = project.workspace.resolve()
    candidate = (workspace / destination).resolve() if not destination.is_absolute() else destination.resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise PublishRefusedError(f"{destination} is outside the workspace; deliver to a path under {workspace}")
    if candidate.is_dir():
        raise PublishRefusedError(f"{destination} is a directory; name the .pptx file to write")
    if candidate.suffix.lower() != ".pptx":
        raise PublishRefusedError(f"{destination} is not a .pptx path")
    return candidate


def _refusal(findings: Sequence[Finding]) -> str:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    listed = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
    first = findings[0]
    where = f" (page {first.page})" if first.page is not None else ""
    return f"not delivered while these stand: {listed}. First: {first.message}{where}"
