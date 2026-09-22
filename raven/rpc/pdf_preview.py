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
import math
import os
import shutil
import tempfile
import time
from pathlib import Path

from loguru import logger

from raven.utils import office

# What LibreOffice is asked to turn into a PDF. Decks were the first, because
# the page can frame a PDF and cannot draw a .pptx; a knowledge base takes
# uploads in the rest of the office formats and the page cannot draw those
# either. The legacy trio is here deliberately -- .doc and .xls have no reader
# in the browser and no pure-Python one worth trusting, so LibreOffice is not
# one option among several for them, it is the only one.
#
# Not a general "anything LibreOffice opens" list: every suffix here is a
# conversion the gateway will start on a page's say-so, and the timeout below
# is sized for a document rather than for a spreadsheet nobody meant to render.
RENDERABLE_SUFFIXES = frozenset({".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls", ".odt", ".odp", ".ods", ".rtf"})

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


def has_thumb(path: Path) -> bool:
    """Whether a first-page picture can be made of ``path``: every Office source
    LibreOffice renders, and a PDF, which is its own rendering and only needs
    rasterising."""
    return is_renderable(path) or path.suffix.lower() == ".pdf"


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


def sources_dir() -> Path:
    """Where a renderer keeps a suffixed copy of what it was asked to convert.

    Under the cache but not in its root: the root holds ``<key>.pdf`` outputs
    and ``published_pdf`` looks for a sibling PDF beside whatever it is given,
    so a copy sitting next to those would let one document's rendering answer
    for another's.
    """
    return cache_dir() / "sources"


def forget_source(document_id: str) -> None:
    """Drop the retained copy for one document, if there is one.

    Here rather than with the caller that makes it, because this module owns
    the directory: a function that empties part of this cache belongs beside
    the one that fills it, and reaching back the other way puts the two rpc
    modules in an import cycle.

    Matched on the id rather than on a name, because a suffix that has since
    changed would leave a copy behind and the point of this call is that
    nothing is left.
    """
    directory = sources_dir()
    if not directory.is_dir():
        return
    for entry in directory.glob(f"{document_id}.*"):
        # The rendering's name is a digest of this file's path and stamp, so
        # the key has to be taken while the file is still here. The PDF is a
        # readable copy of the whole document, which is the thing a delete is
        # being asked to get rid of.
        rendered = None
        try:
            rendered = cache_dir() / f"{cache_key(entry)}.pdf"
        except OSError:
            pass
        for path in (entry, rendered):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # A sweep takes it later. Failing a delete over its cache copy
                # would leave a reader with a row they cannot be rid of.
                logger.debug("pdf-preview: could not remove {}", path)


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


async def png_for(source: Path, *, timeout_s: float | None = None) -> Path:
    """The first page of ``source`` as a PNG, rendered if nothing current exists.

    For a tile with room for one picture and not for a viewer. LibreOffice's PNG
    export writes the first page alone, which is the thumbnail; no shortcut to a
    published PDF here, because turning that into a picture would need a
    rasteriser this package does not carry. Same path policy caveat, same cache
    and lock as :func:`pdf_for`, keyed apart so the two renderings of one deck
    do not race for one file.
    """
    budget = CONVERT_TIMEOUT_S if timeout_s is None else timeout_s
    key = cache_key(source) + "-thumb"
    cached = cache_dir() / f"{key}.png"
    if cached.is_file():
        return _touched(cached)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if cached.is_file():
            return _touched(cached)
        if source.suffix.lower() == ".pdf":
            await asyncio.to_thread(_rasterise_pdf_page, source, cached, THUMB_WIDTH_PX, budget)
        else:
            await asyncio.to_thread(_render, source, cached, budget, "png")
    return cached


#: The width of a PDF's first page as a tile picture. LibreOffice's PNG export
#: of a deck is the slide at screen size; a PDF page is drawn to about the same.
THUMB_WIDTH_PX = 1280

#: And the ceiling on the picture that width implies. A page's height is a
#: number inside the file, so width alone bounds nothing: a legal 519-byte PDF
#: declaring a 72 x 14400 point page draws 1280 x 256000 at this width, which is
#: 328 megapixels and 1.4 GB of resident memory for one thumbnail, several of
#: them at once on a transcript full of deliveries. Past this the whole page is
#: scaled down instead, so a tall page arrives small rather than expensively.
#: Four megapixels holds a letter page at full width (1280 x 1656) with room.
THUMB_MAX_PIXELS = 4_000_000


def _rasterise_pdf_page(source: Path, target: Path, width: int, timeout_s: float = CONVERT_TIMEOUT_S) -> None:
    """The first page of a PDF as a PNG at most ``width`` wide, written atomically.

    A PDF is already a rendering, so LibreOffice has nothing to convert (and its
    Draw import refuses most of them). PyMuPDF draws the page when it is
    installed -- it rides with either deck engine, so an install that makes
    decks has it -- and poppler's ``pdftoppm`` is the fallback for one that
    does not. Neither is a dependency of this package; both are probed.

    ``timeout_s`` bounds the fallback, which is a child process. The PyMuPDF
    path is bounded by :data:`THUMB_MAX_PIXELS` instead: it draws in this
    thread, where a clock would not stop it, so what is bounded is the work
    rather than the wait.
    """
    root = cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="render-", dir=root))
    try:
        staged = scratch / "page.png"
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:
            try:
                import fitz as pymupdf  # type: ignore[import-not-found, no-redef]
            except ImportError:
                pymupdf = None
        if pymupdf is not None:
            with pymupdf.open(source) as doc:
                if doc.page_count == 0:
                    raise PdfPreviewError(f"{source.name} has no pages")
                page = doc[0]
                zoom = _thumb_zoom(page.rect.width, page.rect.height, width)
                page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(str(staged))
        else:
            pdftoppm = shutil.which("pdftoppm")
            if pdftoppm is None:
                raise PdfPreviewUnavailableError(
                    f"no PDF rasteriser on the gateway host, so {source.name} has no picture: "
                    "install a deck engine (PyMuPDF) or poppler (pdftoppm)"
                )
            import subprocess

            prefix = scratch / "p"
            subprocess.run(
                [
                    pdftoppm,
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-scale-to-x",
                    str(width),
                    "-scale-to-y",
                    "-1",
                    "-png",
                    str(source),
                    str(prefix),
                ],
                check=True,
                capture_output=True,
                timeout=timeout_s,
            )
            made = sorted(scratch.glob("p-*.png"))
            if not made:
                raise PdfPreviewError(f"pdftoppm drew nothing for {source.name}")
            os.replace(made[0], staged)
        _sweep(root)
        os.replace(staged, target)
    except PdfPreviewError:
        raise
    except Exception as exc:  # noqa: BLE001 - every way a rasteriser fails is this route's 500
        # Not a tuple of the ones foreseen: PyMuPDF refuses a page past its own
        # limits with an exception of its own (`FzErrorLimit`), and the fallback
        # can raise `CalledProcessError` or `TimeoutExpired`. Left uncaught each
        # of those reaches the transport as an unnamed failure; named here, the
        # route answers with the same sentence as every other render that could
        # not be made.
        raise PdfPreviewError(f"{source.name} could not be drawn: {exc}") from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _render(source: Path, target: Path, timeout_s: float, fmt: str = "pdf") -> None:
    executable = find_soffice()
    if executable is None:
        raise PdfPreviewUnavailableError(
            # Named for what the reader asked to see, not for the first
            # caller this had: a spreadsheet reported as a deck reads like
            # the wrong file was opened.
            f"LibreOffice is not installed on the gateway host, so {source.name} cannot be shown. "
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
            done = office.to_pdf(
                source, staged, executable=executable, timeout_s=timeout_s, profile_root=scratch, fmt=fmt
            )
        except TimeoutError as exc:
            raise PdfPreviewTimeoutError(
                f"LibreOffice took longer than {timeout_s:g}s to render {source.name} and was stopped"
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
                f"LibreOffice did not produce a {fmt.upper()} for {source.name}" + (f": {detail}" if detail else "")
            )
        _sweep(root)
        os.replace(done.produced[0], target)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _thumb_zoom(page_width: float, page_height: float, width: int) -> float:
    """The scale that makes a page ``width`` wide, or less where that would draw
    more than :data:`THUMB_MAX_PIXELS`."""
    zoom = width / max(page_width, 1.0)
    pixels = max(page_width * zoom, 1.0) * max(page_height * zoom, 1.0)
    if pixels > THUMB_MAX_PIXELS:
        zoom *= math.sqrt(THUMB_MAX_PIXELS / pixels)
    return zoom


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
    # ``*`` under the root's own subdirectories as well as the root: a renderer
    # that has to give its input a meaningful suffix keeps a copy of the source
    # beside the output, and a sweep that only counts the outputs lets those
    # accumulate for as long as the installation lives. Their owner removes
    # them when the document goes; this is what bounds the ones whose owner
    # never got the chance.
    for entry in (*root.glob("*.pdf"), *root.glob("*/*")):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue
