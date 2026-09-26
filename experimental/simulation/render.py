"""Page images of a delivered deck: LibreOffice makes a PDF, PyMuPDF rasterises it, and the result is cached once."""

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

CONVERT_TIMEOUT = 180

_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def render(file: Path, out: Path, width: int) -> None:
    """Write one PNG per page of a deck into `out`."""
    import pymupdf

    with tempfile.TemporaryDirectory(prefix="deck-pages-") as work:
        pdf = file
        if file.suffix.lower() != ".pdf":
            # A private profile per conversion: two soffice processes sharing one profile block or fail silently.
            command = [
                "soffice",
                f"-env:UserInstallation=file://{work}/profile",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                work,
                str(file),
            ]
            done = subprocess.run(command, capture_output=True, text=True, timeout=CONVERT_TIMEOUT, check=False)
            pdf = Path(work) / f"{file.stem}.pdf"
            if not pdf.is_file():
                raise RuntimeError(f"LibreOffice produced no PDF: {(done.stderr or done.stdout).strip()[-400:]}")
        with pymupdf.open(pdf) as document:
            for number, page in enumerate(document, 1):
                zoom = width / page.rect.width
                page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(out / f"page-{number:02d}.png")
    if not any(out.glob("page-*.png")):
        raise RuntimeError("the deck has no pages")


def pages(directory: Path) -> list[Path]:
    return sorted(directory.glob("page-*.png"))


def cached(file: Path, cache: Path, width: int, draw=render) -> list[Path]:
    """Render a deck into `cache` once. A per-file lock serialises this process, and the finished folder appears
    by an atomic rename from a staging folder beside it, so another process never sees a half-written cache."""
    if pages(cache):
        return pages(cache)
    with _locks_guard:
        lock = _locks.setdefault(file, threading.Lock())
    with lock:
        if pages(cache):
            return pages(cache)
        cache.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{cache.name}-", dir=cache.parent))
        try:
            draw(file, staging, width)
            try:
                os.rename(staging, cache)
            except OSError:
                if not pages(cache):
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return pages(cache)
