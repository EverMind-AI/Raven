"""Taking custody of a built deck, delivering it, and keeping the record true.

The record has one job that is easy to state and was never checked twice: the
sha256 in `published.json` names the bytes under `out/`. It is written from the
digest the gates measured, on every publish, and it was right -- what moved was
the file. A live run's author, holding a delivered deck it wanted one more change
in, ran `python3 - <<EOF  # Apply the same fix to the published deck
(out/deck.pptx) in place` through `exec` at 08:14:18 and rewrote the delivery
where it lay; `deck/build/deck.pptx` and the staged copy both still held the
recorded bytes six minutes later, and the deck the user was handed had been
through no gate at all. So this module now reads the delivered file back rather
than trusting the copy it made of it, and says so when the two disagree.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
    # The bytes that are actually there, not the digest of the copy they came from.
    # The record's whole promise is "this sha names the file at this path", and the
    # only way to keep it is to read the file at that path.
    try:
        delivered = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise PublishRefusedError(f"the deck at {resolved} could not be read back: {exc}") from exc
    if not hmac.compare_digest(delivered, staged.digest):
        raise PublishRefusedError(f"the deck written to {resolved} is not the deck that was measured; build it again")
    record_published(project, resolved, delivered, staged.pages)
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
    target = project.state_dir / PUBLISHED_RECORD
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        held = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    except (OSError, ValueError):
        held = {}
    entries = [e for e in held.get("published", []) if isinstance(e, dict) and e.get("path") != str(path)]
    entries.append({"path": str(path), "sha256": digest, "pages": pages})
    # Atomic, for the reason the deck's own write is: a half-written record is a record
    # a reader believes. Rewritten on every publish rather than the delivery being
    # frozen at the moment it is written -- the tier's cap releases the deck and lets
    # the author keep building, and a crash reprieve adds more such builds, so the
    # record has to follow the file rather than pin it.
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps({"published": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        # The deck is in place, so this is not "nothing was published" -- but a delivery
        # with no record is one the harness reads as a copy the model made for itself,
        # and saying so here is better than that being discovered downstream.
        raise PublishRefusedError(
            f"the deck was written to {path} but its record could not be: {exc}. Build again"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    # What was refused before this publish no longer stands in anyone's way.
    (project.state_dir / REFUSED_RECORD).unlink(missing_ok=True)


def delivery_report(project, destination: Path) -> str | None:
    """Whether the deck already at ``destination`` is still the one this route delivered.

    Asked before the publish that overwrites it, because afterwards there is nothing
    left to compare. It never withholds the deck: the answer to an unmeasured delivery
    is the measured one written over it, and the caller is on its way to do that.
    """
    try:
        resolved = _inside_workspace(project, destination)
    except PublishRefusedError:
        return None
    return tampered_delivery(project.state_dir, resolved)


def tampered_delivery(state_dir: Path, path: Path) -> str | None:
    """Whether the deck recorded at ``path`` still holds the bytes recorded for it.

    None when nothing is recorded for it, when the file is gone, or when it matches; a
    passage naming what happened when it does not. This is the check nothing did. The
    record was written correctly on every publish and then never read back against the
    file, so a delivery edited where it lay stayed the deck the user had while every
    record in the deck folder went on describing the one that passed the gates.

    A report, never a refusal. The answer to an unmeasured delivery is the measured one
    written over it, which is what the caller is about to do.
    """
    recorded = _recorded(state_dir).get(str(path))
    if recorded is None or not path.is_file():
        return None
    try:
        current = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    if hmac.compare_digest(current, recorded):
        return None
    return (
        f"{path} was not the deck this route delivered: its bytes changed after it was published "
        f"(recorded {recorded[:12]}, found {current[:12]}), so whatever was done to it went through no gate "
        "and no reading. This build's deck replaces it -- make the change in the program that draws the "
        "page and build again, never in the delivered file"
    )


def unrecorded_deliveries(state_dir: Path) -> list[str]:
    """Every recorded deck whose file no longer holds the bytes recorded for it.

    The same question `tampered_delivery` asks per publish, for a caller with no publish
    in hand -- the hook at the end of a turn. Empty is the only state a finished run
    should be in: the record, the gates' own `blocking.json` and the file under `out/`
    all describing one set of bytes.
    """
    return [
        report
        for path in sorted(_recorded(state_dir))
        if (report := tampered_delivery(state_dir, Path(path))) is not None
    ]


def _recorded(state_dir: Path) -> dict[str, str]:
    """path -> sha256, as the record holds it."""
    try:
        held = json.loads((state_dir / PUBLISHED_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(entry["path"]): str(entry["sha256"])
        for entry in held.get("published", [])
        if isinstance(entry, dict) and entry.get("path") and entry.get("sha256")
    }


def published_original(state_dir: Path, copy: Path) -> Path | None:
    """The published deck ``copy`` is a byte-for-byte copy of, or None.

    A model that wants the deliverable under a title of its own copies the
    published deck to that name; the copy passes the digest check, but its
    preview sat beside the original, under the original's stem.
    """
    import hashlib

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
