"""The viewer's file endpoint: who may read what, and how it is served.

The path policy is the agent's own (``raven.agent.tools.filesystem.resolve_path``
with the configured workspace and ``tools.restrict_to_workspace``), so these
tests pin the wiring rather than a second policy: what the agent may read, the
viewer serves; what it may not, the viewer refuses.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from raven.agent.tools.deliverables import DeliverableStore
from raven.config import load_config
from raven.rpc import files as files_module
from raven.rpc.transports.ws import WsGateway, build_app

TOKEN = "test-token"


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    """A serve app whose workspace is tmp_path, reachable with TOKEN."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    cfg.tools.restrict_to_workspace = False
    monkeypatch.setattr(files_module, "load_config", lambda: cfg)
    monkeypatch.setenv("RAVEN_SERVE_TOKEN", TOKEN)
    gateway = WsGateway()
    server = TestServer(build_app(gateway, None))
    c = TestClient(server)
    await c.start_server()
    c.raven_cfg = cfg  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        await c.close()


def auth() -> dict[str, str]:
    return {"X-Raven-Token": TOKEN}


async def test_file_serves_a_workspace_file(client: TestClient, tmp_path: Path) -> None:
    """A file the agent just wrote opens, as UTF-8 text, inline."""
    (tmp_path / "notes.md").write_text("# hi\n\n- one\n", encoding="utf-8")

    r = await client.get("/file", params={"path": str(tmp_path / "notes.md")}, headers=auth())

    assert r.status == 200
    assert r.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert r.headers["Content-Disposition"] == "inline"
    assert await r.text() == "# hi\n\n- one\n"


async def test_file_relative_path_uses_the_session_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.tools.restrict_to_workspace = False
    monkeypatch.setattr(files_module, "load_config", lambda: cfg)
    session_root = tmp_path / "project"
    session_root.mkdir()
    (session_root / "notes.md").write_text("# session\n", encoding="utf-8")

    class Loop:
        def peek_session_workdir(self, session: str) -> Path:
            assert session == "tui:session"
            return session_root

    monkeypatch.setenv("RAVEN_SERVE_TOKEN", TOKEN)
    gateway = WsGateway()
    server = TestServer(build_app(gateway, None, agent_loop_factory=lambda: Loop()))
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.get(
            "/file",
            params={"path": "notes.md", "session": "tui:session"},
            headers=auth(),
        )
        assert response.status == 200
        assert await response.text() == "# session\n"
    finally:
        await client.close()


async def test_file_response_is_sandboxed(client: TestClient, tmp_path: Path) -> None:
    """HTML and SVG the agent produced must not run with the page's origin.

    Without the sandbox directive an artifact opened same-origin could reach the
    session cookie and the RPC socket, so the header is the load-bearing part of
    this endpoint -- not decoration.
    """
    (tmp_path / "report.html").write_text("<h1>hi</h1>", encoding="utf-8")

    r = await client.get("/file", params={"path": str(tmp_path / "report.html")}, headers=auth())

    assert r.status == 200
    assert r.headers["Content-Security-Policy"] == "sandbox"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_pdf_is_sandboxed_but_may_run_its_viewer(client: TestClient, tmp_path: Path) -> None:
    """A PDF needs allow-scripts, and must still get an opaque origin.

    The browser's PDF viewer is script-driven: denied scripts it renders a blank
    frame. Granting allow-scripts without allow-same-origin keeps the frame off
    this page's origin, which is the property that matters.
    """
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    r = await client.get("/file", params={"path": str(tmp_path / "doc.pdf")}, headers=auth())

    assert r.status == 200
    csp = r.headers["Content-Security-Policy"]
    assert csp == "sandbox allow-scripts"
    assert "allow-same-origin" not in csp


async def test_file_needs_a_session(client: TestClient, tmp_path: Path) -> None:
    """No cookie and no token: the endpoint is not a public file server."""
    (tmp_path / "secret.txt").write_text("s", encoding="utf-8")

    r = await client.get("/file", params={"path": str(tmp_path / "secret.txt")})

    assert r.status == 401


async def test_file_relative_path_resolves_against_the_workspace(client: TestClient, tmp_path: Path) -> None:
    """The transcript carries workspace-relative paths, so those must open too."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.txt").write_text("a", encoding="utf-8")

    r = await client.get("/file", params={"path": "out/a.txt"}, headers=auth())

    assert r.status == 200
    assert await r.text() == "a"


async def test_file_outside_workspace_is_served_when_the_agent_may_read_it(client: TestClient, tmp_path: Path) -> None:
    """restrict_to_workspace is off by default, and the viewer follows the agent.

    Confining the viewer on its own would make it useless for the common case:
    the agent edits a file in the user's project, which is not the workspace.
    """
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("o", encoding="utf-8")
    try:
        r = await client.get("/file", params={"path": str(outside)}, headers=auth())
        assert r.status == 200
    finally:
        outside.unlink()


async def test_file_outside_workspace_is_refused_when_the_agent_is_confined(client: TestClient, tmp_path: Path) -> None:
    """With restrict_to_workspace on, the viewer refuses what the agent cannot read."""
    client.raven_cfg.tools.restrict_to_workspace = True  # type: ignore[attr-defined]
    outside = tmp_path.parent / "outside2.txt"
    outside.write_text("o", encoding="utf-8")
    try:
        r = await client.get("/file", params={"path": str(outside)}, headers=auth())
        assert r.status == 403
    finally:
        outside.unlink()


async def test_file_traversal_is_resolved_before_the_check(client: TestClient, tmp_path: Path) -> None:
    """`..` is not a path component the check can be talked out of."""
    client.raven_cfg.tools.restrict_to_workspace = True  # type: ignore[attr-defined]
    outside = tmp_path.parent / "outside3.txt"
    outside.write_text("o", encoding="utf-8")
    try:
        r = await client.get("/file", params={"path": f"../{outside.name}"}, headers=auth())
        assert r.status == 403
    finally:
        outside.unlink()


async def test_file_missing_and_directory_are_not_found(client: TestClient, tmp_path: Path) -> None:
    """A directory is not viewable, and neither is a path that is not there."""
    (tmp_path / "dir").mkdir()

    missing = await client.get("/file", params={"path": str(tmp_path / "nope.txt")}, headers=auth())
    directory = await client.get("/file", params={"path": str(tmp_path / "dir")}, headers=auth())

    assert missing.status == 404
    assert directory.status == 404


async def test_file_without_a_path_is_a_bad_request(client: TestClient) -> None:
    r = await client.get("/file", headers=auth())

    assert r.status == 400


async def test_file_too_large_is_refused(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the cap no renderer in the page does anything useful with the bytes."""
    monkeypatch.setattr(files_module, "MAX_VIEW_BYTES", 8)
    (tmp_path / "big.txt").write_text("0123456789", encoding="utf-8")

    r = await client.get("/file", params={"path": str(tmp_path / "big.txt")}, headers=auth())

    assert r.status == 413


async def test_delivered_file_uses_its_capability_route_without_the_viewer_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAVEN_SERVE_TOKEN", TOKEN)
    path = tmp_path / "large.bin"
    path.write_bytes(b"0123456789")
    store = DeliverableStore(tmp_path / "deliverables.json")
    record = store.register(
        path=str(path),
        name=path.name,
        media_type="application/octet-stream",
        size=path.stat().st_size,
        conversation="tui:default",
    )
    gateway = WsGateway()
    server = TestServer(build_app(gateway, None, deliverables=store))
    client = TestClient(server)
    await client.start_server()
    try:
        denied = await client.head("/files/download", params={"token": record.token})
        served = await client.head("/files/download", params={"token": record.token}, headers=auth())
        assert denied.status == 401
        assert served.status == 200
        assert served.headers["Content-Disposition"].startswith("attachment;")
    finally:
        await client.close()


async def test_content_type_for_known_binaries(tmp_path: Path) -> None:
    """An image opens as an image; an unknown extension does not pose as text."""
    assert files_module.content_type_for(tmp_path / "a.png") == "image/png"
    assert files_module.content_type_for(tmp_path / "a.pdf") == "application/pdf"
    assert files_module.content_type_for(tmp_path / "a.bin") == "application/octet-stream"
    assert files_module.content_type_for(tmp_path / "a.py") == "text/plain; charset=utf-8"


# ---------------------------------------------------------------------------
# render=pdf: a deck is shown as the PDF LibreOffice makes of it
# ---------------------------------------------------------------------------

FAKE_PDF = b"%PDF-1.4 fake\n%%EOF\n"
FAKE_PNG = b"\x89PNG fake first page"

_FAKE_SOFFICE = """#!/bin/sh
# The fixture narrows PATH to the fake alone, so the fake names its own tools.
PATH=/usr/bin:/bin
echo "$@" >> "{log}"
mode=$(cat "{mode}")
outdir=""
prev=""
for a in "$@"; do
  [ "$prev" = "--outdir" ] && outdir=$a
  prev=$a
  last=$a
done
stem=$(basename "$last" .pptx)
fmt=pdf
prev=""
for a in "$@"; do
  [ "$prev" = "--convert-to" ] && fmt=$a
  prev=$a
done
write() {{
  if [ "$fmt" = "png" ]; then printf '\\211PNG fake first page' > "$outdir/$stem.png"
  else printf '%%PDF-1.4 fake\\n%%%%EOF\\n' > "$outdir/$stem.pdf"; fi
}}
case $mode in
  hang) sleep 30 ;;
  slow) sleep 0.4; write ;;
  empty) echo "failed to launch javaldx; Error: source file could not be loaded" >&2 ;;
  *) write ;;
esac
"""


class FakeSoffice:
    """A `soffice` on PATH that behaves as the mode file says, and keeps a log."""

    def __init__(self, root: Path) -> None:
        root.mkdir()
        self.log = root / "soffice.log"
        self.mode = root / "soffice.mode"
        self.mode.write_text("ok")
        self.bin = root / "bin"
        self.bin.mkdir()
        script = self.bin / "soffice"
        script.write_text(_FAKE_SOFFICE.format(log=self.log, mode=self.mode))
        script.chmod(0o755)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def soffice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSoffice:
    from raven.rpc import pdf_preview

    fake = FakeSoffice(tmp_path / "fake-office")
    monkeypatch.setenv("PATH", str(fake.bin))
    monkeypatch.setattr(pdf_preview, "cache_dir", lambda: tmp_path / "state" / "pdf-preview")
    monkeypatch.setattr(pdf_preview, "_locks", {})
    return fake


def _deck(tmp_path: Path, name: str = "deck.pptx", payload: bytes = b"PK\x03\x04 slides") -> Path:
    deck = tmp_path / name
    deck.write_bytes(payload)
    return deck


async def _render(client: TestClient, deck: Path):
    return await client.get("/file", params={"path": str(deck), "render": "pdf"}, headers=auth())


async def test_a_deck_is_rendered_and_served_as_a_pdf(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    """The viewer asks for a PDF of a deck and gets one, with the PDF's own treatment."""
    deck = _deck(tmp_path)

    r = await _render(client, deck)

    assert r.status == 200
    assert r.headers["Content-Type"] == "application/pdf"
    assert r.headers["Content-Security-Policy"] == "sandbox allow-scripts"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert await r.read() == FAKE_PDF
    [call] = soffice.calls()
    assert "--headless" in call and "--convert-to pdf" in call and str(deck) in call
    # A profile of its own, so a second LibreOffice never hands its job to a
    # running instance and exits 0 having written nothing.
    assert "-env:UserInstallation=file://" in call
    assert "--norestore" in call


async def _thumb(client: TestClient, deck: Path):
    return await client.get("/file", params={"path": str(deck), "render": "thumb"}, headers=auth())


async def test_a_deck_tile_gets_its_first_page_as_a_png(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice
) -> None:
    """The delivery tile asks for one picture of a deck, and LibreOffice's PNG export
    is the first page alone; the same road as the PDF, a different target."""
    deck = _deck(tmp_path)

    r = await _thumb(client, deck)

    assert r.status == 200
    assert r.headers["Content-Type"] == "image/png"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert await r.read() == FAKE_PNG
    [call] = soffice.calls()
    assert "--convert-to png" in call and str(deck) in call


async def test_the_thumb_and_the_pdf_are_cached_apart(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    """One deck, two renderings: each is made once, and neither answers for the other."""
    deck = _deck(tmp_path)

    thumb = await _thumb(client, deck)
    pdf = await _render(client, deck)
    again = await _thumb(client, deck)

    assert thumb.status == pdf.status == again.status == 200
    assert await pdf.read() == FAKE_PDF
    assert await again.read() == FAKE_PNG
    calls = soffice.calls()
    assert len(calls) == 2
    assert sum("--convert-to png" in c for c in calls) == 1 and sum("--convert-to pdf" in c for c in calls) == 1


async def test_a_pdf_tile_gets_its_first_page_without_libreoffice(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF is already a rendering: its tile picture comes from rasterising page one,
    not from LibreOffice, which cannot load most PDFs anyway. The pdf render route
    still refuses it, since there is nothing to convert."""
    from raven.rpc import pdf_preview

    drawn: list[tuple[Path, int, float]] = []

    def rasterise(source: Path, target: Path, width: int, timeout_s: float) -> None:
        drawn.append((source, width, timeout_s))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(FAKE_PNG)

    monkeypatch.setattr(pdf_preview, "_rasterise_pdf_page", rasterise)
    report = _deck(tmp_path, "report.pdf", FAKE_PDF)

    thumb = await _thumb(client, report)
    again = await _thumb(client, report)
    as_pdf = await _render(client, report)

    assert thumb.status == again.status == 200
    assert thumb.headers["Content-Type"] == "image/png"
    assert await thumb.read() == FAKE_PNG
    assert drawn == [(report, pdf_preview.THUMB_WIDTH_PX, pdf_preview.CONVERT_TIMEOUT_S)], (
        "drawn once, with the route's budget, then read from the cache"
    )
    assert soffice.calls() == [], "LibreOffice is not asked about a PDF"
    assert as_pdf.status == 400


def test_a_pdf_page_is_drawn_by_pymupdf_when_it_is_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "cache_dir", lambda: tmp_path / "cache")
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=225)
    page.draw_rect(pymupdf.Rect(20, 20, 380, 205), color=(0, 0, 1), fill=(0.9, 0.9, 1))
    source = tmp_path / "one.pdf"
    doc.save(source)
    target = tmp_path / "cache" / "one.png"

    pdf_preview._rasterise_pdf_page(source, target, 640)

    drawn = pymupdf.Pixmap(str(target))
    assert (drawn.width, drawn.height) == (640, 360)
    assert not list((tmp_path / "cache").glob("render-*")), "the scratch directory is gone"


def test_a_tall_page_is_drawn_small_rather_than_expensively(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page's height is a number inside the file, so a width alone bounds
    nothing: a legal 519-byte PDF declaring a 72 x 14400 point page draws 328
    megapixels at the tile width, which is 1.4 GB of memory for one thumbnail.
    The whole page is scaled down past the ceiling instead."""
    pymupdf = pytest.importorskip("pymupdf")
    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "cache_dir", lambda: tmp_path / "cache")
    doc = pymupdf.open()
    doc.new_page(width=72, height=14400)
    source = tmp_path / "tall.pdf"
    doc.save(source)
    assert source.stat().st_size < 2000, "the cost is not in the file"

    target = tmp_path / "cache" / "tall.png"
    pdf_preview._rasterise_pdf_page(source, target, pdf_preview.THUMB_WIDTH_PX)

    drawn = pymupdf.Pixmap(str(target))
    # The ceiling is on the area the scale asks for; each drawn side is then
    # rounded up to a whole pixel, which is the row and column of slack here.
    assert drawn.width * drawn.height <= pdf_preview.THUMB_MAX_PIXELS + drawn.width + drawn.height
    assert drawn.width * drawn.height < 5_000_000, "far under the 328 megapixels this page used to draw"
    assert drawn.width < pdf_preview.THUMB_WIDTH_PX, "the width came down with the height"
    # An ordinary page is untouched by the ceiling.
    assert pdf_preview._thumb_zoom(612, 792, 1280) == pytest.approx(1280 / 612)


def test_a_rasteriser_that_fails_its_own_way_is_still_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PyMuPDF refuses a page past its own limits with an exception of its own,
    and the fallback can raise from the subprocess module. Neither is foreseeable
    here by type; both must reach the route as the render error it answers for."""
    pymupdf = pytest.importorskip("pymupdf")
    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "cache_dir", lambda: tmp_path / "cache")
    doc = pymupdf.open()
    doc.new_page(width=200, height=100)
    source = tmp_path / "one.pdf"
    doc.save(source)

    class _MupdfLimit(Exception):
        """Stands in for pymupdf.mupdf.FzErrorLimit, which is not an OSError."""

    real_open = pymupdf.open

    def refusing(path):
        doc = real_open(path)

        class _Refuses:
            page_count = 1

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                doc.close()
                return None

            def __getitem__(self, n):
                raise _MupdfLimit("integer out of range")

        return _Refuses()

    monkeypatch.setattr(pymupdf, "open", refusing)
    with pytest.raises(pdf_preview.PdfPreviewError, match="could not be drawn"):
        pdf_preview._rasterise_pdf_page(source, tmp_path / "cache" / "x.png", 640)


def test_a_pdf_page_says_what_went_wrong_rather_than_leaking_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three ways drawing fails, each named: a PDF with no pages, a rasteriser
    that dies mid-draw, and the fallback writing nothing."""
    pymupdf = pytest.importorskip("pymupdf")
    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "cache_dir", lambda: tmp_path / "cache")
    doc = pymupdf.open()
    doc.new_page(width=200, height=100)
    source = tmp_path / "one.pdf"
    doc.save(source)

    # A PDF with no pages cannot be written by this library, so the reader is
    # one that answers zero: a file that arrived from somewhere else.
    class _Empty:
        page_count = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(pymupdf, "open", lambda path: _Empty())
    with pytest.raises(pdf_preview.PdfPreviewError, match="has no pages"):
        pdf_preview._rasterise_pdf_page(source, tmp_path / "cache" / "a.png", 640)

    def explode(path):
        raise OSError("the file went away")

    monkeypatch.setattr(pymupdf, "open", explode)
    with pytest.raises(pdf_preview.PdfPreviewError, match="could not be drawn"):
        pdf_preview._rasterise_pdf_page(source, tmp_path / "cache" / "b.png", 640)
    assert not list((tmp_path / "cache").glob("render-*")), "the scratch directory is gone either way"


def _without_pymupdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """The supported host shape the fallback exists for: poppler, no PyMuPDF."""
    import builtins
    import sys

    real_import = builtins.__import__

    def no_pymupdf(name, *args, **kwargs):
        if name in ("pymupdf", "fitz"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pymupdf)
    monkeypatch.delitem(sys.modules, "pymupdf", raising=False)
    monkeypatch.delitem(sys.modules, "fitz", raising=False)


def test_the_fallback_is_bounded_by_the_same_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`-scale-to-x W -scale-to-y -1` is the flag pair a tall page abuses: the
    height it leaves to the ratio is a number inside the file. Given the page's
    size both sides are computed from the bounded scale; without it the page is
    fitted into a square, which bounds the area either way."""
    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "cache_dir", lambda: tmp_path / "cache")
    tall = tmp_path / "tall.pdf"
    tall.write_bytes(FAKE_PDF)

    monkeypatch.setattr(pdf_preview, "_pdf_page_size", lambda source: (72.0, 14400.0))
    argv = pdf_preview._scale_argv(tall, pdf_preview.THUMB_WIDTH_PX)
    assert argv[0::2] == ["-scale-to-x", "-scale-to-y"], "both sides are stated, none left to the page"
    drawn = int(argv[1]) * int(argv[3])
    assert drawn <= pdf_preview.THUMB_MAX_PIXELS + int(argv[1]) + int(argv[3])
    assert "-1" not in argv, "nothing is left for the file to decide"

    monkeypatch.setattr(pdf_preview, "_pdf_page_size", lambda source: (612.0, 792.0))
    letter = pdf_preview._scale_argv(tall, pdf_preview.THUMB_WIDTH_PX)
    assert letter[1] == str(pdf_preview.THUMB_WIDTH_PX), "an ordinary page still gets the tile width"

    monkeypatch.setattr(pdf_preview, "_pdf_page_size", lambda source: None)
    boxed = pdf_preview._scale_argv(tall, pdf_preview.THUMB_WIDTH_PX)
    assert boxed == ["-scale-to", str(pdf_preview.THUMB_BOX_PX)]
    assert pdf_preview.THUMB_BOX_PX**2 <= pdf_preview.THUMB_MAX_PIXELS


def test_the_page_size_is_read_from_poppler_or_answered_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every way the reading can fail answers None, which is the square: a host
    without pdfinfo, a run that cannot start, and output that says nothing."""
    import subprocess

    from raven.rpc import pdf_preview

    source = tmp_path / "one.pdf"
    source.write_bytes(FAKE_PDF)

    monkeypatch.setattr(pdf_preview.shutil, "which", lambda name: None)
    assert pdf_preview._pdf_page_size(source) is None

    monkeypatch.setattr(pdf_preview.shutil, "which", lambda name: "/usr/bin/pdfinfo")

    class _Out:
        def __init__(self, text: str) -> None:
            self.stdout = text

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Out("Page    1 size:  72 x 14400 pts\n"))
    assert pdf_preview._pdf_page_size(source) == (72.0, 14400.0)

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Out("Page size:       612 x 792 pts (letter)\n"))
    assert pdf_preview._pdf_page_size(source) == (612.0, 792.0)

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Out("Encrypted: yes\n"))
    assert pdf_preview._pdf_page_size(source) is None

    def cannot_start(*a, **k):
        raise OSError("pdfinfo is not executable")

    monkeypatch.setattr(subprocess, "run", cannot_start)
    assert pdf_preview._pdf_page_size(source) is None


def test_the_fallback_runs_with_that_argv_on_a_host_without_pymupdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path, on the host shape it is written for: what the child is
    asked to draw is what the bound above computed, not what the page said."""
    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "cache_dir", lambda: tmp_path / "cache")
    _without_pymupdf(monkeypatch)
    monkeypatch.setattr(pdf_preview, "_pdf_page_size", lambda source: (72.0, 14400.0))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    seen = tmp_path / "argv"
    script = fake_bin / "pdftoppm"
    script.write_text(
        '#!/bin/bash\nout="${@: -1}"\nprintf "PNGfake" > "$out-1.png"\nprintf "%s\\n" "$@" > "$RAVEN_ARGV_LOG"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("RAVEN_ARGV_LOG", str(seen))
    source = tmp_path / "one.pdf"
    source.write_bytes(FAKE_PDF)

    target = tmp_path / "cache" / "one.png"
    pdf_preview._rasterise_pdf_page(source, target, pdf_preview.THUMB_WIDTH_PX, 12.0)

    assert target.read_bytes() == b"PNGfake"
    assert not list((tmp_path / "cache").glob("render-*")), "the scratch directory is gone"
    argv = seen.read_text().split()
    assert "-1" not in argv, "the child is told both sides"
    drawn = int(argv[argv.index("-scale-to-x") + 1]) * int(argv[argv.index("-scale-to-y") + 1])
    assert drawn <= pdf_preview.THUMB_MAX_PIXELS * 1.01


def test_a_pdf_page_fallback_that_writes_nothing_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "cache_dir", lambda: tmp_path / "cache")
    _without_pymupdf(monkeypatch)

    fake_bin = tmp_path / "quiet"
    fake_bin.mkdir()
    script = fake_bin / "pdftoppm"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    source = tmp_path / "one.pdf"
    source.write_bytes(FAKE_PDF)

    with pytest.raises(pdf_preview.PdfPreviewError, match="drew nothing"):
        pdf_preview._rasterise_pdf_page(source, tmp_path / "cache" / "c.png", 640)


def test_a_pdf_page_falls_back_to_pdftoppm_and_then_says_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "cache_dir", lambda: tmp_path / "cache")
    _without_pymupdf(monkeypatch)
    source = tmp_path / "one.pdf"
    source.write_bytes(FAKE_PDF)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    script = fake_bin / "pdftoppm"
    script.write_text(
        '#!/bin/bash\nprintf "%s " "$@" > "'
        + str(fake_bin / "argv.txt")
        + '"\nout="${@: -1}"\nprintf "PNGfake" > "$out-1.png"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    target = tmp_path / "cache" / "one.png"
    pdf_preview._rasterise_pdf_page(source, target, 640)
    assert target.read_bytes() == b"PNGfake"
    # One number for the long side, not a pinned width with the height left to
    # follow: the child must not be able to draw what this process refuses to.
    argv = (fake_bin / "argv.txt").read_text().split() if (fake_bin / "argv.txt").exists() else []
    if argv:
        assert "-scale-to" in argv and "-scale-to-y" not in argv

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(pdf_preview.PdfPreviewUnavailableError, match="no PDF rasteriser"):
        pdf_preview._rasterise_pdf_page(source, tmp_path / "cache" / "two.png", 640)


async def test_a_second_click_reads_the_cache(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    deck = _deck(tmp_path)

    first = await _render(client, deck)
    second = await _render(client, deck)

    assert first.status == second.status == 200
    assert await second.read() == FAKE_PDF
    assert len(soffice.calls()) == 1


async def test_a_rebuilt_deck_renders_again(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    """The cache is keyed by size and mtime, so a deck written over is a miss."""
    import os

    deck = _deck(tmp_path)
    assert (await _render(client, deck)).status == 200

    deck.write_bytes(b"PK\x03\x04 more slides than before")
    later = deck.stat().st_mtime + 5
    os.utime(deck, (later, later))
    assert (await _render(client, deck)).status == 200

    assert len(soffice.calls()) == 2


async def test_the_published_pdf_beside_a_deck_is_served_without_libreoffice(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice
) -> None:
    """The engine publishes `<stem>.pdf` next to every deck it delivers; when it is
    current, that is the rendering, and no conversion runs."""
    import os

    deck = _deck(tmp_path)
    published = tmp_path / "deck.pdf"
    published.write_bytes(b"%PDF-1.4 published by the engine\n%%EOF\n")
    newer = deck.stat().st_mtime + 2
    os.utime(published, (newer, newer))

    r = await _render(client, deck)

    assert r.status == 200
    assert await r.read() == published.read_bytes()
    assert soffice.calls() == []


async def test_a_published_pdf_that_leaves_the_fence_is_rendered_instead(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sibling is a path the page never asked for, so it goes through the viewer's
    own check. A deck whose neighbour is a symlink into raven's state directory would
    otherwise serve that file: the shortcut reaches it without resolve_readable, which
    is what resolves the link and refuses where it lands. A refused sibling is not an
    error -- the conversion runs, into the cache this route owns."""
    import os

    state = tmp_path / "state-home"
    (state / "oauth").mkdir(parents=True)
    secret = state / "oauth" / "tokens.json"
    secret.write_bytes(b"%PDF-1.4 not a preview at all\n%%EOF\n")
    monkeypatch.setenv("RAVEN_HOME", str(state))

    deck = _deck(tmp_path)
    sibling = tmp_path / "deck.pdf"
    sibling.symlink_to(secret)
    newer = deck.stat().st_mtime + 2
    os.utime(secret, (newer, newer))

    r = await _render(client, deck)

    assert r.status == 200
    body = await r.read()
    assert body == FAKE_PDF, "the conversion answered, not the file behind the link"
    assert secret.read_bytes() not in body
    assert len(soffice.calls()) == 1


def test_the_sibling_is_fenced_against_the_workspace_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One policy context, not two. handle_file admits the deck against the session's
    own workspace, so the sibling shortcut has to face that same root: checking it
    against the configured default pointed both ways with restrict_to_workspace on --
    a deck.pdf in the session's root symlinked to a PDF the session may not request
    was accepted, and an ordinary sibling in that root was refused into a render of
    what already existed."""
    import os

    from raven.rpc import pdf_preview

    session = tmp_path / "session-root"
    elsewhere = tmp_path / "configured"
    session.mkdir()
    elsewhere.mkdir()
    forbidden = elsewhere / "other.pdf"
    forbidden.write_bytes(b"%PDF-1.4 a file this session may not ask for\n%%EOF\n")

    cfg = load_config()
    cfg.tools.restrict_to_workspace = True
    cfg.agents.defaults.workspace = str(elsewhere)
    monkeypatch.setattr(files_module, "load_config", lambda: cfg)

    deck = _deck(session)
    link = session / "deck.pdf"
    link.symlink_to(forbidden)
    newer = deck.stat().st_mtime + 2
    os.utime(forbidden, (newer, newer))

    assert pdf_preview.published_pdf(deck, session) is None, "the link leaves the session's root"
    # The fence the bug applied: the configured workspace holds the target, so the
    # default context takes it.
    assert pdf_preview.published_pdf(deck) == forbidden.resolve()

    link.unlink()
    published = session / "deck.pdf"
    published.write_bytes(b"%PDF-1.4 published beside the deck\n%%EOF\n")
    os.utime(published, (newer, newer))

    assert pdf_preview.published_pdf(deck, session) == published, "an ordinary sibling is not a render"
    assert pdf_preview.published_pdf(deck) is None, "and the wrong fence refused it"


async def test_the_render_route_carries_the_sessions_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The route resolves the deck against the session's workspace and hands the same
    root to the render path, so the sibling shortcut cannot face a different fence from
    the deck. Its own gateway, because the session branch only runs where an agent-loop
    factory is wired."""
    from raven.rpc import pdf_preview

    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    cfg.tools.restrict_to_workspace = False
    monkeypatch.setattr(files_module, "load_config", lambda: cfg)
    monkeypatch.setenv("RAVEN_SERVE_TOKEN", TOKEN)

    seen: list[object] = []
    rendered = tmp_path / "rendered.pdf"
    rendered.write_bytes(FAKE_PDF)

    async def _fake(source: Path, *, timeout_s: float | None = None, workspace: Path | None = None) -> Path:
        seen.append(workspace)
        return rendered

    monkeypatch.setattr(pdf_preview, "pdf_for", _fake)
    monkeypatch.setattr("raven.rpc.methods.console._safe_loop", lambda factory: object(), raising=False)
    monkeypatch.setattr("raven.rpc.methods.console._workspace_root", lambda loop, key: tmp_path, raising=False)

    # Through build_app's own parameter: it assigns the attribute from the argument,
    # so setting it on the instance first is overwritten.
    app = build_app(WsGateway(), None, agent_loop_factory=lambda: object())
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        deck = _deck(tmp_path)
        r = await c.get("/file", params={"path": str(deck), "render": "pdf"}, headers=auth())
        assert r.status == 200 and seen == [None], "no session named, no workspace to carry"

        r = await c.get("/file", params={"path": str(deck), "render": "pdf", "session": "web:s1"}, headers=auth())
        assert r.status == 200
        assert seen[-1] == tmp_path, "the session's root reached the render path"
    finally:
        await c.close()


async def test_a_stale_published_pdf_is_not_trusted(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    import os

    deck = _deck(tmp_path)
    published = tmp_path / "deck.pdf"
    published.write_bytes(b"%PDF-1.4 from an older build\n%%EOF\n")
    older = deck.stat().st_mtime - 60
    os.utime(published, (older, older))

    r = await _render(client, deck)

    assert r.status == 200
    assert await r.read() == FAKE_PDF
    assert len(soffice.calls()) == 1


async def test_two_simultaneous_clicks_run_one_conversion(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice
) -> None:
    """A double click must not start two LibreOffices against the same deck."""
    import asyncio

    soffice.mode.write_text("slow")
    deck = _deck(tmp_path)

    first, second = await asyncio.gather(_render(client, deck), _render(client, deck))

    assert first.status == second.status == 200
    assert await first.read() == await second.read() == FAKE_PDF
    assert len(soffice.calls()) == 1


async def test_a_conversion_that_writes_nothing_is_a_clear_error(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice
) -> None:
    """LibreOffice exits 0 for a document it could not load; the missing file is the
    failure, and the page is told so in words."""
    soffice.mode.write_text("empty")
    deck = _deck(tmp_path)

    r = await _render(client, deck)

    assert r.status == 500
    body = await r.text()
    assert "did not produce a PDF for deck.pptx" in body
    assert "could not be loaded" in body


async def test_a_hung_conversion_is_stopped_and_reported(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "CONVERT_TIMEOUT_S", 0.5)
    soffice.mode.write_text("hang")
    deck = _deck(tmp_path)

    started = time.monotonic()
    r = await _render(client, deck)

    assert r.status == 504
    assert "longer than 0.5s" in await r.text()
    assert time.monotonic() - started < 10, "the process group was not killed"
    assert not any(pdf_preview.cache_dir().glob("*.pdf"))


async def test_a_cached_rendering_that_is_still_in_use_is_not_swept(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep reads mtime, and mtime is now when the entry was last served.

    Written-at would have deleted a rendering this route had just handed back: a
    cached file older than the TTL is returned as it stands, and the next render's
    sweep took it while the response that asked for it had not opened it yet --
    lost on any platform, and on Windows lost with a handle open.
    """
    import os

    from raven.rpc import pdf_preview

    deck = _deck(tmp_path)
    assert (await _render(client, deck)).status == 200
    cached = next(pdf_preview.cache_dir().glob("*.pdf"))
    stale = time.time() - pdf_preview.CACHE_TTL_S - 60
    os.utime(cached, (stale, stale))

    served = await _render(client, deck)

    assert served.status == 200 and cached.is_file(), "the entry was swept while it was the answer"
    assert cached.stat().st_mtime > stale, "and it is dated by this use, so the next sweep spares it"

    other = _deck(tmp_path, name="second.pptx", payload=b"PK\x03\x04 other")
    assert (await _render(client, other)).status == 200
    assert cached.is_file(), "a later render sweeps by last use, and this one was just used"


async def test_a_host_without_libreoffice_says_so(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    deck = _deck(tmp_path)

    r = await _render(client, deck)

    assert r.status == 503
    assert "LibreOffice is not installed" in await r.text()


async def test_only_a_deck_can_be_asked_for_as_a_pdf(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    (tmp_path / "notes.md").write_text("# hi\n", encoding="utf-8")

    r = await client.get("/file", params={"path": str(tmp_path / "notes.md"), "render": "pdf"}, headers=auth())

    assert r.status == 400
    assert soffice.calls() == []


async def test_rendering_needs_a_session_too(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    """Starting LibreOffice on the host is not something an unauthenticated
    request may do, however harmless the file."""
    deck = _deck(tmp_path)

    r = await client.get("/file", params={"path": str(deck), "render": "pdf"})

    assert r.status == 401
    assert soffice.calls() == []


# ---------------------------------------------------------------------------
# /knowledge/file -- the original upload behind a knowledge document
# ---------------------------------------------------------------------------


@pytest.fixture
def kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A knowledge manager rooted in tmp_path, wired to the route's lookup."""
    from raven.knowledge import EmbeddingConfig, KnowledgeManager
    from raven.rpc.methods import knowledge as knowledge_methods

    # The width probe is what creating a base reaches the network for, and
    # these tests are about what the route serves rather than about embedding.
    # Stubbed at that one call rather than behind a fake client, so the test
    # does not have to track EmbeddingClient's surface to stay passing.
    manager = KnowledgeManager(
        tmp_path / "kbroot",
        embedding=EmbeddingConfig(model="stub-embed", base_url="https://embed.test/v1", api_key="k", dimensions=8),
    )

    async def _width(_client) -> int:
        return 8

    monkeypatch.setattr(manager, "_width_of", _width)
    monkeypatch.setattr(knowledge_methods, "knowledge_manager", lambda: manager)
    return manager


async def _base(kb):
    return await kb.create_base(name="handbook")


async def test_knowledge_file_serves_the_upload_by_id(client: TestClient, kb) -> None:
    """The bytes come back under the name they were uploaded with, not under
    the blob's, which has no name to speak of."""
    base = await _base(kb)
    doc = kb.add_document(base.id, filename="onboarding.md", content=b"# hi\n")

    r = await client.get("/knowledge/file", params={"document": doc.id}, headers=auth())

    assert r.status == 200
    assert await r.read() == b"# hi\n"
    # Decided from the record's filename: read off the extensionless blob this
    # would be application/octet-stream, which downloads instead of showing.
    assert r.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert r.headers["Content-Disposition"] == "inline"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_knowledge_file_sandboxes_every_response(client: TestClient, kb) -> None:
    """An upload can be HTML, which is a script carrier. Served same-origin
    without this it would hand the page's own origin -- and with it the session
    cookie and the RPC socket -- to whatever was uploaded."""
    base = await _base(kb)
    doc = kb.add_document(base.id, filename="report.html", content=b"<p>hi</p>")

    r = await client.get("/knowledge/file", params={"document": doc.id}, headers=auth())

    assert r.headers["Content-Security-Policy"] == "sandbox"


async def test_knowledge_file_lets_a_pdf_run_its_viewer(client: TestClient, kb) -> None:
    """The browser's PDF viewer is script-driven and renders blank without it.
    The origin stays opaque either way: allow-same-origin is never granted."""
    base = await _base(kb)
    doc = kb.add_document(base.id, filename="scan.pdf", content=b"%PDF-1.4\n")

    r = await client.get("/knowledge/file", params={"document": doc.id}, headers=auth())

    assert r.headers["Content-Security-Policy"] == "sandbox allow-scripts"
    assert r.headers["Content-Type"] == "application/pdf"


async def test_knowledge_file_refuses_without_a_token(client: TestClient, kb) -> None:
    base = await _base(kb)
    doc = kb.add_document(base.id, filename="onboarding.md", content=b"hi")

    r = await client.get("/knowledge/file", params={"document": doc.id})

    assert r.status == 401


@pytest.mark.parametrize("document", ["", "nosuchdocument"])
async def test_knowledge_file_answers_404_for_a_document_that_is_not_there(
    client: TestClient, kb, document: str
) -> None:
    """Including the empty id: a request that names nothing is not a request
    for everything."""
    r = await client.get("/knowledge/file", params={"document": document}, headers=auth())

    assert r.status == 404


async def test_knowledge_file_takes_no_path_from_the_page(client: TestClient, kb, tmp_path: Path) -> None:
    """The route is addressed by id, so there is no path to traverse. A path
    parameter is not a second way in -- it is ignored, and the id still
    decides."""
    secret = tmp_path / "serve.json"
    secret.write_text('{"token": "shh"}', encoding="utf-8")
    base = await _base(kb)
    doc = kb.add_document(base.id, filename="onboarding.md", content=b"# hi\n")

    r = await client.get(
        "/knowledge/file",
        params={"document": doc.id, "path": str(secret)},
        headers=auth(),
    )

    assert r.status == 200
    assert await r.read() == b"# hi\n"


async def test_knowledge_file_refuses_a_render_it_cannot_do(client: TestClient, kb) -> None:
    """Markdown has no PDF rendering and needs none; saying so beats starting
    LibreOffice on a text file."""
    base = await _base(kb)
    doc = kb.add_document(base.id, filename="onboarding.md", content=b"# hi\n")

    r = await client.get("/knowledge/file", params={"document": doc.id, "render": "pdf"}, headers=auth())

    assert r.status == 400


async def test_knowledge_file_reports_a_host_with_no_libreoffice(
    client: TestClient, kb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503 with the reason in the body, which is the page's only signal: an
    empty frame cannot say "install LibreOffice"."""
    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "find_soffice", lambda: None)
    base = await _base(kb)
    doc = kb.add_document(base.id, filename="report.docx", content=b"PK\x03\x04stub")

    r = await client.get("/knowledge/file", params={"document": doc.id, "render": "pdf"}, headers=auth())

    assert r.status == 503
    assert "LibreOffice" in await r.text()


async def test_the_converter_is_handed_a_suffixed_name(kb) -> None:
    """LibreOffice picks its input filter partly from the extension, and a
    legacy .doc arriving as an extensionless blob is the case its sniffing is
    worst at. The alias is the same inode, so the cache key does not move."""
    from raven.rpc import knowledge_preview

    base = await _base(kb)
    doc = kb.add_document(base.id, filename="report.doc", content=b"\xd0\xcf\x11\xe0stub")
    blob = kb.document_path(doc.id)

    alias = knowledge_preview._alias_for(doc, blob)

    assert alias.suffix == ".doc"
    assert alias.read_bytes() == blob.read_bytes()
    assert alias.stat().st_size == blob.stat().st_size


async def test_deleting_a_document_takes_its_retained_source_with_it(kb) -> None:
    """Previewing an office file leaves a second copy of its content in the
    cache, because the renderer needs a path whose suffix says what the bytes
    are and a blob is stored under a bare id. Deleting the document unlinks
    the blob and knows nothing about that copy, so without this the content
    stays on disk under a name nothing lists."""
    from raven.rpc import knowledge_preview
    from raven.rpc.methods import knowledge as kb_methods

    base = await _base(kb)
    doc = kb.add_document(base.id, filename="notice.xls", content=b"\xd0\xcf\x11\xe0stub")
    alias = knowledge_preview._alias_for(doc, kb.document_path(doc.id))
    assert alias.is_file()

    kb_methods._set_manager_for_tests(kb)
    try:
        await kb_methods.knowledge_documents_delete({"document_id": doc.id})
    finally:
        kb_methods._set_manager_for_tests(None)

    assert not alias.is_file()


async def test_deleting_a_document_takes_its_rendering_with_it(kb) -> None:
    """The retained source is not the only copy a preview leaves. The PDF the
    converter produced is a readable copy of the whole document, and it is
    keyed by a digest of the source's path and stamp -- so the key has to be
    taken while that file is still there."""
    from raven.rpc import knowledge_preview, pdf_preview
    from raven.rpc.methods import knowledge as kb_methods

    base = await _base(kb)
    doc = kb.add_document(base.id, filename="notice.xls", content=b"\xd0\xcf\x11\xe0stub")
    alias = knowledge_preview._alias_for(doc, kb.document_path(doc.id))
    rendered = pdf_preview.cache_dir() / f"{pdf_preview.cache_key(alias)}.pdf"
    rendered.write_bytes(b"%PDF-1.4 the document, readable")

    kb_methods._set_manager_for_tests(kb)
    try:
        await kb_methods.knowledge_documents_delete({"document_id": doc.id})
    finally:
        kb_methods._set_manager_for_tests(None)

    assert not rendered.is_file()
    assert not alias.is_file()


async def test_deleting_a_base_takes_every_retained_source_with_it(kb) -> None:
    """The same for the base around them: its delete visits blobs only."""
    from raven.rpc import knowledge_preview, pdf_preview
    from raven.rpc.methods import knowledge as kb_methods

    base = await _base(kb)
    aliases = []
    for name in ("one.xls", "two.doc"):
        doc = kb.add_document(base.id, filename=name, content=b"\xd0\xcf\x11\xe0stub")
        aliases.append(knowledge_preview._alias_for(doc, kb.document_path(doc.id)))
    assert all(a.is_file() for a in aliases)

    renderings = [pdf_preview.cache_dir() / f"{pdf_preview.cache_key(a)}.pdf" for a in aliases]
    for rendering in renderings:
        rendering.write_bytes(b"%PDF-1.4 the document, readable")

    kb_methods._set_manager_for_tests(kb)
    try:
        await kb_methods.knowledge_bases_delete({"base_id": base.id})
    finally:
        kb_methods._set_manager_for_tests(None)

    assert not any(a.is_file() for a in aliases)
    assert not any(r.is_file() for r in renderings)


async def test_a_retained_source_nobody_deleted_is_swept_like_a_rendering(kb, monkeypatch) -> None:
    """What bounds the ones whose document went away by some other route. The
    sweep counted only the renderings in the cache root, so these accumulated
    for as long as the installation lived."""
    import os

    from raven.rpc import knowledge_preview, pdf_preview

    base = await _base(kb)
    doc = kb.add_document(base.id, filename="notice.xls", content=b"\xd0\xcf\x11\xe0stub")
    alias = knowledge_preview._alias_for(doc, kb.document_path(doc.id))

    stale = time.time() - (pdf_preview.CACHE_TTL_S + 3600)
    os.utime(alias, (stale, stale))
    pdf_preview._sweep(pdf_preview.cache_dir())

    assert not alias.is_file()


async def test_the_sweep_leaves_a_retained_source_still_in_use(kb) -> None:
    from raven.rpc import knowledge_preview, pdf_preview

    base = await _base(kb)
    doc = kb.add_document(base.id, filename="notice.xls", content=b"\xd0\xcf\x11\xe0stub")
    alias = knowledge_preview._alias_for(doc, kb.document_path(doc.id))

    pdf_preview._sweep(pdf_preview.cache_dir())

    assert alias.is_file()


async def test_a_page_runs_only_when_the_request_asks(client: TestClient, tmp_path: Path) -> None:
    """The reader's opt-in travels on the request, and nothing remembers it.

    What ``run`` grants is what a PDF already has -- ``allow-scripts`` with no
    ``allow-same-origin`` -- so the origin stays opaque and the page still
    reaches no cookie of the session's. What it newly permits is the page making
    requests of its own, which is why it is an act rather than a default.
    """
    page = tmp_path / "deck.html"
    page.write_text("<h1>hi</h1>", encoding="utf-8")

    plain = await client.get("/file", params={"path": str(page)}, headers=auth())
    asked = await client.get("/file", params={"path": str(page), "run": "1"}, headers=auth())

    assert plain.headers["Content-Security-Policy"] == "sandbox"
    assert asked.headers["Content-Security-Policy"] == "sandbox allow-scripts"
    assert "allow-same-origin" not in asked.headers["Content-Security-Policy"]

    again = await client.get("/file", params={"path": str(page)}, headers=auth())
    assert again.headers["Content-Security-Policy"] == "sandbox", "the grant is not remembered"


async def test_only_the_kinds_that_can_need_it_may_be_asked_to_run(client: TestClient, tmp_path: Path) -> None:
    """A text file has no code to run, so `run` on one is a request with no
    meaning -- and answering it would widen the grant past the two kinds an
    agent writes that can need their own code to finish."""
    notes = tmp_path / "notes.txt"
    notes.write_text("plain", encoding="utf-8")

    r = await client.get("/file", params={"path": str(notes), "run": "1"}, headers=auth())

    assert r.headers["Content-Security-Policy"] == "sandbox"


async def _page(client: TestClient, source: Path, p: int | str | None = None):
    params: dict[str, str] = {"path": str(source), "render": "page"}
    if p is not None:
        params["p"] = str(p)
    return await client.get("/file", params=params, headers=auth())


async def test_a_pdf_is_served_as_pictures_of_its_pages(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The viewer draws a PDF page by page rather than framing the document.

    Safari does not draw a framed PDF served under the sandbox policy every file
    here carries, and that policy is what keeps an agent's document away from the
    page's cookie and its socket -- so the pictures are what changed. Each answer
    carries the page count, which is how one round trip both proves the rendering
    can be made and sizes the rest of it.
    """
    from raven.rpc import pdf_preview

    drawn: list[int] = []

    def rasterise(source: Path, target: Path, width: int, timeout_s: float, page: int = 1) -> None:
        drawn.append(page)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(FAKE_PNG)

    monkeypatch.setattr(pdf_preview, "_rasterise_pdf_page", rasterise)
    monkeypatch.setattr(pdf_preview, "_count_pages", lambda pdf: 3)
    report = _deck(tmp_path, "report.pdf", FAKE_PDF)

    first = await _page(client, report)
    assert first.status == 200
    assert first.headers["Content-Type"] == "image/png"
    assert first.headers["X-Raven-Pdf-Pages"] == "3"
    assert await first.read() == FAKE_PNG

    last = await _page(client, report, 3)
    assert last.status == 200 and last.headers["X-Raven-Pdf-Pages"] == "3"
    assert drawn == [1, 3]

    again = await _page(client, report, 3)
    assert again.status == 200
    assert drawn == [1, 3], "a page already drawn is read from the cache"


async def test_a_page_past_the_end_is_refused_rather_than_drawn(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page number that is not one, and one past the end, are both 400.

    The viewer asks for what the count told it exists, so a page past the end is
    this route's answer to get right rather than the reader's mistake to report.
    """
    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "_count_pages", lambda pdf: 2)
    report = _deck(tmp_path, "report.pdf", FAKE_PDF)

    for asked in (0, 3, "x"):
        r = await _page(client, report, asked)
        assert r.status == 400, f"page {asked!r} should be refused"


async def test_a_deck_is_paged_through_its_own_pdf(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deck is converted once and its pages come from that one rendering.

    The count and the pages the reader then sees are read off the same PDF, so
    the two cannot disagree, and the conversion is the one the viewer had
    already cached.
    """
    from raven.rpc import pdf_preview

    drawn: list[Path] = []

    def rasterise(source: Path, target: Path, width: int, timeout_s: float, page: int = 1) -> None:
        drawn.append(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(FAKE_PNG)

    monkeypatch.setattr(pdf_preview, "_rasterise_pdf_page", rasterise)
    monkeypatch.setattr(pdf_preview, "_count_pages", lambda pdf: 2)
    deck = _deck(tmp_path)

    one = await _page(client, deck, 1)
    two = await _page(client, deck, 2)

    assert one.status == two.status == 200
    assert one.headers["X-Raven-Pdf-Pages"] == "2"
    # LibreOffice ran once, for the PDF; both pages were drawn from it.
    assert sum("--convert-to pdf" in c for c in soffice.calls()) == 1
    assert len(drawn) == 2 and drawn[0] == drawn[1]
    assert drawn[0].suffix == ".pdf"
