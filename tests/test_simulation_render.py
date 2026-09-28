"""A delivered deck is rendered to page images once and cached beside it, even under concurrent requests."""

import shutil
import threading

import pytest

from experimental.simulation.files import page_images
from experimental.simulation.render import cached


def fake_render(calls, pages=2, pause=None):
    def render(file, out, width):
        calls.append((file, width))
        if pause is not None:
            pause.wait(5)
        for number in range(1, pages + 1):
            (out / f"page-{number:02d}.png").write_bytes(b"png")

    return render


def deck(tmp_path):
    file = tmp_path / "deliverables" / "deck.pptx"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"pptx")
    return file


def thumbs(file):
    return file.with_name(f"{file.name}.thumbs")


def test_a_deck_renders_once_and_caches_beside_itself(tmp_path):
    calls = []
    file = deck(tmp_path)
    pages = cached(file, thumbs(file), 480, fake_render(calls))
    assert pages == [thumbs(file) / "page-01.png", thumbs(file) / "page-02.png"]
    assert cached(file, thumbs(file), 480, fake_render(calls)) == pages
    assert len(calls) == 1 and calls[0][1] == 480
    assert sorted(path.name for path in file.parent.iterdir()) == ["deck.pptx", "deck.pptx.thumbs"]


def test_concurrent_requests_render_one_deck_once(tmp_path):
    calls, pause = [], threading.Event()
    file = deck(tmp_path)
    render = fake_render(calls, pages=3, pause=pause)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(cached(file, thumbs(file), 480, render))) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    pause.set()
    for thread in threads:
        thread.join(10)
    assert len(calls) == 1
    assert len(results) == 4 and all(len(pages) == 3 for pages in results)


def test_a_failed_render_leaves_no_cache_and_is_retried(tmp_path):
    file = deck(tmp_path)

    def broken(file, out, width):
        (out / "page-01.png").write_bytes(b"half")
        raise RuntimeError("LibreOffice produced no PDF")

    with pytest.raises(RuntimeError):
        cached(file, thumbs(file), 480, broken)
    assert sorted(path.name for path in file.parent.iterdir()) == ["deck.pptx"]
    calls = []
    assert len(cached(file, thumbs(file), 480, fake_render(calls))) == 2 and len(calls) == 1


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice is not installed")
def test_a_real_deck_renders_to_page_images(tmp_path):
    pptx = pytest.importorskip("pptx")
    pymupdf = pytest.importorskip("pymupdf")
    file = deck(tmp_path)
    presentation = pptx.Presentation()
    for title in ("Plan", "Quote"):
        slide = presentation.slides.add_slide(presentation.slide_layouts[0])
        slide.shapes.title.text = title
    presentation.save(file)
    pages = page_images(file)
    assert [page.name for page in pages] == ["page-01.png", "page-02.png"]
    assert pymupdf.Pixmap(str(pages[0])).width == 960
