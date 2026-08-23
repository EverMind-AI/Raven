"""The two defects every deck arrives with, fixed mechanically before anything reads it.

Neither is a mistake the author made and neither is visible in a render, which is
what they have in common and why they are corrected here instead of being reported
as findings. A finding asks the author to fix something; these have nothing to fix.

*Empty placeholders.* A page started from a layout that carries a title placeholder
holds that placeholder whether or not anything was written into it, and an empty one
is not invisible: PowerPoint draws its frame and the words "Click to add title"
across it in the editing view, which is the view whoever receives the deck opens it
in. Measured on a delivered deck: ten of thirteen pages carried one, all at the same
(0.72, 0.14) 11.88x0.98in the template puts its title row at.

*Table styles.* `add_table` stamps every table with the Office gallery's "Medium
Style 2 - Accent 1" -- blue header, blue banding -- and python-pptx offers no way to
take it off. Every cell here is styled explicitly, so it should be unreachable, and
it is not: the style also carries the header's font and the cell borders, and
PowerPoint applies each of those wherever the cell itself is silent.

*Cell borders.* ECMA-376 fixes the order of a:tcPr's children -- lnL, lnR, lnT, lnB,
then the fill -- and a table built by a program that wrote them in another order is
not the format. LibreOffice does not check, so every render and every measurement in
this pipeline said four delivered decks had clean tables; PowerPoint does check, and
drops the properties it cannot parse, so the reader saw the gallery's blue table.

Run over the whole deck rather than at the point each table is drawn, because the
author writes the program: `table()` from the layout helpers gets it right, and a
page that reached for `add_table` directly gets the same treatment.
"""

from __future__ import annotations

from pathlib import Path

from raven.ppt.services.measure.geometry import iter_shapes, open_deck, shows_picture

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

# The four edge elements, in the order the schema puts them in.
EDGES = ("lnL", "lnR", "lnT", "lnB")

# What python-pptx stamps on a table it creates. Matched by value so a table cloned
# out of a template keeps the style its designer chose.
GALLERY_STYLE = "{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"


def tidy(pptx_path: Path) -> tuple[str, ...]:
    """Correct the deck in place; return one line per thing changed."""
    presentation = open_deck(pptx_path)
    changed: list[str] = []
    for number, slide in enumerate(presentation.slides, start=1):
        changed.extend(_empty_placeholders(number, slide))
        changed.extend(_tables(number, slide))
    if changed:
        presentation.save(str(pptx_path))
    return tuple(changed)


def _empty_placeholders(number: int, slide) -> list[str]:
    dropped: list[str] = []
    for shape in list(slide.shapes):
        if not shape.is_placeholder or _holds_something(shape):
            continue
        kind = str(shape.placeholder_format.type).split(" ")[0].lower()
        shape._element.getparent().remove(shape._element)  # noqa: SLF001 -- no API for deleting a shape
        dropped.append(f"p{number}: dropped an empty {kind} placeholder, which reads as 'Click to add' in Office")
    return dropped


def _holds_something(shape) -> bool:
    """Whether anything would be lost by deleting this placeholder.

    A picture placeholder holds no text frame, a table placeholder's frame is a
    graphic, and a body placeholder filled with a photograph is a `p:sp` with a
    blip fill -- so text is only one of the four things worth checking.
    """
    if getattr(shape, "has_table", False) or getattr(shape, "has_chart", False) or shows_picture(shape):
        return True
    frame = getattr(shape, "text_frame", None)
    return bool(frame is not None and frame.text.strip())


def _tables(number: int, slide) -> list[str]:
    fixed: list[str] = []
    for shape in iter_shapes(slide.shapes):
        if not getattr(shape, "has_table", False):
            continue
        table = shape.table
        if _drop_gallery_style(table):
            fixed.append(f"p{number}: took the Office gallery table style off {shape.name!r}")
        cells = _order_cell_properties(table)
        if cells:
            fixed.append(f"p{number}: put {cells} cells of {shape.name!r} back in the schema's element order")
    return fixed


def _drop_gallery_style(table) -> bool:
    for element in table._tbl.iter(f"{_A}tableStyleId"):  # noqa: SLF001 -- no API for the table style
        if (element.text or "").strip().upper() == GALLERY_STYLE:
            element.getparent().remove(element)
            return True
    return False


def _order_cell_properties(table) -> int:
    """Move each cell's border elements to the front, in the schema's order."""
    touched = 0
    for row in table.rows:
        for cell in row.cells:
            properties = cell._tc.find(f"{_A}tcPr")  # noqa: SLF001 -- no API for cell borders
            if properties is None:
                continue
            edges = [element for edge in EDGES if (element := properties.find(f"{_A}{edge}")) is not None]
            if not edges:
                continue
            if [element.tag for element in properties][: len(edges)] == [element.tag for element in edges]:
                continue
            for element in edges:
                properties.remove(element)
            for index, element in enumerate(edges):
                properties.insert(index, element)
            touched += 1
    return touched
