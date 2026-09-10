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
3 would put every one of those findings on the wrong page. A caller whose cells do
not come from one deck says what each one is through ``labels``, because a page
number is not an identity once page 4 could be four different templates' page 4.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from raven_ppt.services.render.errors import RenderError, RenderUnavailableError

# What a cell has to be, and therefore what the sheet is sized from. Measured over
# 459 vision calls on the model this engine runs, asked to name the template page
# whose arrangement fits a stated need: 767px-wide cells score 90% top-1 over 150
# scored answers, which is exactly what 20 separate full-size page renders score,
# while 232-523px cells score 82% -- and the loss is specifically the model naming
# the page next door in the grid, 5 of 150 answers at that size against 0 of 150 at
# 400-510px. Past this width a cell buys no accuracy and costs bytes.
#
# The cell first and the sheet second, because the other way round gives the biggest
# templates the smallest cells: a sheet clipped to a fixed long edge loses cell width
# with every extra row, and the same clip that left an 11-page template 523px cells
# left a 34-page one 232px.
DEFAULT_CELL_WIDTH = 768
# Four rather than three. Three columns with a 1568px long edge -- what this module
# and the encoder used to agree on -- can never produce a cell wider than 522px, and
# produced 232px on the largest template measured.
DEFAULT_COLUMNS = 4
# The ceiling, which exists so a pathological page count cannot ask for an image no
# gateway will take. Sized to clear the two sheets this engine actually builds: the 36
# reference pages at six columns come out 4664x2642, and 34 template pages at four
# columns would be 3974px tall. It starts costing cell width past about 47 pages in
# four columns; the bundled templates ship 15 to 25 and the widest measured ships 34.
DEFAULT_MAX_EDGE = 5120

_PAGE_IN_NAME = re.compile(r"(\d+)(?!.*\d)")


def contact_sheet(
    pngs: list[Path],
    out: Path,
    columns: int = DEFAULT_COLUMNS,
    *,
    cell_width: int = DEFAULT_CELL_WIDTH,
    max_edge: int = DEFAULT_MAX_EDGE,
    label: bool = True,
    labels: Sequence[str] | None = None,
) -> Path:
    """Tile `pngs` into one PNG at `out`, row-major, and return `out`.

    Cells are uniform and each page is fitted inside its cell, so pages of
    different sizes -- a 16:9 deck with one 4:3 page pasted in -- still line up
    into a grid a reader can scan.

    `cell_width` is what a cell is sized to and the sheet follows from it, so the
    cells stay the same size whether there are eight pages or thirty-four. A page
    is never enlarged past its own pixels, and `max_edge` still caps the whole
    sheet. `labels` names each cell in the caller's own terms, one per page, for a
    sheet whose cells do not share a numbering.
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
    if labels is not None and len(labels) != len(images):
        raise RenderError(f"a contact sheet of {len(images)} page(s) needs {len(images)} label(s), got {len(labels)}")

    columns = min(columns, len(images))
    rows = -(-len(images) // columns)
    with Image.open(images[0]) as first:
        page_width, page_height = first.size
    for png in images[1:]:
        with Image.open(png) as page:
            page_width, page_height = max(page_width, page.width), max(page_height, page.height)

    gap = max(2, round(min(page_width, page_height) * 0.02))
    unscaled = (columns * page_width + gap * (columns + 1), rows * page_height + gap * (rows + 1))
    # The wanted cell, then the ceiling, and never an enlargement: a page pasted
    # bigger than it was rendered adds no detail and pads the sheet with grey.
    scale = min(1.0, cell_width / page_width, max_edge / max(unscaled))
    cell = (max(1, int(page_width * scale)), max(1, int(page_height * scale)))
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
            said = labels[position] if labels is not None else _page_label(png, position)
            _draw_label(draw, font, said, offset)
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
