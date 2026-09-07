"""Shared fixtures for the deck tests: built decks, and synthetic source material.

Two kinds of fixture, kept together because a test needs one or the other and
importing across test modules is worse than one file with two halves.

Built decks, for the measurements: python-pptx is the only way to produce one,
and a deck assembled shape by shape in every test buries the thing under test in
twelve lines of setup. The builder carries the two constants a measurement
depends on -- a 13.333x7.5in canvas and a blank layout -- and nothing else.

Synthetic source material, for the ingest: real papers and brochures must never
reach the code or the tests, and a synthetic shape pins the geometry a heuristic
actually keys on. Each one exists because some real source shape defeated an
earlier version of the ingest.

And one template, for the pages a user hands over. It is here rather than beside
the tests that use it because two modules need it and importing across test
modules is worse than one file with three halves.
"""

from __future__ import annotations

import io
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


class DeckBuilder:
    """A 16:9 deck under construction. Call `save()` for a path to measure."""

    def __init__(self, tmp_path: Path, *, width_in: float = 13.333, height_in: float = 7.5) -> None:
        from pptx import Presentation
        from pptx.util import Inches

        self.tmp_path = tmp_path
        self.presentation = Presentation()
        self.presentation.slide_width = Inches(width_in)
        self.presentation.slide_height = Inches(height_in)

    def page(self) -> Any:
        return self.presentation.slides.add_slide(self.presentation.slide_layouts[6])

    def layout_art(self, *, left: float = 7.0, top: float = 0.0, width: float = 6.3, height: float = 7.5) -> None:
        """Put a filled panel on the blank layout every `page()` is built on.

        Which python-pptx has no API for -- `add_shape` belongs to a slide's shapes,
        not a layout's -- so the shape is drawn on a scratch slide and its XML moved
        across. The surgery is here because the thing under test is what a layout
        draws, and a layout with nothing on it cannot test it: a master's and a
        layout's own shapes never reach `slide.shapes`, which is exactly why they
        were invisible to every measurement in this package.
        """
        import copy

        from pptx.enum.shapes import MSO_SHAPE
        from pptx.util import Inches

        scratch = self.page()
        panel = scratch.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
        panel.fill.solid()
        layout = self.presentation.slide_layouts[6]
        layout.shapes._spTree.append(copy.deepcopy(panel._element))
        slide_ids = self.presentation.slides._sldIdLst
        entry = list(slide_ids)[-1]
        self.presentation.part.drop_rel(entry.rId)
        slide_ids.remove(entry)

    def text(
        self,
        slide: Any,
        *runs: str | tuple[str, float],
        left: float = 0.5,
        top: float = 0.5,
        width: float = 12.0,
        height: float = 6.0,
        size: float = 16.0,
        bold: bool | None = None,
        wrap: bool | None = None,
        colour: str | None = None,
    ) -> Any:
        """A text box, one paragraph per run. A run may carry its own size.

        `colour` states the ink as six hex digits, which the contrast check reads off the
        file -- text that inherits its colour from the theme is not judged there.
        """
        from pptx.dml.color import RGBColor
        from pptx.util import Inches, Pt

        box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        frame = box.text_frame
        if wrap is not None:
            frame.word_wrap = wrap
        for index, item in enumerate(runs):
            text, point = item if isinstance(item, tuple) else (item, size)
            para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            run = para.add_run()
            run.text = text
            run.font.size = Pt(point)
            if bold is not None:
                run.font.bold = bold
            if colour is not None:
                run.font.color.rgb = RGBColor.from_string(colour)
        return box

    def panel(
        self,
        slide: Any,
        *,
        left: float,
        top: float,
        width: float,
        height: float,
        filled: bool = True,
        colour: tuple[int, int, int] = (0x3A, 0x3F, 0x8F),
        text: str | None = None,
        size: float = 28.0,
    ) -> Any:
        """A rectangle. Filled by default, since that is what makes it a panel."""
        from pptx.dml.color import RGBColor
        from pptx.util import Inches, Pt

        shape = slide.shapes.add_shape(1, Inches(left), Inches(top), Inches(width), Inches(height))
        if filled:
            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor(*colour)
        else:
            # Stated, not left alone: `add_shape` stamps a `p:style` whose `a:fillRef`
            # names a theme fill, and a shape nobody touched renders in it -- sampled at
            # (112, 160, 229) against a white page. So "unfilled" has to say so, or the
            # fixture and the render disagree about the one thing it is testing.
            shape.fill.background()
        if text:
            run = shape.text_frame.paragraphs[0].add_run()
            run.text = text
            run.font.size = Pt(size)
        return shape

    def table(
        self,
        slide: Any,
        rows: int,
        columns: int,
        *,
        left: float = 0.5,
        top: float = 1.0,
        width: float = 12.0,
        cell: str | None = None,
        points: float = 14.0,
    ) -> Any:
        """A table, optionally with the same string in every cell.

        `cell` and `width` are here because the readable question about a table is
        whether a column has room for what it holds, so a table with no text in it
        cannot be too narrow for anything.
        """
        from pptx.util import Inches, Pt

        frame = slide.shapes.add_table(rows, columns, Inches(left), Inches(top), Inches(width), Inches(2))
        if cell is not None:
            for row in range(rows):
                for column in range(columns):
                    target = frame.table.cell(row, column)
                    target.text = cell
                    for paragraph in target.text_frame.paragraphs:
                        for run in paragraph.runs:
                            run.font.size = Pt(points)
        return frame

    def picture(
        self,
        slide: Any,
        image: Path,
        *,
        left: float = 1.0,
        top: float = 2.0,
        width: float = 4.0,
        height: float | None = None,
    ) -> Any:
        from pptx.util import Inches

        return slide.shapes.add_picture(
            str(image),
            Inches(left),
            Inches(top),
            width=Inches(width),
            height=Inches(height) if height is not None else None,
        )

    def save(self, name: str = "deck.pptx") -> Path:
        path = self.tmp_path / name
        self.presentation.save(str(path))
        return path


@pytest.fixture
def deck(tmp_path: Path) -> DeckBuilder:
    return DeckBuilder(tmp_path)


@pytest.fixture
def image(tmp_path: Path):
    """A distinct solid-colour PNG, so two figures hash differently."""
    from PIL import Image

    def build(name: str, colour: tuple[int, int, int]) -> Path:
        path = tmp_path / name
        Image.new("RGB", (400, 300), colour).save(path)
        return path

    return build


def _noise(width: int, height: int):
    """Deterministic high-entropy content.

    A solid colour compresses below the decoration byte threshold and is
    filtered out as a logo, and it matches whatever border colour the trimmer
    detects -- so a fixture made of one is invisible to half the pipeline.
    """
    from PIL import Image

    img = Image.new("RGB", (width, height))
    img.putdata([((x * 7 + y * 13) % 256, (x * 3) % 256, (y * 5) % 256) for y in range(height) for x in range(width)])
    return img


@pytest.fixture
def noise_image() -> Callable[[int, int], object]:
    return _noise


@pytest.fixture
def noise_png() -> Callable[[int, int], bytes]:
    def build(width: int, height: int) -> bytes:
        buffer = io.BytesIO()
        _noise(width, height).save(buffer, format="PNG")
        return buffer.getvalue()

    return build


@pytest.fixture
def product_page() -> Callable[[Path], Path]:
    """A page screenshot in the shape web-print PDFs actually produce: a light
    chrome strip over a dark page, a headline, and two image blocks."""

    def build(path: Path) -> Path:
        from PIL import Image, ImageDraw

        page = Image.new("RGB", (1400, 1800), (12, 12, 14))
        draw = ImageDraw.Draw(page)
        draw.rectangle((0, 0, 1400, 60), fill=(245, 245, 245))  # browser chrome
        for row in range(2):  # a headline, thin strokes on the dark ground
            draw.rectangle((300, 150 + row * 50, 1100, 175 + row * 50), fill=(235, 235, 235))
        for top, colour in ((400, (180, 90, 40)), (1150, (40, 120, 180))):
            block = Image.new("RGB", (700, 500))
            pixels = block.load()
            for y in range(500):
                for x in range(700):
                    pixels[x, y] = (
                        (colour[0] + x // 5) % 256,
                        (colour[1] + y // 7) % 256,
                        (colour[2] + (x + y) // 6) % 256,
                    )
            page.paste(block, (350, top))
        page.save(path)
        return path

    return build


@pytest.fixture
def template_file(tmp_path: Path, image) -> Callable[..., Path]:
    """A template shaped like the real ones: layouts, a theme, example pages.

    Every element on the example page is one the measurement found on the user's
    own templates and that an earlier version of the decompiler got wrong: a
    group with its own child coordinate space, a vertically centred text box, a
    centred paragraph, a connector, a freeform, and a picture.
    """

    def build(name: str = "house-style.pptx", where: Path | None = None) -> Path:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        from pptx.util import Inches, Pt

        presentation = Presentation()
        presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)

        page = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = page.shapes.add_textbox(Inches(1), Inches(0.5), Inches(11), Inches(1.2))
        frame = box.text_frame
        frame.text = "Section title"
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        frame.paragraphs[0].runs[0].font.size = Pt(32)
        frame.paragraphs[0].runs[0].font.bold = True

        panel = page.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1), Inches(2), Inches(4), Inches(2))
        panel.rotation = 15.0
        page.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(6), Inches(2), Inches(11), Inches(2))

        freeform = page.shapes.build_freeform(Inches(6), Inches(3))
        freeform.add_line_segments([(Inches(9), Inches(3)), (Inches(9), Inches(5))])
        freeform.convert_to_shape()

        page.shapes.add_picture(str(image("logo.png", (30, 60, 120))), Inches(1), Inches(4.5), Inches(2), Inches(2))

        presentation.slides.add_slide(presentation.slide_layouts[6])
        path = (where or tmp_path) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        presentation.save(str(path))
        return path

    return build


def layout_picture(layout, image: Path, left: float, top: float, width: float, height: float):
    """Put a picture on a *layout*, which python-pptx's `LayoutShapes` cannot do itself.

    The bundled templates carry their cover, section and closing photographs there, so a
    test about those pictures needs one; the element is the same `p:pic` a slide gets,
    related to the layout part's own image.
    """
    from pptx.oxml.shapes.picture import CT_Picture
    from pptx.util import Inches

    _, relationship = layout.part.get_or_add_image_part(str(image))
    tree = layout.shapes._spTree
    taken = [int(node.get("id")) for node in tree.iter() if node.tag.endswith("}cNvPr") and node.get("id")]
    shape_id = max(taken, default=1) + 1
    tree.append(
        CT_Picture.new_pic(
            shape_id, f"Picture {shape_id}", "", relationship, Inches(left), Inches(top), Inches(width), Inches(height)
        )
    )
    return list(layout.shapes)[-1]


# A comment run contiguous above the first banner, so the banner walk-back reaches the
# file's first line, and the comment names a slide creator, so which block the line lands
# in changes what the diagnosis counts. This is the file on which reading the mark and not
# reading it give two different refusals.
COMMENT_ABOVE_FIRST_BANNER = textwrap.dedent(
    """
    # each page calls add_slide() once, through new_slide
    # SLIDE 1
    import os

    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)


    def new_slide(text):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(0.8), Inches(0.6), Inches(11), Inches(1))
        box.text_frame.text = text
        return slide


    new_slide("First")

    # SLIDE 2
    new_slide("Second")

    prs.save(os.environ["PPT_OUTPUT"])
    """
).lstrip()
