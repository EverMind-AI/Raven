"""Image uploads: one picture, described by a model, indexed as one piece."""

from __future__ import annotations

import base64

import pytest

from raven.knowledge._naive_chunker import NaiveChunker
from raven.knowledge._vision import VisionError
from raven.knowledge.parser import ELEMENTS, LAYOUT_TYPE, READING_ORDER, LayoutType
from raven.knowledge.parser.image_parser import ImageParser

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _Vision:
    max_figures = 64

    def __init__(self, text: str = "A bar chart of revenue by region.", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.seen: list[tuple[int, str]] = []

    async def describe(self, image: bytes, *, mime: str = "", context_above: str = "", context_below: str = "") -> str:
        self.seen.append((len(image), mime))
        if self.error is not None:
            raise self.error
        return self.text


# -- what it claims ------------------------------------------------


def test_it_claims_the_raster_formats_a_decoder_can_open() -> None:
    assert "image/png" in ImageParser.supported_media_types
    assert "image/jpeg" in ImageParser.supported_media_types


def test_svg_and_heic_are_left_alone() -> None:
    """SVG is XML, which a text parser reads better than a model would; HEIC
    needs a Pillow plugin nothing here installs. Offering either would be
    offering an upload that fails on arrival."""
    claimed = " ".join(ImageParser.supported_media_types)

    assert "svg" not in claimed
    assert "heic" not in claimed


def test_the_picker_gets_the_extensions_people_type() -> None:
    assert ImageParser.supported_extensions() == [
        ".bmp",
        ".gif",
        ".jpeg",
        ".jpg",
        ".png",
        ".tif",
        ".tiff",
        ".webp",
    ]


# -- what it produces ----------------------------------------------


async def test_the_description_is_the_section() -> None:
    vision = _Vision()

    sections = await ImageParser(vision).parse(PNG, "chart.png")

    assert len(sections) == 1
    assert sections[0].content.text == "A bar chart of revenue by region."
    assert sections[0].source == "chart.png"
    assert sections[0].metadata[READING_ORDER] == 0
    assert sections[0].metadata[LAYOUT_TYPE] == LayoutType.FIGURE


async def test_the_section_says_it_is_a_figure_over_its_whole_text() -> None:
    """Which is what the chunker reads to mean "this stands alone" -- so the
    span has to cover everything, not the first line."""
    vision = _Vision(text="A" * 500)

    sections = await ImageParser(vision).parse(PNG, "chart.png")

    spans = sections[0].metadata[ELEMENTS]
    assert len(spans) == 1
    assert spans[0]["layout_type"] == LayoutType.FIGURE
    assert (spans[0]["char_start"], spans[0]["char_end"]) == (0, 500)


async def test_the_media_type_travels_from_the_filename() -> None:
    """A BMP or a TIFF is not in the magic-byte table the preparation step
    sniffs with, so the name is what carries the format through."""
    vision = _Vision()

    await ImageParser(vision).parse(PNG, "scan.tiff")

    assert vision.seen[0][1] == "image/tiff"


async def test_a_path_is_read_from_disk(tmp_path) -> None:
    picture = tmp_path / "chart.png"
    picture.write_bytes(PNG)
    vision = _Vision()

    sections = await ImageParser(vision).parse(str(picture), "chart.png")

    assert sections[0].content.text
    assert vision.seen[0][0] == len(PNG)


# -- one picture, one chunk ----------------------------------------


async def test_a_long_description_is_still_one_chunk() -> None:
    """The requirement this parser exists under: an image is one chunk, however
    much the model had to say about it. A description cut in half is two pieces
    of one picture, and neither says what it is a picture of."""
    vision = _Vision(text="The chart shows revenue. " * 300)

    sections = await ImageParser(vision).parse(PNG, "chart.png")
    chunks = await NaiveChunker(chunk_size=64).chunk(sections)

    assert len(chunks) == 1


async def test_it_is_not_merged_with_anything_either() -> None:
    """The other half of standing alone: a query that matches the picture gets
    the picture, not a paragraph that happened to sit beside it."""
    from raven.knowledge._types import Section, TextBlock

    sections = await ImageParser(_Vision()).parse(PNG, "chart.png")
    sections.append(Section(content=TextBlock(text="Unrelated prose."), source="notes.txt", metadata={}))

    chunks = await NaiveChunker(chunk_size=4096, image_context_size=0).chunk(sections)

    assert [chunk.content.text for chunk in chunks] == [
        "A bar chart of revenue by region.",
        "Unrelated prose.",
    ]


# -- when it cannot ------------------------------------------------


async def test_no_vision_model_is_a_failure_with_a_way_out(monkeypatch) -> None:
    """Not an empty section: a document that silently holds nothing is worse
    than a row that says why it is empty."""
    monkeypatch.setattr("raven.knowledge._vision.load_vision_model", lambda: None)

    with pytest.raises(VisionError) as caught:
        await ImageParser().parse(PNG, "chart.png")

    assert "no vision model is configured" in str(caught.value)
    assert "Settings" in str(caught.value), "and where to set one"


async def test_a_model_that_answers_nothing_is_a_failure_too() -> None:
    with pytest.raises(VisionError):
        await ImageParser(_Vision(text="  ")).parse(PNG, "chart.png")


async def test_the_endpoint_failure_is_passed_through() -> None:
    """So the row names the endpoint's own words rather than "could not
    parse"."""
    with pytest.raises(VisionError, match="rate limited"):
        await ImageParser(_Vision(error=VisionError("rate limited"))).parse(PNG, "chart.png")
