"""A PDF rendering of a slide deck, for the front end's file viewer.

The page can frame a PDF and cannot draw a ``.pptx``, so a click on a deck asks
the file route for its PDF instead. LibreOffice does the conversion, through
``raven.utils.office``, which the ppt engine calls as well.

This module used to spawn LibreOffice itself, on the grounds that the host must
not import a plugin. The premise is true and the conclusion was not: the utility
sits under ``raven/``, so both callers import inwards, and the engine has been
importing ``raven.utils`` from four of its tools for a long time. What the copy
actually bought was two implementations of one verb, identical argv and all,
which had diverged within a day of the second one landing -- this one knew how to
kill a hung LibreOffice on Windows and the engine's did not. What is this
module's own stays here: the order below, the cache, and the three status codes
the page reads.

What comes back is decided in this order:

1. The deck's own published PDF: ``<stem>.pdf`` beside it and not older than
   it. The engine writes one under ``out/`` next to every deck it delivers, so
   the common case never starts LibreOffice at all.
2. The cache under raven's state directory, keyed by the deck's resolved path,
   size and mtime. A re-click is a file read; a rebuilt deck misses and
   renders again; old entries are swept when a new one is written.
3. A conversion, one at a time per key: a double click waits for the first
   click's render rather than starting a second LibreOffice against the same
   file, which with a shared profile would silently lose one of the two.

LibreOffice's exit code says nothing about whether a PDF was written, and its
stderr carries a ``javaldx`` complaint on every stock container, so the only
check is whether the file appeared where it was asked for.

A cache entry's mtime is when it was last served, not when it was written: the
sweep reads that, so a rendering still in use is not the one deleted. Written-at
would have swept a file this route had just handed to a response that had not
opened it yet -- lost on any platform, and on Windows lost while a handle was
open, which is the failure this whole convergence is about.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

from raven.utils import office

RENDERABLE_SUFFIXES = frozenset({".pptx"})

# A deck of forty image-heavy pages converts in about half a minute on a cold
# profile; three minutes is a hang, not a slow deck.
CONVERT_TIMEOUT_S = 180.0

# A rendering nobody has opened for a week is cheaper to redo than to keep.
CACHE_TTL_S = 7 * 24 * 3600

_LOG_TAIL_CHARS = 400

_locks: dict[str, asyncio.Lock] = {}


class PdfPreviewError(Exception):
    """LibreOffice ran, or could not run, and there is no PDF to show."""


class PdfPreviewUnavailableError(PdfPreviewError):
    """No LibreOffice on this host."""


class PdfPreviewTimeoutError(PdfPreviewError):
    """LibreOffice was still running when its budget ran out."""


def is_renderable(path: Path) -> bool:
    return path.suffix.lower() in RENDERABLE_SUFFIXES


def cache_dir() -> Path:
    from raven.config.paths import get_cache_dir

    return get_cache_dir() / "pdf-preview"


def find_soffice() -> str | None:
    """LibreOffice's launcher, asked of the one module that owns the question."""
    return office.find_soffice()


def published_pdf(source: Path, workspace: Path | None = None) -> Path | None:
    """The PDF the deck's author already published beside it, if it is current.

    Through the viewer's own fence, not past it. ``source`` arrives resolved and
    allowed, but this sibling is a second path the page never asked for: a deck
    whose neighbour is a symlink into raven's state directory would otherwise be
    the route by which that file is served. ``resolve_readable`` is the same
    check the viewer makes -- symlinks resolved, the state directory refused,
    ``restrict_to_workspace`` honoured -- so the shortcut cannot reach further
    than the request that opened it. A sibling the fence refuses is not an error
    here: the conversion below writes into the cache this route owns, which is
    where a deck outside the fence's reach gets its preview from anyway.

    ``workspace`` is the session's own root, the one the deck was admitted
    against, and it has to be carried here rather than left to the configured
    default: with ``restrict_to_workspace`` on and a session rooted elsewhere the
    default fence is a different fence. It let a ``deck.pdf`` in the session's
    workspace symlink to a PDF in the configured one -- a file that same session
    may not request directly -- and it refused an ordinary sibling in the
    session's own root, paying for a render of what was already there.
    """
    from raven.rpc.files import resolve_readable

    sibling = source.with_suffix(".pdf")
    try:
        allowed = resolve_readable(str(sibling), workspace=workspace)
    except (ValueError, OSError, PermissionError):
        return None
    try:
        if allowed.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return allowed
    except OSError:
        return None
    return None


def cache_key(source: Path) -> str:
    st = source.stat()
    digest = hashlib.sha256(f"{source}\0{st.st_size}\0{st.st_mtime_ns}".encode()).hexdigest()
    return digest[:32]


async def pdf_for(source: Path, *, timeout_s: float | None = None, workspace: Path | None = None) -> Path:
    """The PDF to serve for ``source``, rendering it if nothing current exists.

    ``source`` must already have passed the viewer's path policy; nothing here
    re-checks it, and the PDF returned may sit in the state directory the
    policy refuses, which is fine because the caller serves it by handle, not
    by a path the page chose. ``workspace`` is the root ``source`` was admitted
    against, and the sibling shortcut is checked against that same one.

    Raises:
        PdfPreviewUnavailableError: LibreOffice is not installed.
        PdfPreviewTimeoutError: the conversion outran ``timeout_s``.
        PdfPreviewError: LibreOffice ran and produced no PDF.
    """
    published = published_pdf(source, workspace)
    if published is not None:
        return published
    budget = CONVERT_TIMEOUT_S if timeout_s is None else timeout_s
    key = cache_key(source)
    cached = cache_dir() / f"{key}.pdf"
    if cached.is_file():
        return _touched(cached)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if cached.is_file():
            return _touched(cached)
        await asyncio.to_thread(_render, source, cached, budget)
    return cached


def _render(source: Path, target: Path, timeout_s: float) -> None:
    executable = find_soffice()
    if executable is None:
        raise PdfPreviewUnavailableError(
            "LibreOffice is not installed on the gateway host, so a deck cannot be shown as a PDF. "
            "Install it with: " + office.install_hint()
        )
    root = cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    # Under the cache root rather than the system temp: the finished PDF is
    # renamed into place, and a rename across filesystems is not a rename.
    scratch = Path(tempfile.mkdtemp(prefix="render-", dir=root))
    try:
        staged = scratch / "out"
        staged.mkdir()
        try:
            done = office.to_pdf(source, staged, executable=executable, timeout_s=timeout_s, profile_root=scratch)
        except TimeoutError as exc:
            raise PdfPreviewTimeoutError(
                f"LibreOffice took longer than {timeout_s:g}s to render the deck and was stopped"
            ) from exc
        except FileNotFoundError as exc:
            raise PdfPreviewUnavailableError(
                f"LibreOffice could not be started: {executable!r} is not executable"
            ) from exc
        except OSError as exc:
            raise PdfPreviewError(f"LibreOffice could not be started: {exc}") from exc
        if len(done.produced) != 1 or not done.produced[0].is_file():
            detail = (done.stderr.strip() or done.stdout.strip())[-_LOG_TAIL_CHARS:]
            raise PdfPreviewError(
                f"LibreOffice did not produce a PDF for {source.name}" + (f": {detail}" if detail else "")
            )
        _sweep(root)
        os.replace(done.produced[0], target)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _touched(cached: Path) -> Path:
    """Date the entry by this use, so the sweep never takes one still in service.

    CACHE_TTL_S is written as "a rendering nobody has opened for a week", and the
    sweep reads mtime: without this, mtime is when the file was written, so a
    cached PDF older than the TTL could be handed to a response and deleted by
    the next render before that response opened it.
    """
    try:
        os.utime(cached, None)
    except OSError:
        pass
    return cached


def _sweep(root: Path) -> None:
    cutoff = time.time() - CACHE_TTL_S
    for entry in root.glob("*.pdf"):
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue
