"""The deck template gallery's own pieces: where the templates come from, how a
page becomes a picture, and what a cover or a page set does when the host cannot
draw. The console handlers that list, page and pick are tested with the console."""

from __future__ import annotations

import asyncio
import sys
import types
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

    async def slow_cover(t):
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
    (tmp_path / "covers" / f"{deck_templates._cover_key(two.path)}.jpg").write_bytes(b"\xff\xd8")
    drawn: list[str] = []

    async def draw(template):
        drawn.append(template.name)
        return None

    monkeypatch.setattr(deck_templates, "cover_for", draw)

    task = deck_templates.warm_covers_in_background(delay_s=0)
    assert task is not None
    await task
    await asyncio.gather(*deck_templates._drawing.values())
    assert sorted(drawn) == ["one", "three"], "the cover already on disk is left alone"


def test_warming_is_a_no_op_without_an_engine_or_a_rasteriser(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(deck_templates, "templates_dir", lambda: None)
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: True)
    assert deck_templates.warm_covers_in_background(delay_s=0) is None

    root = tmp_path / "tpl"
    root.mkdir()
    monkeypatch.setattr(deck_templates, "templates_dir", lambda: root)
    monkeypatch.setattr(deck_templates, "_rasteriser_available", lambda: False)
    assert deck_templates.warm_covers_in_background(delay_s=0) is None
