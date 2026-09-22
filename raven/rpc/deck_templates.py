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
import json
import shutil
import zipfile
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


#: The language the bundled templates are written in as they ship. Every other
#: language is made from them rather than shipped beside them.
SOURCE_LANGUAGE = "zh"


def phrasebook(language: str) -> dict[str, str]:
    """What this language calls each of the templates' stand-in strings, or ``{}``.

    A template's pages carry a designer's placeholder copy rather than content,
    and all of it is Chinese as the templates ship. A phrasebook beside them
    says each of those strings in another language; nothing beside them means a
    reader in that language sees the templates as they are.
    """
    root = templates_dir()
    if root is None or language == SOURCE_LANGUAGE:
        return {}
    try:
        loaded = json.loads((root / "i18n" / f"{language}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def translated_dir() -> Path:
    from raven.config.paths import get_cache_dir

    return get_cache_dir() / "deck-template-copies"


#: How wide a Latin character is beside a Chinese one, at the same size. A label
#: the designer fitted in Chinese is about twice as many characters in English,
#: and each of them is a little over half as wide, so the line grows.
LATIN_WIDTH = 0.58

#: How small a box may tell its text to go, and how much room is left over the
#: computed fit. The fit is then rounded down to a step, so two labels of
#: different lengths in boxes cut to one size still land on one size.
FIT_FLOOR = 0.5
FIT_MARGIN = 0.95
FIT_STEP = 0.2


#: Line height as a multiple of the type size, for reading a box's own size back
#: as the size of the type that filled it.
LINE_HEIGHT = 1.2


def _width(text: str) -> float:
    """Roughly how wide a string is, in ems, whatever it is written in."""
    wide = sum(1 for ch in text if 0x2E80 <= ord(ch) <= 0x9FFF or 0xFF00 <= ord(ch) <= 0xFFEF)
    return wide + (len(text) - wide) * LATIN_WIDTH


def _across(shape, ns_a: str, ems: float) -> float | None:
    """How many ems of the source's writing fit across this box, or None.

    Chinese breaks between any two glyphs, so a narrow box holds a long label by
    stacking it, and the label's own length says nothing about how wide the box
    is. English cannot break inside a word, so the box has to be measured: a box
    that held ``ems`` worth of glyphs over however many lines was set in type of
    about ``sqrt(w * h / (LINE_HEIGHT * ems))``, never taller than one line of
    its own height, and the box is that many of those across.
    """
    ext = shape.find(f".//{ns_a}xfrm/{ns_a}ext")
    if ext is None or ems <= 0:
        return None
    try:
        w, h = int(ext.get("cx")) / 12700, int(ext.get("cy")) / 12700
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    size = min((w * h / (LINE_HEIGHT * ems)) ** 0.5, h / LINE_HEIGHT)
    return w / size if size > 0 else None


def _fit(root, said_as: dict[str, str], ns_a: str, ns_p: str) -> None:
    """Tell each shape whose text grew to scale that text down to fit its box.

    The box is the designer's: a title box is cut to the line it was drawn for,
    with the body text under it, so a label that needs a second line lands on
    the paragraph below rather than pushing it down. The text gives way instead,
    by the ratio the two widths differ by, which is what ``normAutofit`` says in
    a file and what a reader would do by hand. Shapes drawn to one size are
    given one scale, so a row of cards does not come back at five sizes.
    """
    from lxml import etree

    wanted: dict = {}
    for shape in root.iter(f"{ns_p}sp"):
        worst = 1.0
        swapped = []
        for paragraph in shape.iter(f"{ns_a}p"):
            said = "".join(t.text or "" for t in paragraph.iter(f"{ns_a}t")).strip()
            was = said_as.get(said)
            if not was:
                continue
            swapped.append((was, said))
            before, after = _width(was), _width(said)
            if after > before > 0:
                worst = min(worst, before / after * FIT_MARGIN)
        across = _across(shape, ns_a, sum(_width(was) for was, _ in swapped))
        if across:
            longest = max((len(word) for _, said in swapped for word in said.split()), default=0)
            if longest:
                worst = min(worst, across / (longest * LATIN_WIDTH) * FIT_MARGIN)
        wanted[shape] = worst if worst >= 1.0 else max(int(worst / FIT_STEP) * FIT_STEP, FIT_FLOOR)
    shared: dict = {}
    for shape, scale in wanted.items():
        ext = shape.find(f".//{ns_a}xfrm/{ns_a}ext")
        if ext is not None:
            key = (ext.get("cx"), ext.get("cy"))
            shared[key] = min(shared.get(key, 1.0), scale)
    for shape, scale in wanted.items():
        ext = shape.find(f".//{ns_a}xfrm/{ns_a}ext")
        if ext is not None:
            scale = shared[(ext.get("cx"), ext.get("cy"))]
        if scale >= 1.0:
            continue
        body = shape.find(f".//{ns_a}bodyPr")
        if body is None:
            continue
        for stated in (
            *body.findall(f"{ns_a}normAutofit"),
            *body.findall(f"{ns_a}spAutoFit"),
            *body.findall(f"{ns_a}noAutofit"),
        ):
            body.remove(stated)
        etree.SubElement(body, f"{ns_a}normAutofit").set("fontScale", str(round(scale * 100000)))


def _swap_text(source: Path, target: Path, table: dict[str, str]) -> None:
    """Write ``source`` to ``target`` with every string the table knows said differently.

    Paragraph by paragraph rather than run by run: a line is often split across
    runs by formatting, so a run on its own is not a unit anything can be said
    about. The replacement goes into the paragraph's first run, which carries
    the formatting the designer chose, and the rest are emptied so the paragraph
    keeps its shape. A chart's categories and series are cell values rather than
    runs and are swapped on their own -- a template whose bars still read in the
    source language is not a translated template.

    The new text is then fitted to the box it landed in, since a language the
    template was not drawn for says the same thing at a different width.
    """
    from lxml import etree

    ns_a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    ns_c = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
    ns_p = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    said_as: dict[str, str] = {}
    for was, said in table.items():
        if _width(was) > _width(said_as.get(said, "")):
            said_as[said] = was
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.endswith(".xml"):
                try:
                    root = etree.fromstring(data)
                except etree.XMLSyntaxError:
                    out.writestr(item, data)
                    continue
                touched = False
                for paragraph in root.iter(f"{ns_a}p"):
                    runs = list(paragraph.iter(f"{ns_a}t"))
                    said = table.get("".join(r.text or "" for r in runs).strip()) if runs else None
                    if said is None:
                        continue
                    runs[0].text = said
                    for extra in runs[1:]:
                        extra.text = ""
                    touched = True
                for value in root.iter(f"{ns_c}v"):
                    said = table.get((value.text or "").strip())
                    if said is None:
                        continue
                    value.text = said
                    touched = True
                if touched:
                    _fit(root, said_as, ns_a, ns_p)
                    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            out.writestr(item, data)


def source_for(template: Template, language: str = SOURCE_LANGUAGE) -> Path:
    """The file this reader's cover, page set and pick are made from.

    The template as it ships, or a copy of it speaking the reader's language,
    built once and kept in the cache. Falling back to the shipped file is always
    right: a picker in the wrong language is worse than one in the reader's, but
    only a little, and a gallery that fails to open is worse than both.
    """
    table = phrasebook(language)
    if not table:
        return template.path
    cached = translated_dir() / f"{_cover_key(template.path)}-{language}.pptx"
    if cached.is_file():
        return cached
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        staged = cached.with_suffix(".part")
        _swap_text(template.path, staged, table)
        staged.replace(cached)
    except Exception as exc:  # noqa: BLE001 - a missing translation must not cost the picker
        logger.warning("deck template {!r}: no {} copy ({}: {})", template.name, language, type(exc).__name__, exc)
        return template.path
    return cached


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


async def cover_for(template: Template, language: str = SOURCE_LANGUAGE) -> Path | None:
    """The template's cover as a cached JPEG, or None where this host cannot draw one.

    None, not an error: a picker with names and no pictures still picks, and the
    reasons a cover is missing (no LibreOffice, no PyMuPDF, a render that timed
    out) are the host's, logged once here and not worth failing the list for.
    """
    source = source_for(template, language)
    cached = cover_cache_dir() / f"{_cover_key(source)}.jpg"
    if cached.is_file():
        return cached
    if not _rasteriser_available():
        return None
    from raven.rpc import pdf_preview

    try:
        async with _render_gate:
            pdf = await pdf_preview.pdf_for(source)
            await asyncio.to_thread(_rasterise_first_page, pdf, cached)
    except Exception as exc:  # noqa: BLE001 - a missing picture must not fail the list
        _failed.add(f"{template.name}:{language}")
        logger.warning("deck template {!r}: no cover ({}: {})", template.name, type(exc).__name__, exc)
        return None
    return cached


async def pages_for(template: Template, language: str = SOURCE_LANGUAGE) -> list[Path]:
    """Every page of the template as a cached JPEG, in order; empty where this host cannot draw.

    Rendered on demand, once per template: the reader asks for one template's
    pages when they open it, and twenty pages off an already-cached PDF is a
    second of work. Empty rather than an error for the same reason the cover
    is None -- the picker still picks by name.
    """
    if not _rasteriser_available():
        return []
    source = source_for(template, language)
    stem = cover_cache_dir() / _cover_key(source)
    from raven.rpc import pdf_preview

    try:
        pdf = await pdf_preview.pdf_for(source)
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


def cached_cover(template: Template, language: str = SOURCE_LANGUAGE) -> Path | None:
    """The cover already on disk, or None; never renders and never translates.

    A reader's copy that has not been built yet has no cover by definition, so
    the question is answered without building one: this is the cheap half of the
    listing, and drawing is the other half's business.
    """
    if phrasebook(language):
        source = translated_dir() / f"{_cover_key(template.path)}-{language}.pptx"
        if not source.is_file():
            return None
    else:
        source = template.path
    cached = cover_cache_dir() / f"{_cover_key(source)}.jpg"
    return cached if cached.is_file() else None


def _draw_in_background(template: Template, language: str = SOURCE_LANGUAGE) -> bool:
    """Start drawing this template's cover unless one is already on its way; True while one is."""
    key = f"{template.name}:{language}"
    if key in _failed or not _rasteriser_available():
        return False
    task = _drawing.get(key)
    if task is not None and not task.done():
        return True
    task = asyncio.ensure_future(cover_for(template, language))
    _drawing[key] = task
    task.add_done_callback(lambda done, k=key: _drawing.pop(k, None) if _drawing.get(k) is done else None)
    return True


async def listing(*, with_covers: bool = True, language: str = SOURCE_LANGUAGE) -> tuple[list[dict], bool]:
    """What the picker shows, and whether a cover is still being drawn.

    Answers at once with what is on disk: the first render of ten templates is
    a minute and a half of LibreOffice, and a gallery that opens after that is a
    button that does nothing for a minute and a half. A missing cover is drawn
    in the background instead, and ``pending`` tells the page to ask again.
    """
    rows: list[dict] = []
    pending = False
    for template in bundled():
        cover = cached_cover(template, language) if with_covers else None
        if with_covers and cover is None and _draw_in_background(template, language):
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

_warming: asyncio.Task | None = None
"""The warm-up this process started, held so a shutdown can stop it."""


def warm_covers_in_background(
    *, delay_s: float = WARM_COVERS_AFTER_S, language: str = SOURCE_LANGUAGE
) -> asyncio.Task | None:
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
    global _warming
    try:
        if templates_dir() is None or not _rasteriser_available():
            return None

        async def warm() -> None:
            await asyncio.sleep(delay_s)
            missing = [template for template in bundled() if cached_cover(template, language) is None]
            if not missing:
                return
            logger.info("deck templates: drawing {} missing cover(s) in the background", len(missing))
            for template in missing:
                _draw_in_background(template, language)

        _warming = asyncio.ensure_future(warm())
        return _warming
    except Exception as exc:  # noqa: BLE001 - the gallery is optional, starting is not
        logger.debug("deck templates: covers not warmed ({})", exc)
        return None


def stop_warming() -> None:
    """Stop the warm-up and the conversions it started; for a shutdown.

    Cancelling the tasks is half of it. A conversion is waited for in a thread,
    and neither the cancel nor the loop closing reaches the child LibreOffice:
    the interpreter's own thread-join then holds the process open until the
    conversion ends by itself, which is why a gateway stopped during its first
    cold warm used to sit there. The converters are stopped through the module
    that owns them.
    """
    global _warming
    from raven.utils import office

    if _warming is not None:
        _warming.cancel()
        _warming = None
    for task in list(_drawing.values()):
        task.cancel()
    _drawing.clear()
    try:
        stopped = office.stop_running()
    except Exception as exc:  # noqa: BLE001 - a shutdown is not the place to raise
        logger.debug("deck templates: converters not stopped ({})", exc)
        return
    if stopped:
        logger.info("deck templates: stopped {} conversion(s) still running", stopped)


def deposit(template: Template, uploads: Path, language: str = SOURCE_LANGUAGE) -> Path:
    """Copy the template under ``uploads`` as an attachment would land, never overwriting.

    The reader's copy rather than the shipped one. A deck is built by cloning the
    template's pages and replacing what they say, so a page whose stand-in copy
    already reads in the reader's language is one its author can read, name and
    check -- and a line the author forgets to replace then reads as a slip
    rather than as the wrong language.
    """
    uploads.mkdir(parents=True, exist_ok=True)
    target = uploads / f"{template.name}.pptx"
    n = 1
    while target.exists():
        target = uploads / f"{template.name}-{n}.pptx"
        n += 1
    shutil.copyfile(source_for(template, language), target)
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
    "phrasebook",
    "source_for",
    "templates_dir",
    "stop_warming",
    "warm_covers_in_background",
]
