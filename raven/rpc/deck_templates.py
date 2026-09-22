"""The bundled deck templates, for the page's template picker.

A deck on a template is the deck engine's work, and the engine runs only when a
turn hands it a ``.pptx`` (the route on ``agents/raven-design/subagent.json``
opens on that file). The picker is how a reader hands one over without owning a
copy: it shows the templates the engine ships, and picking one deposits that
file under ``<agent home>/uploads`` exactly as ``fs.upload`` would, so the same
path rides on ``turn.send`` and the same fence admits it.

The host does not import the engine. What it needs is a directory, and
``find_spec`` locates the installed package without executing it; a deployment
without the engine wheel simply has no templates to show. The covers are the
first page of each template, rendered through the deck viewer's own PDF cache
(``pdf_preview``) and rasterised with PyMuPDF where the engine's dependency
brought it (imported lazily, so the host's own dependency face stays as it
is) -- a host without either shows the names alone.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import shutil
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from loguru import logger

COVER_WIDTH_PX = 480
COVER_JPEG_QUALITY = 78
#: The pages a reader flips through before picking: wide enough to read the
#: template's type at the sheet's width, small enough that twenty of them ride
#: in one answer.
PAGE_WIDTH_PX = 960
PAGE_JPEG_QUALITY = 72
_MIME_JPEG = "image/jpeg"
#: How many covers LibreOffice draws at once. A cold gallery asks for every
#: bundled template in one walk, and one soffice per template is ten
#: processes and a couple of GB started by a single click on a gateway that is
#: also serving turns. Three keeps most of the measured gain of fanning out
#: (the per-conversion time barely moves past four) at a third of the cost.
COVER_RENDERS_AT_ONCE = 3
_render_gate = asyncio.Semaphore(COVER_RENDERS_AT_ONCE)


def templates_dir() -> Path | None:
    """Where the installed engine keeps its templates, or None without the engine."""
    try:
        spec = find_spec("raven_ppt")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    root = Path(next(iter(spec.submodule_search_locations))) / "assets" / "templates"
    return root if root.is_dir() else None


def label_for(stem: str) -> str:
    """``amber_wave_quarterly_summary`` reads as ``Amber Wave Quarterly Summary``."""
    return " ".join(part.capitalize() for part in stem.replace("-", "_").split("_") if part)


@dataclass(frozen=True)
class Template:
    name: str
    path: Path

    @property
    def label(self) -> str:
        return label_for(self.name)

    @property
    def size(self) -> int:
        return self.path.stat().st_size


def bundled() -> list[Template]:
    """Every bundled template, by file name order, so the picker is stable across loads."""
    root = templates_dir()
    if root is None:
        return []
    return [Template(name=p.stem, path=p) for p in sorted(root.glob("*.pptx")) if p.is_file()]


def find(name: str) -> Template | None:
    """The bundled template named ``name`` (a stem, ``.pptx`` tolerated), or None."""
    wanted = str(name or "").strip().removesuffix(".pptx")
    if not wanted or "/" in wanted or "\\" in wanted or wanted.startswith("."):
        return None
    for template in bundled():
        if template.name == wanted:
            return template
    return None


def cover_cache_dir() -> Path:
    from raven.config.paths import get_cache_dir

    return get_cache_dir() / "deck-template-covers"


def _cover_key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha256(f"{path}\0{st.st_size}\0{st.st_mtime_ns}\0{COVER_WIDTH_PX}".encode()).hexdigest()[:32]


def _write_page(page: Any, target: Path, *, width: int, quality: int) -> None:
    """One PDF page as a JPEG ``width`` wide, written atomically."""
    pymupdf = _pymupdf()
    zoom = width / max(page.rect.width, 1.0)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(".part")
    pix.save(str(staged), output="jpeg", jpg_quality=quality)
    staged.replace(target)


def _rasterise_first_page(pdf: Path, target: Path) -> None:
    with _pymupdf().open(pdf) as doc:
        if doc.page_count == 0:
            raise ValueError(f"{pdf.name} has no pages")
        _write_page(doc[0], target, width=COVER_WIDTH_PX, quality=COVER_JPEG_QUALITY)


def _rasterise_every_page(pdf: Path, stem: Path) -> list[Path]:
    """Every page of ``pdf`` as ``<stem>-p01.jpg`` and so on; the list in page order."""
    out: list[Path] = []
    with _pymupdf().open(pdf) as doc:
        for n, page in enumerate(doc, start=1):
            target = stem.with_name(f"{stem.name}-p{n:02d}.jpg")
            if not target.is_file():
                _write_page(page, target, width=PAGE_WIDTH_PX, quality=PAGE_JPEG_QUALITY)
            out.append(target)
    return out


def _pymupdf():
    """PyMuPDF under its current name, or the old one on an older install."""
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    return pymupdf


def _rasteriser_available() -> bool:
    try:
        return find_spec("pymupdf") is not None or find_spec("fitz") is not None
    except (ImportError, ValueError):
        return False


async def cover_for(template: Template) -> Path | None:
    """The template's cover as a cached JPEG, or None where this host cannot draw one.

    None, not an error: a picker with names and no pictures still picks, and the
    reasons a cover is missing (no LibreOffice, no PyMuPDF, a render that timed
    out) are the host's, logged once here and not worth failing the list for.
    """
    cached = cover_cache_dir() / f"{_cover_key(template.path)}.jpg"
    if cached.is_file():
        return cached
    if not _rasteriser_available():
        return None
    from raven.rpc import pdf_preview

    try:
        async with _render_gate:
            pdf = await pdf_preview.pdf_for(template.path)
            await asyncio.to_thread(_rasterise_first_page, pdf, cached)
    except Exception as exc:  # noqa: BLE001 - a missing picture must not fail the list
        _failed.add(template.name)
        logger.warning("deck template {!r}: no cover ({}: {})", template.name, type(exc).__name__, exc)
        return None
    return cached


async def pages_for(template: Template) -> list[Path]:
    """Every page of the template as a cached JPEG, in order; empty where this host cannot draw.

    Rendered on demand, once per template: the reader asks for one template's
    pages when they open it, and twenty pages off an already-cached PDF is a
    second of work. Empty rather than an error for the same reason the cover
    is None -- the picker still picks by name.
    """
    if not _rasteriser_available():
        return []
    stem = cover_cache_dir() / _cover_key(template.path)
    from raven.rpc import pdf_preview

    try:
        pdf = await pdf_preview.pdf_for(template.path)
        return await asyncio.to_thread(_rasterise_every_page, pdf, stem)
    except Exception as exc:  # noqa: BLE001 - a missing preview must not fail the pick
        logger.warning("deck template {!r}: no pages ({}: {})", template.name, type(exc).__name__, exc)
        return []


def data_url(path: Path) -> str:
    return f"data:{_MIME_JPEG};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


_drawing: dict[str, asyncio.Task[Path | None]] = {}
#: Templates whose cover could not be drawn in this process. Not retried: the
#: reasons (no LibreOffice, a render that timed out, a broken file) do not
#: change between two clicks, and retrying would start LibreOffice on every
#: ask while the page kept polling for a picture that never comes.
_failed: set[str] = set()


def cached_cover(template: Template) -> Path | None:
    """The cover already on disk, or None; never renders."""
    cached = cover_cache_dir() / f"{_cover_key(template.path)}.jpg"
    return cached if cached.is_file() else None


def _draw_in_background(template: Template) -> bool:
    """Start drawing this template's cover unless one is already on its way; True while one is."""
    if template.name in _failed or not _rasteriser_available():
        return False
    task = _drawing.get(template.name)
    if task is not None and not task.done():
        return True
    task = asyncio.ensure_future(cover_for(template))
    _drawing[template.name] = task
    task.add_done_callback(
        lambda done, name=template.name: _drawing.pop(name, None) if _drawing.get(name) is done else None
    )
    return True


async def listing(*, with_covers: bool = True) -> tuple[list[dict], bool]:
    """What the picker shows, and whether a cover is still being drawn.

    Answers at once with what is on disk: the first render of ten templates is
    a minute and a half of LibreOffice, and a gallery that opens after that is a
    button that does nothing for a minute and a half. A missing cover is drawn
    in the background instead, and ``pending`` tells the page to ask again.
    """
    rows: list[dict] = []
    pending = False
    for template in bundled():
        cover = cached_cover(template) if with_covers else None
        if with_covers and cover is None and _draw_in_background(template):
            pending = True
        rows.append(
            {
                "name": template.name,
                "label": template.label,
                "size": template.size,
                "cover": data_url(cover) if cover is not None else None,
            }
        )
    return rows, pending


#: How long after the gateway is up the missing covers start drawing. The
#: roster and the page come first; LibreOffice can have the machine afterwards.
WARM_COVERS_AFTER_S = 15.0


def warm_covers_in_background(*, delay_s: float = WARM_COVERS_AFTER_S) -> asyncio.Task | None:
    """Draw every bundled template's missing cover once the gateway has settled.

    Never raises, and answers None rather than failing, because its callers are
    boot paths: a gallery that cannot be drawn is smaller than a gateway that
    does not start.

    The first cold gallery used to be built on the click that opened it: ten
    LibreOffice conversions, three at a time, a picker that filled in over half
    a minute. Drawn here instead, at start and through the same gate, a picker
    opened later finds its covers on disk. None where there is no engine or no
    rasteriser, and a no-op start to start once the cache is warm.
    """
    try:
        if templates_dir() is None or not _rasteriser_available():
            return None

        async def warm() -> None:
            await asyncio.sleep(delay_s)
            missing = [template for template in bundled() if cached_cover(template) is None]
            if not missing:
                return
            logger.info("deck templates: drawing {} missing cover(s) in the background", len(missing))
            for template in missing:
                _draw_in_background(template)

        return asyncio.ensure_future(warm())
    except Exception as exc:  # noqa: BLE001 - the gallery is optional, starting is not
        logger.debug("deck templates: covers not warmed ({})", exc)
        return None


def deposit(template: Template, uploads: Path) -> Path:
    """Copy the template under ``uploads`` as an attachment would land, never overwriting."""
    uploads.mkdir(parents=True, exist_ok=True)
    target = uploads / f"{template.name}.pptx"
    n = 1
    while target.exists():
        target = uploads / f"{template.name}-{n}.pptx"
        n += 1
    shutil.copyfile(template.path, target)
    return target


__all__ = [
    "COVER_WIDTH_PX",
    "Template",
    "bundled",
    "cached_cover",
    "cover_for",
    "data_url",
    "deposit",
    "find",
    "label_for",
    "listing",
    "pages_for",
    "templates_dir",
    "warm_covers_in_background",
]
