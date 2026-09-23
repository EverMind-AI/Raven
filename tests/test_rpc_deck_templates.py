"""The deck template gallery's own pieces: where the templates come from, how a
page becomes a picture, and what a cover or a page set does when the host cannot
draw. The console handlers that list, page and pick are tested with the console."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

from raven.rpc import deck_templates

# --- a renderer that draws nothing but leaves a file ----------------------------


class _Rect:
    width = 960.0


class _Pixmap:
    def __init__(self, page: _Page, zoom: float) -> None:
        self.page = page
        self.zoom = zoom

    def save(self, path: str, *, output: str, jpg_quality: int) -> None:
        Path(path).write_bytes(f"{output}:{self.page.number}:{self.zoom:.3f}:{jpg_quality}".encode())


class _Page:
    rect = _Rect()

    def __init__(self, number: int) -> None:
        self.number = number

    def get_pixmap(self, *, matrix, alpha: bool) -> _Pixmap:
        assert alpha is False
        return _Pixmap(self, matrix.zoom)


class _Doc:
    def __init__(self, page_count: int) -> None:
        self.page_count = page_count
        self.pages = [_Page(n) for n in range(page_count)]

    def __enter__(self) -> _Doc:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def __getitem__(self, n: int) -> _Page:
        return self.pages[n]

    def __iter__(self):
        return iter(self.pages)


class _Matrix:
    def __init__(self, zx: float, zy: float) -> None:
        assert zx == zy
        self.zoom = zx


def _fake_pymupdf(page_count: int) -> types.ModuleType:
    module = types.ModuleType("pymupdf")
    module.Matrix = _Matrix
    module.open = lambda path: _Doc(page_count)
    return module


@pytest.fixture
def templates(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "tpl"
    root.mkdir()
    (root / "amber_wave.pptx").write_bytes(b"PKamber")
    monkeypatch.setattr(deck_templates, "templates_dir", lambda: root)
    monkeypatch.setattr(deck_templates, "cover_cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(deck_templates, "_failed", set())
    monkeypatch.setattr(deck_templates, "_drawing", {})
    return root


# --- where the templates come from -----------------------------------------------


def test_templates_dir_is_the_installed_engines_assets_or_none(tmp_path: Path, monkeypatch) -> None:
    engine = tmp_path / "raven_ppt"
    (engine / "assets" / "templates").mkdir(parents=True)
    spec = types.SimpleNamespace(submodule_search_locations=[str(engine)])
    monkeypatch.setattr(deck_templates, "find_spec", lambda name: spec)
    assert deck_templates.templates_dir() == engine / "assets" / "templates"

    (engine / "assets" / "templates").rmdir()
    assert deck_templates.templates_dir() is None, "an engine without templates is no gallery"

    monkeypatch.setattr(deck_templates, "find_spec", lambda name: None)
    assert deck_templates.templates_dir() is None, "no engine, no gallery"

    def broken(name: str):
        raise ValueError(name)

    monkeypatch.setattr(deck_templates, "find_spec", broken)
    assert deck_templates.templates_dir() is None


def test_pymupdf_is_found_under_its_new_name_or_its_old_one(monkeypatch) -> None:
    fresh = types.ModuleType("pymupdf")
    monkeypatch.setitem(sys.modules, "pymupdf", fresh)
    assert deck_templates._pymupdf() is fresh

    old = types.ModuleType("fitz")
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", old)
    assert deck_templates._pymupdf() is old


def test_rasteriser_availability_is_a_probe_that_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(deck_templates, "find_spec", lambda name: object() if name == "fitz" else None)
    assert deck_templates._rasteriser_available() is True

    def broken(name: str):
        raise ImportError(name)

    monkeypatch.setattr(deck_templates, "find_spec", broken)
    assert deck_templates._rasteriser_available() is False


# --- a page becomes a picture ------------------------------------------------------


def test_first_page_is_written_at_cover_width_through_a_staging_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(deck_templates, "_pymupdf", lambda: _fake_pymupdf(3))
    target = tmp_path / "covers" / "abc.jpg"

    deck_templates._rasterise_first_page(tmp_path / "deck.pdf", target)

    zoom = deck_templates.COVER_WIDTH_PX / 960
    assert target.read_bytes() == f"jpeg:0:{zoom:.3f}:{deck_templates.COVER_JPEG_QUALITY}".encode()
    assert not target.with_suffix(".part").exists(), "the staging file is renamed, not left"


def test_a_pdf_without_pages_has_no_cover(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(deck_templates, "_pymupdf", lambda: _fake_pymupdf(0))
    with pytest.raises(ValueError, match="no pages"):
        deck_templates._rasterise_first_page(tmp_path / "empty.pdf", tmp_path / "covers" / "x.jpg")


def test_every_page_is_written_once_in_order_and_kept_between_asks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(deck_templates, "_pymupdf", lambda: _fake_pymupdf(3))
    stem = tmp_path / "covers" / "abc"

    first = deck_templates._rasterise_every_page(tmp_path / "deck.pdf", stem)
    assert [p.name for p in first] == ["abc-p01.jpg", "abc-p02.jpg", "abc-p03.jpg"]
    zoom = deck_templates.PAGE_WIDTH_PX / 960
    assert first[1].read_bytes() == f"jpeg:1:{zoom:.3f}:{deck_templates.PAGE_JPEG_QUALITY}".encode()

    first[1].write_bytes(b"kept")
    again = deck_templates._rasterise_every_page(tmp_path / "deck.pdf", stem)
    assert again == first
    assert first[1].read_bytes() == b"kept", "a page already on disk is not drawn again"


# --- a cover, a page set, and the host that cannot draw ---------------------------


async def test_cover_for_answers_from_disk_and_draws_once_when_it_can(templates: Path, monkeypatch) -> None:
    from raven.rpc import pdf_preview

    template = deck_templates.bundled()[0]
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: True)
    rendered: list[Path] = []

    async def pdf_for(path: Path, **_) -> Path:
        rendered.append(path)
        return path.with_suffix(".pdf")

    def rasterise(pdf: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xd8cover")

    monkeypatch.setattr(pdf_preview, "pdf_for", pdf_for)
    monkeypatch.setattr(deck_templates, "_rasterise_first_page", rasterise)

    cover = await deck_templates.cover_for(template)
    assert cover is not None and cover.read_bytes() == b"\xff\xd8cover"
    assert await deck_templates.cover_for(template) == cover
    assert rendered == [template.path], "the second ask is answered from disk"
    assert deck_templates.data_url(cover) == "data:image/jpeg;base64,/9hjb3Zlcg=="


async def test_cover_for_is_none_where_the_host_cannot_draw(templates: Path, monkeypatch) -> None:
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: False)
    assert await deck_templates.cover_for(deck_templates.bundled()[0]) is None


async def test_pages_for_renders_every_page_or_none(templates: Path, monkeypatch) -> None:
    from raven.rpc import pdf_preview

    template = deck_templates.bundled()[0]
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: False)
    assert await deck_templates.pages_for(template) == []

    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: True)

    async def pdf_for(path: Path, **_) -> Path:
        return path.with_suffix(".pdf")

    monkeypatch.setattr(pdf_preview, "pdf_for", pdf_for)
    monkeypatch.setattr(deck_templates, "_rasterise_every_page", lambda pdf, stem: [stem.with_name("p01.jpg")])
    assert [p.name for p in await deck_templates.pages_for(template)] == ["p01.jpg"]

    async def no_office(path: Path, **_) -> Path:
        raise RuntimeError("soffice is not installed")

    monkeypatch.setattr(pdf_preview, "pdf_for", no_office)
    assert await deck_templates.pages_for(template) == [], "a missing preview is not an error; the pick still picks"


async def test_a_cover_on_its_way_is_not_started_twice(templates: Path, monkeypatch) -> None:
    template = deck_templates.bundled()[0]
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: True)
    release = asyncio.Event()
    starts = 0

    async def slow_cover(t, *_):
        nonlocal starts
        starts += 1
        await release.wait()
        return None

    monkeypatch.setattr(deck_templates, "cover_for", slow_cover)

    assert deck_templates._draw_in_background(template) is True
    await asyncio.sleep(0)
    assert deck_templates._draw_in_background(template) is True, "still on its way"
    assert starts == 1
    release.set()
    await asyncio.gather(*deck_templates._drawing.values())
    assert deck_templates._drawing == {}, "a finished draw leaves the ledger"


# --- the covers drawn at start, not on the click -----------------------------------


async def test_warming_draws_only_the_covers_that_are_missing(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "tpl"
    root.mkdir()
    for name in ("one", "two", "three"):
        (root / f"{name}.pptx").write_bytes(b"PK" + name.encode())
    monkeypatch.setattr(deck_templates, "templates_dir", lambda: root)
    monkeypatch.setattr(deck_templates, "cover_cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: True)
    monkeypatch.setattr(deck_templates, "_failed", set())
    monkeypatch.setattr(deck_templates, "_drawing", {})
    two = deck_templates.find("two")
    assert two is not None
    (tmp_path / "covers").mkdir()
    (tmp_path / "covers" / f"{deck_templates._drawn_key(two.path)}.jpg").write_bytes(b"\xff\xd8")
    drawn: list[str] = []

    async def draw(template, *_):
        drawn.append(template.name)
        return None

    monkeypatch.setattr(deck_templates, "cover_for", draw)

    task = deck_templates.warm_covers_in_background(delay_s=0)
    assert task is not None
    await task
    await asyncio.gather(*deck_templates._drawing.values())
    assert sorted(drawn) == ["one", "three"], "the cover already on disk is left alone"


async def test_warming_draws_nothing_when_every_cover_is_on_disk(templates: Path, monkeypatch) -> None:
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: True)
    template = deck_templates.bundled()[0]
    covers = deck_templates.cover_cache_dir()
    covers.mkdir(parents=True, exist_ok=True)
    (covers / f"{deck_templates._drawn_key(template.path)}.jpg").write_bytes(b"\xff\xd8")
    drawn: list[str] = []

    async def draw(t, *_):
        drawn.append(t.name)
        return None

    monkeypatch.setattr(deck_templates, "cover_for", draw)

    task = deck_templates.warm_covers_in_background(delay_s=0)
    assert task is not None
    await task
    assert drawn == [] and deck_templates._drawing == {}


def test_warming_never_raises_out_of_a_boot_path(monkeypatch) -> None:
    """Its callers are the gateway's and the serve path's boot; a gallery that
    cannot be drawn is smaller than a gateway that does not start."""

    def broken() -> None:
        raise RuntimeError("no engine here")

    monkeypatch.setattr(deck_templates, "templates_dir", broken)
    assert deck_templates.warm_covers_in_background(delay_s=0) is None


def test_warming_is_a_no_op_without_an_engine_or_a_rasteriser(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(deck_templates, "templates_dir", lambda: None)
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: True)
    assert deck_templates.warm_covers_in_background(delay_s=0) is None

    root = tmp_path / "tpl"
    root.mkdir()
    monkeypatch.setattr(deck_templates, "templates_dir", lambda: root)
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: False)
    assert deck_templates.warm_covers_in_background(delay_s=0) is None


async def test_a_shutdown_stops_the_warm_up_and_the_conversions_it_started(templates: Path, monkeypatch) -> None:
    """Cancelling the tasks is half of it: a conversion is waited for in a thread
    that no cancel reaches, and the interpreter's own thread-join then holds the
    process open until LibreOffice finishes. The converters are stopped too."""
    from raven.utils import office

    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: True)
    stopped: list[int] = []
    monkeypatch.setattr(office, "stop_running", lambda: stopped.append(1) or 1)
    release = asyncio.Event()

    async def slow(template, language=deck_templates.SOURCE_LANGUAGE):
        await release.wait()
        return None

    monkeypatch.setattr(deck_templates, "cover_for", slow)

    task = deck_templates.warm_covers_in_background(delay_s=0)
    assert task is not None
    await task
    assert len(deck_templates._drawing) == 1

    deck_templates.stop_warming()

    assert stopped == [1], "the child LibreOffice is stopped, not only the task"
    assert deck_templates._drawing == {}
    assert deck_templates._warming is None
    release.set()


def test_a_shutdown_that_cannot_stop_the_converters_still_returns(monkeypatch) -> None:
    """A shutdown is not the place to raise: whatever the converters do, the
    teardown after this call has to run."""
    from raven.utils import office

    monkeypatch.setattr(deck_templates, "_warming", None)
    monkeypatch.setattr(deck_templates, "_drawing", {})

    def broken() -> int:
        raise RuntimeError("the registry is gone")

    monkeypatch.setattr(office, "stop_running", broken)
    deck_templates.stop_warming()


def test_a_shutdown_before_any_warm_up_is_harmless(monkeypatch) -> None:
    from raven.utils import office

    monkeypatch.setattr(deck_templates, "_warming", None)
    monkeypatch.setattr(deck_templates, "_drawing", {})
    monkeypatch.setattr(office, "stop_running", lambda: 0)
    deck_templates.stop_warming()


# --- the reader's own copy of a template ------------------------------------------


def _pptx_with(text: str, path: Path) -> Path:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    para = box.text_frame.paragraphs[0]
    # Two runs for one line, as a designer's formatting leaves it: the swap has to
    # read the line, not the fragments.
    half = len(text) // 2
    for piece in (text[:half], text[half:]):
        para.add_run().text = piece
    prs.save(str(path))
    return path


def _phrasebook(monkeypatch, tmp_path: Path, table: dict) -> Path:
    root = tmp_path / "tpl"
    (root / "i18n").mkdir(parents=True, exist_ok=True)
    (root / "i18n" / "en.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(deck_templates, "templates_dir", lambda: root)
    monkeypatch.setattr(deck_templates, "translated_dir", lambda: tmp_path / "copies")
    monkeypatch.setattr(deck_templates, "cover_cache_dir", lambda: tmp_path / "covers")
    return root


def test_a_phrasebook_is_the_templates_own_and_the_source_language_has_none(tmp_path: Path, monkeypatch) -> None:
    root = _phrasebook(monkeypatch, tmp_path, {"\u76ee\u5f55": "Contents"})
    assert deck_templates.phrasebook("en") == {"\u76ee\u5f55": "Contents"}
    assert deck_templates.phrasebook(deck_templates.SOURCE_LANGUAGE) == {}, "the templates already speak it"
    assert deck_templates.phrasebook("de") == {}, "no book, no translation, no error"

    (root / "i18n" / "en.json").write_text("not json", encoding="utf-8")
    assert deck_templates.phrasebook("en") == {}
    monkeypatch.setattr(deck_templates, "templates_dir", lambda: None)
    assert deck_templates.phrasebook("en") == {}


def test_the_readers_copy_is_built_once_and_says_the_line_not_the_fragments(tmp_path: Path, monkeypatch) -> None:
    from pptx import Presentation

    root = _phrasebook(monkeypatch, tmp_path, {"\u5b63\u5ea6\u603b\u7ed3": "Quarterly Summary"})
    _pptx_with("\u5b63\u5ea6\u603b\u7ed3", root / "amber.pptx")
    template = deck_templates.bundled()[0]

    english = deck_templates.source_for(template, "en")
    assert english != template.path and english.is_file()
    assert "Quarterly Summary" in Presentation(str(english)).slides[0].shapes[0].text_frame.text
    assert "\u5b63\u5ea6" in Presentation(str(template.path)).slides[0].shapes[0].text_frame.text, (
        "the shipped file is untouched"
    )

    stamp = english.stat().st_mtime_ns
    assert deck_templates.source_for(template, "en") == english
    assert english.stat().st_mtime_ns == stamp, "the second ask reads the copy it already made"
    assert deck_templates.source_for(template, deck_templates.SOURCE_LANGUAGE) == template.path
    assert not list((tmp_path / "copies").glob("*.part")), "the staged copy is renamed, not left"


def test_a_copy_that_cannot_be_built_falls_back_to_the_shipped_template(tmp_path: Path, monkeypatch) -> None:
    root = _phrasebook(monkeypatch, tmp_path, {"\u76ee\u5f55": "Contents"})
    _pptx_with("\u76ee\u5f55", root / "amber.pptx")
    template = deck_templates.bundled()[0]

    def broken(*_args, **_kwargs):
        raise RuntimeError("no lxml here")

    monkeypatch.setattr(deck_templates, "_swap_text", broken)
    assert deck_templates.source_for(template, "en") == template.path


def test_a_charts_categories_are_swapped_too(tmp_path: Path, monkeypatch) -> None:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    root = _phrasebook(monkeypatch, tmp_path, {"\u7c7b\u522b1": "Category 1", "\u7cfb\u5217 1": "Series 1"})
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    data = CategoryChartData()
    data.categories = ["\u7c7b\u522b1"]
    data.add_series("\u7cfb\u5217 1", (1.0,))
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(4), Inches(3), data)
    prs.save(str(root / "amber.pptx"))

    english = deck_templates.source_for(deck_templates.bundled()[0], "en")
    with zipfile.ZipFile(english) as z:
        charts = "".join(z.read(n).decode("utf8") for n in z.namelist() if "/charts/" in n)
    assert "Category 1" in charts and "Series 1" in charts
    assert "\u7c7b\u522b1" not in charts, "a chart still reading in the source language is not translated"


async def test_a_pick_hands_over_the_readers_copy(tmp_path: Path, monkeypatch) -> None:
    from pptx import Presentation

    root = _phrasebook(monkeypatch, tmp_path, {"\u76ee\u5f55": "Contents"})
    _pptx_with("\u76ee\u5f55", root / "amber.pptx")
    template = deck_templates.bundled()[0]

    landed = deck_templates.deposit(template, tmp_path / "uploads", "en")
    assert landed.name == "amber.pptx"
    assert "Contents" in Presentation(str(landed)).slides[0].shapes[0].text_frame.text

    shipped = deck_templates.deposit(template, tmp_path / "uploads", deck_templates.SOURCE_LANGUAGE)
    assert shipped.name == "amber-1.pptx", "a second pick never overwrites the first"
    assert "\u76ee\u5f55" in Presentation(str(shipped)).slides[0].shapes[0].text_frame.text


async def test_each_language_has_its_own_cover_and_neither_answers_for_the_other(tmp_path: Path, monkeypatch) -> None:
    root = _phrasebook(monkeypatch, tmp_path, {"\u76ee\u5f55": "Contents"})
    _pptx_with("\u76ee\u5f55", root / "amber.pptx")
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: True)
    monkeypatch.setattr(deck_templates, "_failed", set())
    monkeypatch.setattr(deck_templates, "_drawing", {})
    template = deck_templates.bundled()[0]
    drawn: list[str] = []

    async def draw(source, **_):
        drawn.append(source.name)
        return source.with_suffix(".pdf")

    def rasterise(pdf: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xd8cover")

    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "pdf_for", draw)
    monkeypatch.setattr(deck_templates, "_rasterise_first_page", rasterise)

    assert deck_templates.cached_cover(template, "en") is None
    zh_cover = await deck_templates.cover_for(template, deck_templates.SOURCE_LANGUAGE)
    en_cover = await deck_templates.cover_for(template, "en")
    assert zh_cover is not None and en_cover is not None and zh_cover != en_cover
    assert deck_templates.cached_cover(template, "en") == en_cover
    assert len(drawn) == 2, "one render each, not one shared between them"


def _scale_of(path: Path, index: int = 0) -> float | None:
    """What the shape's box tells its text to do about its own size."""
    from pptx import Presentation

    body = Presentation(str(path)).slides[0].shapes[index].text_frame._txBody.bodyPr
    fit = body.find("{http://schemas.openxmlformats.org/drawingml/2006/main}normAutofit")
    return None if fit is None else int(fit.get("fontScale")) / 100000


def test_a_label_that_grew_is_told_to_scale_down_and_one_that_did_not_is_left_alone(
    tmp_path: Path, monkeypatch
) -> None:
    grew = "\u5e74\u5ea6\u611f\u609f\u4e0e\u611f\u8c22"
    fits = "\u76ee\u5f55"
    root = _phrasebook(monkeypatch, tmp_path, {grew: "Annual Reflections And Thanks", fits: "TOC"})
    _pptx_with(grew, root / "amber.pptx")
    _pptx_with(fits, root / "beige.pptx")

    wide, narrow = deck_templates.bundled()
    scale = _scale_of(deck_templates.source_for(wide, "en"))
    assert scale is not None, "seven glyphs of Chinese do not hold twenty-eight characters of English"
    assert scale == deck_templates.FIT_FLOOR, "what it asks for is past the floor, so the floor is what it gets"
    assert _scale_of(deck_templates.source_for(narrow, "en")) is None, "'TOC' is narrower than what it replaced"


def test_boxes_cut_to_one_size_are_scaled_to_one_size(tmp_path: Path, monkeypatch) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    short, long = "\u56e2\u961f", "\u516c\u53f8\u5e73\u53f0"
    root = _phrasebook(monkeypatch, tmp_path, {short: "Team", long: "Thanks To The Company"})
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for n, text in enumerate((short, long)):
        box = slide.shapes.add_textbox(Inches(1), Inches(1 + n), Inches(3), Inches(1))
        box.text_frame.paragraphs[0].add_run().text = text
    prs.save(str(root / "cards.pptx"))

    english = deck_templates.source_for(deck_templates.bundled()[0], "en")
    assert _scale_of(english, 0) == _scale_of(english, 1), "a row of cards comes back at one size"
    assert _scale_of(english, 0) is not None and _scale_of(english, 0) < 1


def test_a_width_is_read_in_ems_whatever_the_writing(tmp_path: Path, monkeypatch) -> None:
    assert deck_templates._width("\u76ee\u5f55") == 2
    assert deck_templates._width("Contents") == pytest.approx(8 * deck_templates.LATIN_WIDTH)
    assert deck_templates._width("") == 0


def test_a_picture_drawn_without_chinese_is_not_served_after_the_host_can_draw_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source deck is byte-identical either side of a font fix, so a key made
    only of its path, size and mtime keeps serving the boxes. The upgrade would
    land, the pictures would not change, and nothing would say why."""
    from raven.utils import office

    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"PK placeholder")

    monkeypatch.setattr(office, "RENDER_GENERATION", 1)
    boxes = deck_templates._drawn_key(deck)

    monkeypatch.setattr(office, "RENDER_GENERATION", 2)
    drawn = deck_templates._drawn_key(deck)

    assert boxes != drawn, "a cover drawn with no Han face would be served for one drawn with it"
    assert deck_templates._cover_key(deck) in boxes
    assert deck_templates._cover_key(deck) in drawn


def test_a_translated_copy_is_not_redone_just_because_the_fonts_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: the copy is a deck, not a picture. Its text swap does not
    depend on what the host can draw, so putting the font state in its name
    would rebuild every translation on a machine that only gained a font."""
    from raven.utils import office

    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"PK placeholder")

    monkeypatch.setattr(office, "RENDER_GENERATION", 1)
    before = deck_templates._cover_key(deck)

    monkeypatch.setattr(office, "RENDER_GENERATION", 2)

    assert deck_templates._cover_key(deck) == before
