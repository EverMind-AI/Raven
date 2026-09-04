"""Render-bundle layout, publication, and validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from raven_design.rendering.models import RenderError

DOCUMENT_PDF = "document.pdf"
PAGES_DIR = "pages"
PREVIEW_DIR = "preview"
PUBLISHED_ENTRIES = frozenset({DOCUMENT_PDF, PAGES_DIR, PREVIEW_DIR})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class BundlePaths:
    root: Path

    @property
    def document(self) -> Path:
        return self.root / DOCUMENT_PDF

    @property
    def pages(self) -> Path:
        return self.root / PAGES_DIR

    @property
    def preview(self) -> Path:
        return self.root / PREVIEW_DIR


def validate_published_bundle(root: Path, max_bytes: int) -> None:
    if root.is_symlink() or not root.is_dir() or {path.name for path in root.iterdir()} != PUBLISHED_ENTRIES:
        raise RenderError("invalid_output", "The renderer bundle has an invalid top-level layout.")
    paths = BundlePaths(root)
    if (
        paths.document.is_symlink()
        or not paths.document.is_file()
        or paths.pages.is_symlink()
        or not paths.pages.is_dir()
        or paths.preview.is_symlink()
        or not paths.preview.is_dir()
    ):
        raise RenderError("invalid_output", "The renderer bundle is incomplete.")
    if not _starts_with(paths.document, b"%PDF-"):
        raise RenderError("invalid_output", "The renderer bundle contains an invalid PDF.")
    _validate_png_directory(paths.pages)
    _validate_png_directory(paths.preview)
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RenderError("invalid_output", "The renderer bundle contains a symbolic link.")
        if path.is_file():
            total += path.stat().st_size
            if total > max_bytes:
                raise RenderError("resource_limit_exceeded", "The renderer bundle exceeds the output byte limit.")


def _validate_png_directory(directory: Path) -> None:
    entries = list(directory.iterdir())
    if not entries:
        raise RenderError("invalid_output", "The renderer bundle contains an empty image directory.")
    for path in entries:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix.lower() != ".png"
            or not _starts_with(path, _PNG_SIGNATURE)
        ):
            raise RenderError("invalid_output", "The renderer bundle contains an invalid page image.")


def _starts_with(path: Path, prefix: bytes) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(prefix)) == prefix
    except OSError:
        return False
