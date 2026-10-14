"""An uploaded picture, read by a model rather than by a decoder.

Every other parser here reads a format: a docx has paragraphs, a spreadsheet has
rows, and the text is already in the file. An image has none, so the only way to
index one is to have a model look at it and say what is there -- which is what
``raven.knowledge._vision`` does, and all this parser adds is the file.

Follows RAGFlow's ``VisionFigureParser`` (Apache-2.0; see NOTICES.md) in what it
asks the model for. What it does not follow is the OCR ladder in front of it:
RAGFlow runs a local OCR engine first and only calls the model when the text it
found is short. Raven ships no OCR engine, so there is one path here, and a
picture with no vision model configured fails rather than indexing as nothing.

One image is one chunk. The section carries a single element span covering the
whole description, marked as a figure, which is what the chunker already reads
to mean "this stands alone": a figure is never split on a delimiter and never
merged with a neighbour, so however long the description runs it stays one
piece.
"""

from __future__ import annotations

from pathlib import Path

from raven.knowledge._types import Section, TextBlock
from raven.knowledge._vision import VisionError, VisionModel
from raven.knowledge.parser import ElementSpan, LayoutType, ParserBase, section_metadata


class ImageParser(ParserBase):
    """Parser for image files: one picture, one described section.

    The vision model is resolved per call rather than held, so a reader who
    configures one does not have to restart the gateway for their next upload
    to be indexed -- and one who clears the pin stops paying for calls at the
    same point.
    """

    supported_media_types: list[str] = [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
    ]
    """Standard IANA media types this parser handles.

    The raster formats Pillow decodes without a plugin. SVG is XML rather than
    a picture -- a text parser reads it better than a vision model would -- and
    HEIC needs ``pillow-heif``, which nothing here installs; both are left out
    so the file picker does not offer an upload that fails on arrival.
    """

    def __init__(self, model: VisionModel | None = None) -> None:
        """
        Args:
            model (`VisionModel | None`):
                The describer to use. ``None`` (the default) resolves the
                configured ``vision`` pin on each call, which is what the
                manager wants; a caller that has already bound a model -- or a
                test -- passes one.
        """
        self._model = model

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """The image extensions, spelled the way a file picker wants them.

        Overridden because the base reverse-lookup answers with a long tail of
        aliases nobody types (``.jpe``, ``.tif`` beside ``.tiff``) and, for
        ``image/jpeg``, whichever of them the platform's mime table happens to
        list first.
        """
        return [".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"]

    async def parse(self, file: bytes | str, filename: str) -> list[Section]:
        """Describe one image and return it as a single section.

        Raises:
            `VisionError`:
                When no vision model is configured, or the configured one could
                not describe the image. Raised rather than returning an empty
                list: a picture nobody described indexes as nothing at all, and
                a document that silently holds nothing is worse than a row that
                says why it is empty.
        """
        from raven.knowledge._vision import load_vision_model

        content = file if isinstance(file, bytes) else Path(file).read_bytes()
        model = self._model or load_vision_model()
        if model is None:
            raise VisionError(
                f"{filename!r} is an image and no vision model is configured; "
                "choose one in Settings, under Default models"
            )
        # Stripped here as well as in the describer: this parser is handed a
        # model rather than building one, and what it asserts about the section
        # it returns cannot depend on who supplied it.
        text = (await model.describe(content, mime=_mime_of(filename))).strip()
        if not text:
            raise VisionError(f"the vision model returned no description for {filename!r}")
        return [
            Section(
                content=TextBlock(text=text),
                source=filename,
                metadata=section_metadata(
                    reading_order=0,
                    layout_type=LayoutType.FIGURE,
                    elements=[
                        ElementSpan(
                            reading_order=0,
                            layout_type=LayoutType.FIGURE,
                            char_start=0,
                            char_end=len(text),
                        )
                    ],
                ),
            )
        ]


def _mime_of(filename: str) -> str:
    """The media type this parser was routed by, re-derived from the name.

    The bytes are sniffed downstream anyway; this is what carries a format the
    magic-byte table does not know (BMP, TIFF) through to the preparation step.
    """
    import mimetypes

    guess = mimetypes.guess_type(filename)[0] or ""
    return guess if guess.startswith("image/") else ""


__all__ = ["ImageParser"]
