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
    return resolved


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
