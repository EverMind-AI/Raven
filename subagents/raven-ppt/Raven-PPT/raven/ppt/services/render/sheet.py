"""Several pages on one image, for whoever judges the deck as a deck.

There are two questions and they need different pictures. Per page: is this page's
layout doing its job. Across the deck: do these pages look like they belong
together -- does every page open with the same slab of title, do
three consecutive pages use the same two-column split, does the accent colour mean
the same thing throughout. The second question cannot be answered from pages sent
one at a time, because the answer *is* the comparison, and a model looking at page
four has only its notes about page three.

So the pages are composed into one contact sheet, which also costs far less than
sending each page as its own image.

The cell label is the real page number, parsed out of the file name rather than
taken from the position in the grid. A designer looking at a sheet of pages 3, 7
and 9 writes findings that cite page numbers, and a sheet that numbered them 1, 2,
3 would put every one of those findings on the wrong page.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from raven.ppt.services.render.errors import RenderError, RenderUnavailableError

# Vision models downsample anything larger, so a bigger sheet costs bytes and
# buys nothing.
DEFAULT_MAX_EDGE = 2048
DEFAULT_COLUMNS = 3

_PAGE_IN_NAME = re.compile(r"(\d+)(?!.*\d)")


def contact_sheet(
    pngs: list[Path],
    out: Path,
    columns: int = DEFAULT_COLUMNS,
    *,
    max_edge: int = DEFAULT_MAX_EDGE,
    label: bool = True,
) -> Path:
    """Tile `pngs` into one PNG at `out`, row-major, and return `out`.

    Cells are uniform and each page is fitted inside its cell, so pages of
    different sizes -- a 16:9 deck with one 4:3 page pasted in -- still line up
    into a grid a reader can scan.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - Pillow is a hard dependency
        raise RenderUnavailableError(
            "Pillow is not installed, so pages cannot be composed into a contact sheet"
        ) from exc

    images = [Path(png) for png in pngs]
    if not images:
        raise RenderError("a contact sheet needs at least one page")
    missing = [str(png) for png in images if not png.is_file()]
    if missing:
        raise RenderError(f"these pages are not on disk: {', '.join(missing)}")
    if columns < 1:
        raise RenderError(f"a contact sheet needs at least one column, got {columns}")

    columns = min(columns, len(images))
    rows = -(-len(images) // columns)
    with Image.open(images[0]) as first:
        cell_width, cell_height = first.size
    for png in images[1:]:
        with Image.open(png) as page:
            cell_width, cell_height = max(cell_width, page.width), max(cell_height, page.height)

    gap = max(2, round(min(cell_width, cell_height) * 0.02))
    unscaled = (columns * cell_width + gap * (columns + 1), rows * cell_height + gap * (rows + 1))
    scale = min(1.0, max_edge / max(unscaled))
    cell = (max(1, int(cell_width * scale)), max(1, int(cell_height * scale)))
    gap = max(1, int(gap * scale))
    sheet = Image.new(
        "RGB",
        (columns * cell[0] + gap * (columns + 1), rows * cell[1] + gap * (rows + 1)),
        (245, 245, 247),
    )
    font = ImageFont.load_default(size=max(11, int(cell[1] * 0.05))) if label else None
    draw = ImageDraw.Draw(sheet)
    for position, png in enumerate(images):
        row, column = divmod(position, columns)
        origin = (gap + column * (cell[0] + gap), gap + row * (cell[1] + gap))
        with Image.open(png) as page:
            thumbnail = page.convert("RGB")
            thumbnail.thumbnail(cell, Image.Resampling.LANCZOS)
            offset = (
                origin[0] + (cell[0] - thumbnail.width) // 2,
                origin[1] + (cell[1] - thumbnail.height) // 2,
            )
            sheet.paste(thumbnail, offset)
        if font is not None:
            _draw_label(draw, font, _page_label(png, position), offset)
    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, "PNG", optimize=True)
    return destination


def _page_label(png: Path, position: int) -> str:
    """The page number this image is of, or its place in the grid as a last resort."""
    match = _PAGE_IN_NAME.search(png.stem)
    if match is None:
        return str(position + 1)
    return str(int(match.group(1)))


def _draw_label(draw: Any, font: Any, text: str, offset: tuple[int, int]) -> None:
    """A number on a plate, top-left of the page it belongs to.

    On a plate rather than straight onto the page because a deck's own corner is
    often dark, and a label a reader cannot see makes every finding it was supposed
    to anchor ambiguous.
    """
    pad = 3
    box = draw.textbbox((0, 0), text, font=font)
    width, height = box[2] - box[0], box[3] - box[1]
    draw.rectangle(
        [offset[0], offset[1], offset[0] + width + 2 * pad, offset[1] + height + 2 * pad],
        fill=(24, 24, 27),
    )
    draw.text(
        (offset[0] + pad - box[0], offset[1] + pad - box[1]),
        text,
        font=font,
        fill=(255, 255, 255),
    )
