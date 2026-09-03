"""How wide a string is, for the one check a render cannot see.

A label that wraps inside a box too narrow for it overlaps nothing: '01' set in
a box guessed at 0.25in becomes a stacked 0 over 1, and every rendered word
still sits exactly where it belongs. So that one check has to be decided
against the copy's width before it is drawn, which needs a measurer.

Two exist. The real one loads TTFs through FreeType and belongs to the assets
service, which owns the deck's fonts; the one here is a clean-room
reimplementation of the export dialect's character-class estimator, and its
value is that it needs no font file, so a measurement never depends on what is
installed. It reads *narrower* than FreeType on most strings, which makes it the
conservative choice for a gate: the check under-reports rather than inventing
findings on pages that are fine.

`WidthMeasurer` is the seam. When the assets service lands, the caller passes
its measurer in and this estimator stays as the fallback.
"""

from __future__ import annotations

from typing import Protocol


class WidthMeasurer(Protocol):
    """Anything that can say how wide a string sets at a size."""

    def width(self, text: str, font_px: int, bold: bool = False) -> float: ...


def is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x2E80 <= code <= 0x303F
        or 0x3040 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE4F
        or 0xFF00 <= code <= 0xFFEF
    )


# Character-class widths adapted from the vendored svg_to_pptx estimator, which
# is what grades exported text against its module bounds -- so a string measured
# here the way that estimator measures it is measured the way the export will.
_WIDE_CHARS = set("mMwWOQ%")
_NARROW_CHARS = set("iIlj!|")
_DIALECT_HEADROOM_BASE = 1.06
_DIALECT_HEADROOM_CAPS = 1.12
_DIALECT_BOLD_FACTOR = 1.05


def _char_em(ch: str) -> float:
    if is_cjk_char(ch):
        return 1.0
    if ch == " ":
        return 0.3
    if ch in _WIDE_CHARS:
        return 0.75
    if ch in _NARROW_CHARS:
        return 0.3
    return 0.55


class EstimatedWidth:
    """Font-free width estimation, to the same rules the exporter uses."""

    def width(self, text: str, font_px: int, bold: bool = False) -> float:
        if not text:
            return 0.0
        width_em = sum(_char_em(ch) for ch in text)
        if bold:
            width_em *= _DIALECT_BOLD_FACTOR
        cased = [ch for ch in text if ch.lower() != ch.upper()]
        caps = sum(1 for ch in cased if ch.isupper()) / len(cased) if cased else 0.0
        headroom = _DIALECT_HEADROOM_BASE + (_DIALECT_HEADROOM_CAPS - _DIALECT_HEADROOM_BASE) * caps
        return width_em * font_px * headroom


DEFAULT_MEASURER: WidthMeasurer = EstimatedWidth()
