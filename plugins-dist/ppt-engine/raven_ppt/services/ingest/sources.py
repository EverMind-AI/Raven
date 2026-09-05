"""The one place a deck's sources live, and how each of them got there.

Sources reach a deck four ways -- a directory the user already had, a file they
attached, something fetched from the web, and the request text itself when it
carries the substance -- and they reach it over time: prepare reads what is there,
the outline finds a gap, a fetch fills it, the outline is revised, another fetch.
The materials set accumulates.

Nothing owned it, and four failures came out of that one absence. `ingest_materials`
reads one directory and overwrites its three artefacts, so two directories could not
both be evidence and whoever ingested last won: twice in live runs the source the
*user supplied* silently left the deck's evidence while sitting on disk. Every
re-ingest re-read everything, so one run parsed the same two PDFs twice. And nothing
wrote the manifest `documents.source_urls` reads, so the attribution it calls "the
only attribution a web asset has" was empty in every real run -- every fetched figure
reached a slide with no source.

The manifest is the one `documents.source_urls` already reads, in the shape it
already parses, at the path it already looks in -- including its hash check, which
fails the ingest rather than attributing a figure to a file that has changed since it
was fetched. Writing a second manifest of my own would have left two, one of them
still empty.

So the deck owns a set of source *files*, not a directory. Everything that brings a
source in copies it here and records where it came from; the ingest reads here and
nowhere else. Copying rather than referencing costs disk and buys three things: the
project is self-contained, so it can be moved or archived with its evidence intact;
it is already what attachments and templates do; and a user's file that moves or is
deleted cannot leave a deck citing something that is gone. What keeps their directory
authoritative is that mirroring runs again on every prepare -- including removals.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from raven_ppt.services.ingest import documents

SOURCES_DIR = "sources"

# What a mirrored entry's origin looks like. Prefixed rather than a separate field
# so one string answers both questions a sync asks: is this mine to remove, and
# which directory does it belong to.
MIRROR = "mirror:"
FETCH = "fetch:"
ATTACHED = "attached"
REQUEST = "request"


def sources_dir(project) -> Path:
    return project.root / SOURCES_DIR


def manifest_path(project) -> Path:
    """Where provenance is recorded: the file `documents.source_urls` reads."""
    from raven_ppt.services.ingest.pipeline import MANIFEST_FILE

    return project.ingest_dir / MANIFEST_FILE


@dataclass(frozen=True)
class Source:
    """One file in the deck's source set, and where it came from."""

    path: Path
    origin: str
    """`mirror:<dir>` for a file from a directory the user has, `fetch:<url>` for a
    download, `attached` for a file they sent, `request` for the request text."""
    digest: str
    caption: str = ""
    """The source's own words about a fetched picture, when whatever fetched it was
    given them. A picture off a web page has no caption of its own to read -- the
    words sit in the HTML beside it, not in the bytes -- so they are recorded here at
    fetch time or nowhere at all."""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def url(self) -> str:
        return self.origin[len(FETCH) :] if self.origin.startswith(FETCH) else ""

    def as_dict(self) -> dict[str, str]:
        """The record shape `documents.source_urls` parses, plus the origin.

        `path` absolute and `content_sha256` full, because that is what its hash
        check compares; `url` is the key it reads attribution from.
        """
        recorded = {"path": str(self.path), "content_sha256": self.digest, "origin": self.origin}
        if self.url:
            recorded["url"] = self.url
        if self.caption:
            recorded["caption"] = self.caption
        return recorded


def held(project) -> tuple[Source, ...]:
    """Every source this deck holds, as recorded."""
    return tuple(
        Source(
            path=Path(str(e["path"])),
            origin=str(e.get("origin") or ""),
            digest=str(e.get("content_sha256") or ""),
            caption=str(e.get("caption") or ""),
        )
        for e in _read_manifest(project)
        if e.get("path")
    )


def take(project, path: Path, origin: str, *, name: str | None = None) -> Source | None:
    """Copy one file into the deck's sources and record where it came from.

    Returns None for something this cannot read, so a caller can say so rather than
    leaving a file in the set that the ingest will skip in silence.
    """
    if not path.is_file() or path.suffix.lower() not in documents.SUPPORTED_SUFFIXES:
        return None
    folder = sources_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (name or path.name)
    if path.resolve() != target.resolve():
        shutil.copy2(path, target)
    entry = Source(path=target, origin=_kept(project, target, origin), digest=_digest(target))
    _record(project, entry)
    return entry


def write_source(project, name: str, text: str, origin: str) -> Source:
    """Put text into the deck's sources -- the request itself, or a transcript."""
    folder = sources_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    entry = Source(path=folder / name, origin=origin, digest=_digest(folder / name))
    _record(project, entry)
    return entry


def receive(project, name: str, payload: bytes, origin: str, *, caption: str | None = None) -> Source | None:
    """Put downloaded bytes into the deck's sources, or None if nothing can read them.

    Bytes rather than a path because a fetch holds the body and not a file, and
    writing it somewhere else first is how a second pile starts.

    ``caption`` is the source's own words about a picture, which only the fetch ever
    holds; recorded here so the ingest can put them in the figure catalogue.
    """
    if Path(name).suffix.lower() not in documents.SUPPORTED_SUFFIXES:
        return None
    folder = sources_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(payload)
    entry = Source(
        path=folder / name,
        origin=origin,
        digest=_digest(folder / name),
        caption=_kept_caption(project, folder / name, caption),
    )
    _record(project, entry)
    return entry


def _kept(project, target: Path, origin: str) -> str:
    """The origin to record: the first one, when the bytes have not changed.

    Provenance is where these bytes came from, and that does not change because
    something walked past the file again. Without this, one `ppt_ingest` pointed at
    the deck's own sources rewrote a fetched paper's origin to `mirror:sources` and
    took its URL with it -- caught in a live run, one call after the attribution
    started working at all.
    """
    prior = _prior(project, target)
    return prior.origin if prior is not None and prior.origin else origin


def _kept_caption(project, target: Path, caption: str | None) -> str:
    """The caption to record: the one already held, when this fetch brought none.

    Same reasoning as :func:`_kept`, and the same failure it prevents: fetching a URL
    a second time -- a retry, or the same picture wanted for a second page -- is not
    new information about these bytes, so it must not blank the words the first fetch
    carried in. A caption passed now does win, because that one *is* new information.
    """
    if caption:
        return caption
    prior = _prior(project, target)
    return prior.caption if prior is not None else ""


def _prior(project, target: Path) -> Source | None:
    """What the manifest already records about these exact bytes, if anything."""
    digest = _digest(target)
    for source in held(project):
        if source.path.resolve() == target.resolve() and source.digest == digest:
            return source
    return None


@dataclass(frozen=True)
class Mirrored:
    """What bringing a user's directory into the source set did.

    `templates` is separate from `left_behind` because the two need opposite
    sentences. A `.pptx` in a folder of materials is almost always the house style
    -- the commonest way anyone hands one over is "the paper and the template are
    both in here" -- and the first version of this reported it as a file nothing
    could read, which is both wrong and the exact opposite of the next move.
    """

    taken: int = 0
    dropped: int = 0
    left_behind: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()


def mirror(project, directory: Path) -> Mirrored:
    """Bring a user's directory into the deck's sources, and keep it in step.

    Additions and updates are copied; a file that has left their directory leaves the
    set too, because a deck citing a source its owner deleted is worse than a deck
    missing it. Only this directory's own mirrored entries are touched -- what was
    fetched, attached or typed is not theirs to remove.

    `left_behind` names what nothing here can read. An attachment this cannot read
    says so in a sentence; a mirrored directory said nothing at all, so a document in
    a format we do not handle was simply absent from the deck's evidence and its
    owner never found out. Naming it is the whole of the guarantee: the material may
    be unreadable, but it is never lost quietly.
    """
    if directory.resolve() == sources_dir(project).resolve():
        # Mirroring the set into itself is a no-op that only churns the manifest, and
        # a model does ask for it: `ppt_ingest` pointed at the deck's own sources is
        # how the origin rewrite above was found.
        return Mirrored()
    origin = f"{MIRROR}{directory.name}"
    found = {path.name: path for path in documents.discover(directory)}
    taken = 0
    for name, path in sorted(found.items()):
        if take(project, path, origin, name=name) is not None:
            taken += 1
    dropped = 0
    for source in held(project):
        if source.origin == origin and source.name not in found:
            source.path.unlink(missing_ok=True)
            _forget(project, source.path)
            dropped += 1
    unreadable = documents.unreadable(directory)
    return Mirrored(
        taken=taken,
        dropped=dropped,
        left_behind=tuple(path.name for path in unreadable if path.suffix.lower() != ".pptx"),
        templates=tuple(path.name for path in unreadable if path.suffix.lower() == ".pptx"),
    )


def fetched(project) -> tuple[Source, ...]:
    """The sources that came off the web, which are the ones needing attribution."""
    return tuple(source for source in held(project) if source.url)


def _record(project, entry: Source) -> None:
    lines = [e for e in _read_manifest(project) if e.get("path") != str(entry.path)]
    lines.append(entry.as_dict())
    _write_manifest(project, lines)


def _forget(project, path: Path) -> None:
    _write_manifest(project, [e for e in _read_manifest(project) if e.get("path") != str(path)])


def _read_manifest(project) -> list[dict]:
    path = manifest_path(project)
    if not path.is_file():
        return []
    found: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            found.append(entry)
    return found


def _write_manifest(project, lines: list[dict]) -> None:
    path = manifest_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in sorted(lines, key=lambda e: e["path"]))
    path.write_text(body, encoding="utf-8")


def _digest(path: Path) -> str:
    """The full digest, because the manifest's reader compares it to `sha256_file`."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
