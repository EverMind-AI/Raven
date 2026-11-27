"""Bringing something from the web into a deck's materials.

The tests that matter are about the bytes. A URL ending in `.png` that returns
HTML, and a `Content-Type: image/png` on something that is not an image, are both
ordinary on the open web -- and writing either into the materials directory would
fail later somewhere that reads like a bug in the ingest.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpcore
import httpx
import pytest
from httpcore._backends.auto import AutoBackend

from raven.security.network import judge_url_target as _real_judge
from raven.security.network import validate_url_target as _real_validate
from raven_ppt.contracts import Project
from raven_ppt.tools.fetch import MAX_BYTES, PptFetchTool

pytest.importorskip("PIL")


def _png(width: int = 8, height: int = 8) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _described_png(description: str, *, key: str = "Description") -> bytes:
    """A PNG carrying its own description in a text chunk, as an export tool writes it."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    chunks = PngInfo()
    chunks.add_text(key, description)
    buffer = io.BytesIO()
    Image.new("RGB", (320, 180), "white").save(buffer, format="PNG", pnginfo=chunks)
    return buffer.getvalue()


def _described_jpeg(description: str) -> bytes:
    """A JPEG carrying EXIF `ImageDescription`, NUL-padded the way cameras write it."""
    from PIL import Image

    image = Image.new("RGB", (320, 180), "white")
    exif = image.getexif()
    exif[0x010E] = f"{description}\x00\x00"
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def _pptx() -> bytes:
    """A real presentation, because a downloaded template is now prepared as it
    lands: the two ways one arrives -- by URL and by path -- have to end in the
    same state, and a zip that only looks like a deck no longer reaches it."""
    from pptx import Presentation

    buffer = io.BytesIO()
    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    presentation.save(buffer)
    return buffer.getvalue()


def _not_a_deck() -> bytes:
    """A zip shaped like a .pptx that python-pptx will not open."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("ppt/presentation.xml", "<presentation/>")
    return buffer.getvalue()


@pytest.fixture()
def fetch(monkeypatch, tmp_path: Path):
    """A tool whose HTTP layer is a MockTransport.

    Patched at httpx rather than at the tool, so the code under test is the code
    that ships -- including the streaming size guard, which a fake download method
    would have skipped.
    """

    def make(body: bytes, *, headers: dict | None = None, status: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=body, headers=headers or {})

        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs.pop("proxy", None)
            return real(*args, transport=transport, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        monkeypatch.setattr("raven.security.network.validate_url_target", lambda url: (True, ""))
        monkeypatch.setattr("raven.security.network.judge_url_target", lambda url: (True, "", ()))
        monkeypatch.setattr("raven.security.network.judge_resolved_url", lambda url: (True, "", ()))
        return PptFetchTool(tmp_path)

    return make


async def _run(tool: PptFetchTool, **kw) -> dict:
    args = {"project": "tarvis", "url": "https://example.com/thing"}
    return json.loads(await tool.execute(**{**args, **kw}))


@pytest.mark.asyncio
async def test_an_image_lands_in_the_decks_own_sources(fetch, tmp_path: Path) -> None:
    """One place, always. A fetch that could land elsewhere started a second pile, and
    the ingest that followed replaced the index rather than adding to it."""
    body = await _run(fetch(_png()), url="https://example.com/figure")

    assert body["ok"] is True and body["kind"] == "image"
    landed = Path(body["path"])
    assert landed.parent == Project(workspace=tmp_path, slug="tarvis").sources_dir
    assert landed.suffix == ".png" and landed.read_bytes()


@pytest.mark.asyncio
async def test_the_url_is_recorded_beside_what_it_fetched(fetch, tmp_path: Path) -> None:
    """The attribution `documents.source_urls` exists to provide and never had:
    nothing wrote the manifest it reads, so every fetched figure reached a slide with
    no source at all."""
    from raven_ppt.services.ingest import fetched

    await _run(fetch(_png()), url="https://example.com/figure")

    held = fetched(Project(workspace=tmp_path, slug="tarvis"))
    assert [source.url for source in held] == ["https://example.com/figure"]


@pytest.mark.asyncio
async def test_the_caption_the_caller_gives_reaches_the_figure_catalogue(fetch, tmp_path: Path) -> None:
    """The whole point of the parameter. A picture off a web page carries no caption in
    its bytes -- the words are in the HTML beside it -- so before this every fetched
    figure entered the catalogue with `caption: null` while a paper's figure arrived
    with the line printed under it. One live deck captioned a marketing banner as an
    architecture diagram with nothing to hold it to."""
    from raven_ppt.services.ingest import ingest_materials, load_catalogue

    tool = fetch(_png(320, 180))
    tool.ingest = ingest_materials
    caption = "Figure 2: Architectural overview of the Mem0 system"

    body = await _run(tool, url="https://example.com/figure.png", caption=caption)

    assert body["ok"] is True and body["caption"] == caption
    deck = Project(workspace=tmp_path, slug="tarvis")
    catalogue = load_catalogue(deck.ingest_dir / "figures.json", figures_dir=deck.ingest_dir / "figures")
    (asset,) = catalogue.values()
    assert asset.caption == caption
    assert asset.source_url == "https://example.com/figure.png"


@pytest.mark.asyncio
async def test_the_caption_is_recorded_beside_the_url_it_came_with(fetch, tmp_path: Path) -> None:
    from raven_ppt.services.ingest import fetched

    await _run(fetch(_png()), url="https://example.com/figure", caption="  Fig. 3\n  Retrieval latency  ")

    (held,) = fetched(Project(workspace=tmp_path, slug="tarvis"))
    assert held.caption == "Fig. 3 Retrieval latency"


@pytest.mark.asyncio
async def test_a_picture_that_describes_itself_needs_no_caption_from_the_caller(fetch, tmp_path: Path) -> None:
    """What is reachable from the bytes is taken rather than asked for twice."""
    from raven_ppt.services.ingest import fetched

    await _run(fetch(_described_png("Figure 4: token cost per turn")))

    (held,) = fetched(Project(workspace=tmp_path, slug="tarvis"))
    assert held.caption == "Figure 4: token cost per turn"


@pytest.mark.asyncio
async def test_an_exif_image_description_is_read_and_its_padding_dropped(fetch, tmp_path: Path) -> None:
    """EXIF strings are NUL-padded, and a NUL in the catalogue makes the file a
    search tool treats as binary."""
    from raven_ppt.services.ingest import fetched

    await _run(fetch(_described_jpeg("Mem0 memory lifecycle")))

    (held,) = fetched(Project(workspace=tmp_path, slug="tarvis"))
    assert held.caption == "Mem0 memory lifecycle"


@pytest.mark.asyncio
async def test_the_pages_words_beat_the_files_own_metadata(fetch, tmp_path: Path) -> None:
    """The caller's caption is what the page printed next to this picture; an
    `ImageDescription` field is whatever the file was exported with."""
    from raven_ppt.services.ingest import fetched

    await _run(fetch(_described_png("stock photo 118372")), caption="Figure 1: the extraction phase")

    (held,) = fetched(Project(workspace=tmp_path, slug="tarvis"))
    assert held.caption == "Figure 1: the extraction phase"


@pytest.mark.asyncio
async def test_a_second_fetch_without_a_caption_does_not_blank_the_first(fetch, tmp_path: Path) -> None:
    """Same URL, twice: a retry, or the same picture wanted for a second page. That is
    not new information about these bytes, so it must not take the words away."""
    from raven_ppt.services.ingest import fetched

    # One tool for both calls: the factory wraps whatever `httpx.AsyncClient` is when
    # it runs, so calling it twice in one test nests the transport into itself.
    tool = fetch(_png(320, 180))
    await _run(tool, caption="Figure 5: end-to-end latency")
    await _run(tool)

    (held,) = fetched(Project(workspace=tmp_path, slug="tarvis"))
    assert held.caption == "Figure 5: end-to-end latency"


@pytest.mark.asyncio
async def test_a_caption_on_a_document_is_refused_out_loud_rather_than_dropped(fetch, tmp_path: Path) -> None:
    """A caption belongs to one picture and a paper holds many, so it is not recorded
    for a document -- and saying so is the difference between this and the silence the
    parameter exists to end. The paper's figures carry the captions its pages print."""
    from raven_ppt.services.ingest import fetched

    body = await _run(fetch(b"%PDF-1.7\n trailer"), caption="Figure 2: architectural overview")

    assert body["ok"] is True and "caption" not in body
    assert "describes one picture" in body["note"]
    assert [held.caption for held in fetched(Project(workspace=tmp_path, slug="tarvis"))] == [""]


@pytest.mark.asyncio
async def test_the_suffix_comes_from_the_bytes_not_the_url(fetch, tmp_path: Path) -> None:
    """A URL ending in .png that returns HTML is ordinary on the open web."""
    body = await _run(fetch(b"<html><body>not an image</body></html>"), url="https://example.com/photo.png")

    assert body["ok"] is True and body["kind"] == "document"
    assert Path(body["path"]).suffix == ".html"


@pytest.mark.asyncio
async def test_a_lying_content_type_does_not_get_a_pass(fetch) -> None:
    body = await _run(fetch(b"\x00\x01\x02 not an image", headers={"content-type": "image/png"}))
    assert body["ok"] is False
    assert "not a PDF, a PowerPoint file, an image, or text" in body["error"]


@pytest.mark.asyncio
async def test_a_pdf_is_recognised_by_its_header(fetch) -> None:
    body = await _run(fetch(b"%PDF-1.7\n trailer"))
    assert body["kind"] == "document" and Path(body["path"]).suffix == ".pdf"


@pytest.mark.asyncio
async def test_a_pptx_lands_in_the_template_slot_not_the_materials(fetch, tmp_path: Path) -> None:
    """A deck is not a source to quote; it is a house style to work inside."""
    body = await _run(fetch(_pptx()), url="https://example.com/house-style.pptx")

    assert body["ok"] is True and body["kind"] == "template"
    project = Project(workspace=tmp_path, slug="tarvis")
    assert Path(body["path"]).parent == project.root / "template"
    assert "house style" in body["next_step"]


@pytest.mark.asyncio
async def test_a_downloaded_template_is_prepared_where_it_lands(fetch, tmp_path: Path) -> None:
    """The two ways a template arrives have to end in the same state. Downloading
    one and leaving it in the slot was the earlier behaviour, and it meant a
    template that came by URL was never prepared, so nothing downstream saw one."""
    from raven_ppt.services.template import bound

    body = await _run(fetch(_pptx()), url="https://example.com/house-style.pptx")

    assert body["ok"] is True
    assert bound(Project(workspace=tmp_path, slug="tarvis")) is not None
    assert body["template"] == "house-style.pptx"


@pytest.mark.asyncio
async def test_a_pptx_that_will_not_open_is_refused_rather_than_stored(fetch, tmp_path: Path) -> None:
    body = await _run(fetch(_not_a_deck()))

    assert body["ok"] is False
    assert "does not open as a presentation" in body["error"]
    assert not list((Project(workspace=tmp_path, slug="tarvis").root / "template").glob("*.pptx"))


@pytest.mark.asyncio
async def test_a_zip_that_is_not_a_presentation_is_refused(fetch) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "hello")
    body = await _run(fetch(buffer.getvalue()))
    assert body["ok"] is False and "not a PowerPoint file" in body["error"]


@pytest.mark.asyncio
async def test_a_declared_size_over_the_limit_is_refused_before_the_body_arrives(fetch) -> None:
    body = await _run(fetch(b"%PDF-1.7", headers={"content-length": str(MAX_BYTES + 1)}))
    assert body["ok"] is False and "32MB" in body["error"]


@pytest.mark.asyncio
async def test_an_undeclared_oversized_body_is_refused_as_it_arrives(fetch) -> None:
    """A server may not declare a length, so the bytes are counted too."""
    body = await _run(fetch(b"%PDF-" + b"x" * (MAX_BYTES + 1)))
    assert body["ok"] is False and "32MB" in body["error"]


@pytest.mark.asyncio
async def test_a_failed_request_says_so_rather_than_writing_nothing_quietly(fetch) -> None:
    body = await _run(fetch(b"nope", status=404))
    assert body["ok"] is False and "download failed" in body["error"]


@pytest.mark.asyncio
async def test_the_download_names_itself_rather_than_sending_the_library_default(monkeypatch, tmp_path: Path) -> None:
    """Wikimedia's hosts answer httpx's own `python-httpx/<version>` with a 403 and
    serve the same file to an agent that names itself and a contact. The handler
    here is that policy, so the fetch has to arrive with a name of its own."""
    agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        agent = request.headers.get("user-agent", "")
        agents.append(agent)
        if agent.startswith("python-httpx"):
            return httpx.Response(403)
        return httpx.Response(200, content=_png())

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("proxy", None)
        return real(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr("raven.security.network.validate_url_target", lambda url: (True, ""))
    monkeypatch.setattr("raven.security.network.judge_url_target", lambda url: (True, "", ()))

    body = await _run(PptFetchTool(tmp_path), url="https://upload.wikimedia.org/wikipedia/commons/a/a4/x.jpg")

    assert body["ok"] is True, body
    (agent,) = agents
    assert agent.startswith("raven-ppt/") and "https://github.com/EverMind-AI/Raven" in agent


@pytest.mark.asyncio
async def test_there_is_no_materials_path_to_point_anywhere(fetch) -> None:
    """The parameter is gone, not defaulted: it existed to let a fetch land somewhere
    other than the deck's sources, and there was never a good reason to."""
    from raven_ppt.tools.fetch import PptFetchTool

    assert "materials_dir" not in PptFetchTool(Path("/tmp")).parameters["properties"]


@pytest.mark.asyncio
async def test_a_bad_project_name_is_refused_before_the_download(fetch) -> None:
    body = await _run(fetch(_png()), project="../etc")
    assert body["ok"] is False and "usable project name" in body["error"]


@pytest.mark.asyncio
async def test_a_hostile_filename_is_reduced_to_something_safe(fetch, tmp_path: Path) -> None:
    body = await _run(fetch(_png()), filename="../../etc/passwd")
    assert Path(body["path"]).parent == Project(workspace=tmp_path, slug="tarvis").sources_dir
    assert Path(body["path"]).name == "passwd.png"


@pytest.mark.asyncio
async def test_a_body_of_control_bytes_is_binary_however_well_it_decodes(fetch) -> None:
    """UTF-8 accepts control characters, so decoding is not a test for text.

    Without this, a truncated video saved as `.md` becomes the ingest's problem
    to explain.
    """
    body = await _run(fetch(bytes(range(1, 32)) * 40))
    assert body["ok"] is False and "not a PDF" in body["error"]


@pytest.mark.asyncio
async def test_an_empty_body_is_not_a_document(fetch) -> None:
    body = await _run(fetch(b"   \n\n  "))
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_ordinary_prose_with_punctuation_and_cjk_is_still_text(fetch) -> None:
    body = await _run(fetch("# 标题\n\n正文 — with an em dash, 90% coverage.\n".encode()))
    assert body["ok"] is True and Path(body["path"]).suffix == ".md"


SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="60" viewBox="0 0 240 60">'
    b'<rect width="240" height="60" fill="#155FFD"/><text x="12" y="40" fill="#fff">mem0</text></svg>'
)


def _needs_cairo() -> None:
    from raven_ppt.tools.fetch import _load_cairosvg

    module, missing = _load_cairosvg()
    if module is None:
        pytest.skip(f"rasterising an SVG needs the native cairo library: {missing}")


def test_a_brand_mark_arrives_as_svg_and_is_kept_as_a_picture() -> None:
    """A logo saved as `.md` is a competitor analysis with no competitor's mark on it.

    Rasterising needs cairo's native library, which `cairosvg` cannot supply itself, so
    this asserts the conversion only where the conversion is possible. The skip goes
    through the same loader the tool uses, so a Homebrew libcairo the plain import
    cannot see no longer skips it. The degraded path is a case of its own below.
    """
    _needs_cairo()
    from raven_ppt.tools.fetch import _sniff

    payload, suffix, kind = _sniff(SVG)

    assert (suffix, kind) == (".png", "image")
    assert payload.startswith(b"\x89PNG"), "python-pptx places rasters only"


def test_an_svg_that_will_not_draw_is_refused_not_kept_as_text() -> None:
    _needs_cairo()
    from raven_ppt.tools.fetch import _sniff

    with pytest.raises(ValueError, match="SVG that would not draw"):
        _sniff(b"<svg this is not really an svg at all")


def test_a_page_with_inline_svg_icons_is_a_page(monkeypatch) -> None:
    """A page is not a picture because an icon sits near its top, and the rasteriser
    is not asked about it -- a missing cairo must not refuse ordinary HTML."""
    from raven_ppt.tools import fetch
    from raven_ppt.tools.fetch import _sniff

    monkeypatch.setattr(fetch, "_load_cairosvg", lambda: pytest.fail("a page reached the rasteriser"))
    page = b'<!doctype html><html><body><svg viewBox="0 0 1 1"><path d="M0 0"/></svg>hi</body></html>'

    payload, suffix, kind = _sniff(page)

    assert (payload, suffix, kind) == (page, ".html", "document")


def test_an_svg_behind_a_declaration_and_comment_is_still_an_svg(monkeypatch) -> None:
    from raven_ppt.tools import fetch
    from raven_ppt.tools.fetch import _sniff

    monkeypatch.setattr(fetch, "_load_cairosvg", lambda: (None, "no cairo here"))
    exported = (
        b'<?xml version="1.0"?>\n<!-- Generator: Sketch -->\n<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN">\n' + SVG
    )

    with pytest.raises(ValueError, match="no cairo here"):
        _sniff(exported)


def test_an_svg_behind_a_byte_order_mark_is_still_an_svg(monkeypatch) -> None:
    """Editors that save UTF-8 with a BOM put it before the root element; read as
    text, it would be kept as path data in a document."""
    from raven_ppt.tools import fetch
    from raven_ppt.tools.fetch import _sniff

    monkeypatch.setattr(fetch, "_load_cairosvg", lambda: (None, "no cairo here"))

    with pytest.raises(ValueError, match="no cairo here"):
        _sniff(b"\xef\xbb\xbf" + SVG)


def _without_libcairo(monkeypatch) -> None:
    """`import cairosvg` pulls in cairocffi, which raises OSError -- not ImportError --
    when the native library is absent."""
    import builtins
    import sys

    real_import = builtins.__import__

    def no_libcairo(name, *args, **kwargs):
        if name == "cairosvg":
            raise OSError('no library called "cairo-2" was found\nno library called "cairo" was found')
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "cairosvg", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_libcairo)


def test_a_machine_without_libcairo_refuses_the_svg_and_says_what_installs_it(monkeypatch) -> None:
    """The SVG used to be kept as a `.md` of path data, with no figure id and no log
    line, so a deck lost its logos while every fetch reported success."""
    from loguru import logger

    from raven.utils import cairo
    from raven_ppt.tools.fetch import _sniff

    _without_libcairo(monkeypatch)
    logged: list[str] = []
    sink = logger.add(logged.append, level="WARNING", format="{message}")
    try:
        with pytest.raises(ValueError) as refused:
            _sniff(SVG)
    finally:
        logger.remove(sink)

    said = str(refused.value)
    assert 'no library called "cairo-2" was found' in said
    assert 'cairo" was found' not in said, "one line of cairocffi's list, not all of it"
    assert cairo.install_hint() in said
    assert any("cairo could not be loaded" in line for line in logged), logged


@pytest.mark.asyncio
async def test_a_refused_svg_leaves_nothing_in_the_decks_sources(fetch, monkeypatch, tmp_path: Path) -> None:
    _without_libcairo(monkeypatch)

    body = await _run(fetch(SVG), url="https://example.com/mem0-logo.svg")

    assert body["ok"] is False and "SVG" in body["error"]
    sources_dir = Project(workspace=tmp_path, slug="tarvis").sources_dir
    assert not sources_dir.exists() or not any(sources_dir.iterdir())


@pytest.mark.asyncio
async def test_a_host_never_reached_names_the_setting_that_would_reach_it(fetch, monkeypatch, tmp_path: Path) -> None:
    """The refusal a run actually got was `the download failed: ` and nothing else.

    `httpx.ConnectTimeout("")` renders empty, so the class was the only thing there
    was to say and it was not said. And the cause is knowable: every client in this
    tool is built `trust_env=False`, so a machine that reaches the internet through
    a proxy goes direct here however its environment is set -- measured at 50s per
    timeout while curl through that same proxy answered in 0.86s.
    """
    from raven_ppt.tools.fetch import PptFetchTool

    tool = PptFetchTool(tmp_path)

    async def _timeout(url):
        raise httpx.ConnectTimeout("")

    monkeypatch.setattr(tool, "_download", _timeout)
    monkeypatch.setattr("raven.security.network.validate_url_target", lambda url: (True, ""))
    said = await _run(tool, url="https://example.com/a.png")

    assert said["ok"] is False
    assert "ConnectTimeout" in said["error"], said["error"]
    assert "tools.web.proxy" in said["hint"]
    assert "HTTPS_PROXY" in said["hint"]

    # A configured proxy means the network is already being routed, so the hint
    # would be wrong; and a status is the server talking, which needs no hint.
    routed = PptFetchTool(tmp_path, proxy="http://127.0.0.1:7890")
    monkeypatch.setattr(routed, "_download", _timeout)
    assert "hint" not in await _run(routed, url="https://example.com/a.png")

    async def _refused(url):
        raise httpx.HTTPStatusError("403", request=None, response=None)

    monkeypatch.setattr(tool, "_download", _refused)
    assert "hint" not in await _run(tool, url="https://example.com/a.png")


@pytest.mark.asyncio
async def test_an_address_the_network_guard_refuses_is_not_downloaded(fetch, monkeypatch, tmp_path: Path) -> None:
    """`validate_url_target` answers, it does not throw.

    The call read it by catching an exception, so every refused address went
    through: loopback, the link-local range, and the cloud metadata endpoint that
    hands out credentials to whoever asks from inside the host.
    """
    tool = fetch(_png())
    # After the factory, which installs the permissive stubs the other cases need.
    monkeypatch.setattr("raven.security.network.validate_url_target", _real_validate)
    monkeypatch.setattr("raven.security.network.judge_url_target", _real_judge)
    out = await _run(tool, url="http://169.254.169.254/latest/meta-data/iam/security-credentials/")

    assert out.get("ok") is False, "the metadata endpoint must not be fetched"
    assert "cannot be fetched" in json.dumps(out), out
    landed = list((tmp_path / "tarvis").rglob("*")) if (tmp_path / "tarvis").exists() else []
    assert not [p for p in landed if p.is_file()], "nothing may be written for a refused address"


@pytest.mark.asyncio
async def test_a_fetched_image_hands_back_the_id_the_catalogue_keys_it_under(fetch, tmp_path: Path) -> None:
    """Nothing else in the reply can be turned into one.

    `ppt_figure_inspect` takes catalogue ids and nothing else, and the catalogue keys a
    picture as `<stem>-<digest>`. A measured run fetched ten images, called inspect with
    the filenames it had just passed to this tool, was told none of them was in the
    catalogue, then guessed at the digests and got their length wrong -- two iterations
    spent recovering a string the fetch already had on disk.
    """
    from raven_ppt.services.ingest import ingest_materials, load_catalogue

    tool = fetch(_png(320, 180))
    tool.ingest = ingest_materials

    body = await _run(tool, url="https://example.com/figure.png", filename="diagram.png")

    deck = Project(workspace=tmp_path, slug="tarvis")
    catalogue = load_catalogue(deck.ingest_dir / "figures.json", figures_dir=deck.ingest_dir / "figures")
    assert body["figure_id"] in catalogue, "the id has to be one inspect will accept"
    assert body["figure_id"].startswith("diagram-")


@pytest.mark.asyncio
async def test_the_id_a_fetch_hands_back_is_one_figure_inspect_accepts(fetch, tmp_path: Path) -> None:
    """End to end across the two tools, because the ids agreeing is the whole claim and
    each tool builds its own view of the catalogue."""
    from raven_ppt.services.ingest import ingest_materials
    from raven_ppt.tools.inspect import PptFigureInspectTool

    tool = fetch(_png(320, 180))
    tool.ingest = ingest_materials
    body = await _run(tool, url="https://example.com/figure.png", filename="diagram.png")

    inspect = PptFigureInspectTool(tmp_path, _Views())
    seen = await inspect.execute(project="tarvis", figures=[body["figure_id"]])
    reply = json.loads(seen.model_text if hasattr(seen, "model_text") else seen)

    assert reply["ok"] is True, reply
    assert [f["figure_id"] for f in reply["figures"]] == [body["figure_id"]]


class _Views:
    """Just the surface `ppt_figure_inspect` asks of the render service."""

    async def pages(self, *_args, **_kwargs):
        return {}

    def data_uri(self, png, budget=None):
        return f"data:image/png;base64,{Path(png).stem}"


def test_a_webp_picture_is_kept_as_a_png_the_deck_can_place() -> None:
    """Image hosts serve WebP; python-pptx places none of them, and the ingest
    refused the file as unreadable -- a run spent three fetches on one picture."""
    import io

    from PIL import Image

    from raven_ppt.tools.fetch import _sniff

    raw = io.BytesIO()
    Image.new("RGB", (12, 8), (200, 40, 40)).save(raw, format="WEBP")

    payload, suffix, kind = _sniff(raw.getvalue())

    assert (suffix, kind) == (".png", "image")
    assert payload.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(payload)) as png:
        assert png.size == (12, 8)


def test_a_webp_keeps_the_description_it_carried_across_the_conversion() -> None:
    """A fetch without a caption falls back to what the file says about itself, and a
    re-encode that dropped the EXIF block would silently cost that provenance."""
    import io

    from PIL import Image

    from raven_ppt.tools.fetch import _embedded_caption, _sniff

    image = Image.new("RGB", (12, 8), (40, 40, 200))
    exif = image.getexif()
    exif[0x010E] = "A red brand mark"
    raw = io.BytesIO()
    image.save(raw, format="WEBP", exif=exif.tobytes())
    assert _embedded_caption(raw.getvalue()) == "A red brand mark"

    payload, _suffix, _kind = _sniff(raw.getvalue())

    assert _embedded_caption(payload) == "A red brand mark"


@pytest.mark.asyncio
async def test_several_urls_are_fetched_in_one_call(fetch, tmp_path: Path) -> None:
    """A live run spent nine model round trips on nine fetches, minutes each on that
    model; one call with the list fetches them together and ingests once."""
    tool = fetch(_png())

    body = json.loads(
        await tool.execute(project="tarvis", urls=["https://example.com/a.png", "https://example.com/b.png"])
    )

    assert body["ok"] and len(body["results"]) == 2
    assert all(item["ok"] for item in body["results"])
    assert {Path(item["path"]).name for item in body["results"]} == {"a.png", "b.png"}
    assert "urls" not in body


@pytest.mark.asyncio
async def test_two_sources_whose_paths_end_the_same_do_not_overwrite_each_other(fetch, tmp_path: Path) -> None:
    """`one.example/image.png` and `two.example/image.png` both landed on
    `sources/image.png`: two rows reported success and `receive` replaced the manifest
    row and the bytes, so ingestion saw one source. The url's digest tells them apart.
    """
    tool = fetch(_png())

    body = json.loads(
        await tool.execute(project="tarvis", urls=["https://one.example/image.png", "https://two.example/image.png"])
    )

    assert body["ok"] and len(body["results"]) == 2
    names = [Path(item["path"]).name for item in body["results"]]
    assert len(set(names)) == 2, names
    assert all(name.startswith("image-") and name.endswith(".png") for name in names), names
    from raven_ppt.contracts import Project
    from raven_ppt.services.ingest import sources

    held = {source.origin for source in sources.held(Project(workspace=tmp_path, slug="tarvis"))}
    assert held == {"fetch:https://one.example/image.png", "fetch:https://two.example/image.png"}


@pytest.mark.asyncio
async def test_two_urls_whose_names_normalize_alike_are_told_apart_too(fetch, tmp_path: Path) -> None:
    """The collision is in the destination, not in the url. `_safe_name` folds every
    character outside its allowlist into an underscore, so `chart+final.png` and
    `chart@final.png` are two basenames and one file; grouping on the raw basename saw
    no collision and let the second overwrite the first."""
    tool = fetch(_png())

    body = json.loads(
        await tool.execute(
            project="tarvis", urls=["https://one.example/chart+final.png", "https://two.example/chart@final.png"]
        )
    )

    assert body["ok"] and len(body["results"]) == 2
    paths = [Path(item["path"]) for item in body["results"]]
    assert len({path.name for path in paths}) == 2, paths
    assert all(path.is_file() for path in paths)
    from raven_ppt.contracts import Project
    from raven_ppt.services.ingest import sources

    held = {source.origin for source in sources.held(Project(workspace=tmp_path, slug="tarvis"))}
    assert held == {"fetch:https://one.example/chart+final.png", "fetch:https://two.example/chart@final.png"}


def test_a_mark_survives_the_name_it_is_added_to() -> None:
    """`_safe_name` strips what it reads as an extension from the name it is given, so
    a mark appended after one -- `image.tar` becoming `image.tar-1a2b3c4d` -- came back
    as `image`, and the two urls were on one destination again."""
    from raven_ppt.tools.fetch import _batch_names, _safe_name

    urls = ["https://one.example/image.tar.gz", "https://two.example/image.tar.bz2"]
    names = _batch_names(urls)

    assert len({_safe_name(url, names[url], ".png") for url in urls}) == 2


@pytest.mark.asyncio
async def test_a_batch_without_a_collision_keeps_the_names_the_urls_gave(fetch, tmp_path: Path) -> None:
    tool = fetch(_png())

    body = json.loads(
        await tool.execute(project="tarvis", urls=["https://example.com/a.png", "https://example.com/b.png"])
    )

    assert {Path(item["path"]).name for item in body["results"]} == {"a.png", "b.png"}


# ---------------------------------------------------------------------------
# Redirects: the first hop being public says nothing about the next one.
# ---------------------------------------------------------------------------


def _named(request: httpx.Request) -> str:
    """The URL a recorded request names, rather than the address it connected to.

    A pinned hop reaches the transport with the judged address in the URL and the
    site's name in ``Host``; reading the two back together is how a test says
    which page was asked for.
    """
    host = request.headers.get("host") or request.url.netloc.decode("ascii")
    query = f"?{request.url.query.decode('ascii')}" if request.url.query else ""
    return f"{request.url.scheme}://{host}{request.url.path}{query}"


@pytest.fixture()
def fetch_routed(monkeypatch, tmp_path: Path):
    """A tool whose fake network routes by URL and records every request it sends.

    The address judge is the production one and only DNS is faked, so a hop is
    allowed or refused by the code that ships rather than by a stub. The record is
    what separates "refused before the request" from "refused after it went out":
    only the first is a fix, because the second has already handed the address what
    it asked for.
    """

    def make(handler):
        sent: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return handler(request)

        transport = httpx.MockTransport(recording)
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs.pop("proxy", None)
            return real(*args, transport=transport, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        monkeypatch.setattr("socket.getaddrinfo", lambda *_a, **_k: [(0, 0, 0, "", ("93.184.216.34", 0))])
        return PptFetchTool(tmp_path), sent

    return make


@pytest.mark.asyncio
async def test_a_redirect_toward_the_metadata_endpoint_is_refused_before_it_is_requested(fetch_routed) -> None:
    """A public first hop says nothing about the second. The metadata endpoint hands
    credentials to whoever connects from inside the host, so a request that reaches
    it has already leaked them -- refusing after the fact is not a refusal."""
    metadata = "http://169.254.169.254/latest/meta-data/"

    def handler(request: httpx.Request) -> httpx.Response:
        if _named(request) == "https://public.example/x":
            return httpx.Response(302, headers={"location": metadata})
        return httpx.Response(200, content=b"SECRET")

    tool, sent = fetch_routed(handler)
    body = await _run(tool, url="https://public.example/x")

    assert body["ok"] is False, body
    assert "cannot be fetched" in body["error"] and "private address" in body["error"], body["error"]
    assert [_named(request) for request in sent] == ["https://public.example/x"], (
        "the second hop must never be contacted; a refusal it already answered is still a leak"
    )


@pytest.mark.parametrize(
    "redirect_to",
    ["http://10.1.2.3/secret", "http://127.0.0.1/secret", "http://[::1]/secret"],
    ids=["private-v4", "loopback-v4", "loopback-v6"],
)
@pytest.mark.asyncio
async def test_a_redirect_into_a_private_network_is_refused_before_it_is_requested(fetch_routed, redirect_to) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _named(request).startswith("https://public.example/"):
            return httpx.Response(302, headers={"location": redirect_to})
        return httpx.Response(200, content=b"SECRET")

    tool, sent = fetch_routed(handler)
    body = await _run(tool, url="https://public.example/x")

    assert body["ok"] is False, body
    assert len(sent) == 1, [str(request.url) for request in sent]


@pytest.mark.asyncio
async def test_a_relative_location_is_resolved_against_the_hop_it_came_from(fetch_routed) -> None:
    """A relative Location is legal HTTP. Joining it against the URL it came from
    keeps every hop absolute, which is what makes the next hop checkable -- and
    keeps the legitimate redirect working."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dir/source":
            return httpx.Response(302, headers={"location": "../moved/figure.png"})
        return httpx.Response(200, content=_png())

    tool, sent = fetch_routed(handler)
    body = await _run(tool, url="https://public.example/dir/source")

    assert body["ok"] is True, body
    assert [_named(request) for request in sent] == [
        "https://public.example/dir/source",
        "https://public.example/moved/figure.png",
    ], "the relative target was joined against the URL it came from"


@pytest.mark.asyncio
async def test_a_chain_of_public_redirects_is_followed(fetch_routed) -> None:
    moves = {
        "https://public.example/a": "https://public.example/b",
        "https://public.example/b": "https://public.example/c",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if (target := moves.get(_named(request))) is not None:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, content=_png())

    tool, sent = fetch_routed(handler)
    body = await _run(tool, url="https://public.example/a")

    assert body["ok"] is True, body
    assert [_named(request) for request in sent] == [
        "https://public.example/a",
        "https://public.example/b",
        "https://public.example/c",
    ]


@pytest.mark.asyncio
async def test_a_redirect_chain_longer_than_the_cap_is_refused_instead_of_followed_forever(fetch_routed) -> None:
    """The chain has to end. Beyond the cap the handler answers, so an unbounded
    follower succeeds and this fails on that success instead of hanging."""
    from raven_ppt.tools.fetch import MAX_REDIRECTS

    served = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal served
        served += 1
        if served > MAX_REDIRECTS + 1:
            return httpx.Response(200, content=_png())
        return httpx.Response(302, headers={"location": "/next"})

    tool, sent = fetch_routed(handler)
    body = await _run(tool, url="https://public.example/start")

    assert body["ok"] is False, body
    assert "redirected more than" in body["error"], body["error"]
    assert len(sent) == MAX_REDIRECTS + 1, "one initial request plus the cap, and not one more"


@pytest.mark.asyncio
async def test_a_redirect_to_a_non_http_scheme_is_refused(fetch_routed) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    tool, sent = fetch_routed(handler)
    body = await _run(tool, url="https://public.example/x")

    assert body["ok"] is False, body
    assert "not http/https" in body["error"], body["error"]
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# Pinning: the hop connects to the address the judge saw.
#
# A MockTransport never resolves anything, so it cannot see this defect at all:
# the window is between the judge's resolution and the one the client does when
# it connects. These tests use a real client, a resolver that answers public to
# the judge and loopback to a connection, and record the address the network
# backend is asked to connect to. The seam is httpcore's ``connect_tcp`` rather
# than ``socket.socket.connect``: it sits above the Proactor/selector split, so
# the same recorder sees the connection on Windows, where the Proactor loop
# connects through ``ConnectEx`` and never calls ``socket.connect``.
# ---------------------------------------------------------------------------

_PUBLIC_ANSWER = "93.184.216.34"


@pytest.fixture()
def rebinding_dns(monkeypatch):
    """A resolver whose answer depends on who is asking.

    The judge asks with no port and the connection asks with one, so the two
    answers are told apart without counting calls. That is the rebinding shape
    itself: the same name, a different address by the time anything connects.
    """
    real = socket.getaddrinfo

    def resolver(host, port, *_args, **_kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name == "rebind.test":
            answer = _PUBLIC_ANSWER if port is None else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (answer, port or 0))]
        return real(host, port, *_args, **_kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    return _PUBLIC_ANSWER


@pytest.mark.asyncio
async def test_the_first_hop_connects_to_the_address_the_judge_saw(rebinding_dns, monkeypatch, tmp_path: Path) -> None:
    """The first hop resolves once, so the second answer is never used.

    Before pinning, the judge saw the public address and httpx resolved the name
    again when it connected -- and a name that answers loopback the second time
    put the request inside the host while the check had said it was public.
    """
    public = rebinding_dns
    attempts: list[tuple] = []

    async def recording_connect(_backend, host, port, **_kwargs):
        attempts.append((host, port))
        raise httpcore.ConnectError("connection refused")

    monkeypatch.setattr(AutoBackend, "connect_tcp", recording_connect)

    body = await _run(PptFetchTool(tmp_path), url="https://rebind.test/start")

    assert body["ok"] is False, body
    assert attempts == [(public, 443)], f"the connection must go to the judged address, got {attempts}"


@pytest.mark.asyncio
async def test_every_redirect_hop_connects_to_the_address_its_judge_saw(
    rebinding_dns, monkeypatch, tmp_path: Path
) -> None:
    """A second hop resolves for itself, so it has to be pinned for itself too.

    The first hop is public on both lookups; only the redirect target's
    connection is answered with loopback. A fix that pins the first hop alone
    still follows that redirect inward.
    """
    public = rebinding_dns
    served: list[str] = []

    class Redirects(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            served.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("location", "http://rebind.test/next")
                self.end_headers()
                return
            body = b"SECRET"
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirects)
    local_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    attempts: list[tuple] = []
    real_connect = AutoBackend.connect_tcp

    async def recording_connect(backend, host, port, **kwargs):
        if port != 80:
            return await real_connect(backend, host, port, **kwargs)
        attempts.append((host, port))
        return await real_connect(backend, "127.0.0.1", local_port, **kwargs)

    monkeypatch.setattr(AutoBackend, "connect_tcp", recording_connect)
    try:
        body = await _run(PptFetchTool(tmp_path), url="http://rebind.test/start")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert body["ok"] is True, body
    assert served == ["/start", "/next"], served
    assert attempts == [(public, 80), (public, 80)], f"both hops must connect to the judged address, got {attempts}"
    assert all(address[0] != "127.0.0.1" for address in attempts), attempts


@pytest.mark.asyncio
async def test_a_pinned_hop_still_answers_as_the_name_it_was_given(fetch_routed) -> None:
    """The TCP target is pinned; the request is not renamed. Host and SNI have to
    keep the site's name, or a virtual host answers for the wrong site and the
    certificate check runs against an address."""
    tool, sent = fetch_routed(lambda _request: httpx.Response(200, content=_png()))

    body = await _run(tool, url="https://public.example/figure.png")

    assert body["ok"] is True, body
    (request,) = sent
    assert request.url.host == _PUBLIC_ANSWER, "the URL carries the address the judge saw"
    assert request.headers["host"] == "public.example", "Host keeps the site's name"
    assert request.extensions["sni_hostname"] == "public.example", "SNI keeps the site's name"


# ---------------------------------------------------------------------------
# The judge resolves synchronously, so it runs off the event loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_hop_judge_does_not_block_the_event_loop(fetch, monkeypatch) -> None:
    """A slow resolver must not freeze the turns this process is serving.

    ``socket.getaddrinfo`` is synchronous and can hold for its timeout. Counting
    the ticks a sibling coroutine gets while the judge is inside that call is the
    difference between running it in a worker thread and blocking the loop with
    it: a blocked loop lets the ticker run zero times while the judge is in
    flight, however long the judge takes.
    """
    judging = threading.Event()
    ticks_while_judging = 0
    tool = fetch(_png())

    def slow_judge(_url):
        judging.set()
        time.sleep(0.2)
        judging.clear()
        return True, "", ()

    monkeypatch.setattr("raven.security.network.judge_url_target", slow_judge)
    monkeypatch.setattr("raven.security.network.judge_resolved_url", slow_judge)

    async def ticker() -> None:
        nonlocal ticks_while_judging
        while True:
            if judging.is_set():
                ticks_while_judging += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        body = await _run(tool, url="https://example.com/figure")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert body["ok"] is True, body
    assert ticks_while_judging > 1, (
        "the event loop must keep running while the judge resolves; "
        f"a sibling coroutine got only {ticks_while_judging} turns"
    )
