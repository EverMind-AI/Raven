"""Rendered ground truth: where every word actually landed.

The declared geometry of a text frame says where the box is, not where the copy
inside it ended up. The renderer wraps the copy to the frame's width, grows
table rows to fit their cells, and substitutes a font whose metrics are not the
ones the deck was written against -- so a box 4in wide holding three lines on
paper is indistinguishable, in the .pptx, from the same box holding five.

Estimating it anyway was tried and misfired: the estimate's font and
line-spacing assumptions produced findings on pages a render shows to be clean.
The render pipeline already produces a PDF, and a PDF carries every word's true
bbox, so this is read rather than guessed. Both coordinate systems are points
with the origin at the top left, which is why a rule's position can come off
the .pptx and be compared against a word's position off the render.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from raven_ppt.contracts import WordBox
from raven_ppt.services.measure.geometry import Rect


def rect(word: WordBox) -> Rect:
    """A word's box as the rectangle the geometry helpers work with.

    A function rather than a property on the contract: `Rect` belongs to
    measurement and the contracts layer may not import a service, which is the
    layering rule that keeps the render contract usable by anything.
    """
    return Rect(word.x0, word.y0, word.x1, word.y1)


def by_page(words: Iterable[WordBox]) -> dict[int, list[WordBox]]:
    """The words grouped by the page they were painted on, order preserved."""
    grouped: dict[int, list[WordBox]] = {}
    for word in words:
        grouped.setdefault(word.page, []).append(word)
    return grouped


_WORD_BBOX_RE = re.compile(r'xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>')


def parse_bbox_xml(bbox_xml: str) -> list[WordBox]:
    """Read `pdftotext -bbox` output into word boxes.

    Line-oriented rather than an XML parse: the interesting part of the format
    is that `<page>` opens a page and every `<word>` after it belongs to that
    page, and a partial or truncated document should still yield the pages it
    did contain rather than raising.
    """
    words: list[WordBox] = []
    page = 0
    for line in bbox_xml.splitlines():
        if "<page" in line:
            page += 1
        match = _WORD_BBOX_RE.search(line)
        if match and page:
            x0, y0, x1, y1 = (float(match.group(index)) for index in range(1, 5))
            words.append(WordBox(page=page, text=match.group(5), x0=x0, y0=y0, x1=x1, y1=y1))
    return words


def words_from_pdf(pdf_path: Path) -> list[WordBox] | None:
    """Every rendered word in a document, or None when it cannot be read.

    None and empty mean different things and callers act on the difference: no
    `pdftotext` on the box means no signal, and a check with no signal reports
    nothing rather than reporting that every page is clean.
    """
    bbox_xml = _pdf_bbox_xml(pdf_path)
    return parse_bbox_xml(bbox_xml) if bbox_xml is not None else None


def _pdf_bbox_xml(pdf_path: Path) -> str | None:
    import shutil
    import subprocess

    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        return None  # no pdftotext, no signal -- the declared-geometry checks still run
    try:
        extracted = subprocess.run(
            [pdftotext, "-bbox", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return extracted.stdout if extracted.returncode == 0 else None
