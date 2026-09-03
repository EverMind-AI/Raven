"""How wide a string really sets, measured through FreeType on the bundled fonts.

`measure.width` predicted this module: it owns the font-free estimator and says
"the real one loads TTFs through FreeType and belongs to the assets service, which
owns the deck's fonts. `WidthMeasurer` is the seam." The seam sat empty, the
estimator ran everywhere, and 1.4MB of DejaVu shipped with no consumer -- so every
width check ran on a deliberately conservative guess that reads narrower than the
truth and under-reports rather than inventing findings. Ported from the previous
engine's `text_fit`, where it was measured against the render.

Three rules, and the third is the one that makes it trustworthy:

* Latin runs go through PIL's FreeType binding on a real TTF.
* CJK is a full em per character, deterministically, because a CJK face is not
  bundled and a Han glyph is square in every face that has it. This is exact
  where guessing is not: a page of Chinese measured as 0.55em per character
  under-reports by nearly half, which is most of a deck in this fork.
* The answer is the *maximum* of that measurement and the export dialect's own
  estimate. The renderer's metrics are not the measurer's, and the previous
  engine calibrated the gap: LibreOffice 7.4 laid the longest repro line out
  0.14% wider than PIL at 26px. Taking the max means a string never measures
  narrower than what either party will draw.

Missing fonts raise rather than falling back to estimation: a measurement that
silently changes method is a measurement that silently changes verdict.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from raven_ppt.services.measure.width import DEFAULT_MEASURER, WidthMeasurer, is_cjk_char

FONT_DIR = Path(__file__).resolve().parent / "fonts"
REGULAR_TTF = str(FONT_DIR / "DejaVuSans.ttf")
BOLD_TTF = str(FONT_DIR / "DejaVuSans-Bold.ttf")

# Point a deployment at its own faces -- a CJK TTF above all, which turns the
# full-em rule into a real measurement.
ENV_REGULAR = "RAVEN_PPT_FONT_REGULAR"
ENV_BOLD = "RAVEN_PPT_FONT_BOLD"
ENV_CJK = "RAVEN_PPT_FONT_CJK"


class FontError(RuntimeError):
    """A measurement font is missing, or Pillow is not installed."""


@dataclass(frozen=True)
class FontConfig:
    """Which faces to measure with. Empty means the bundled ones."""

    regular_path: str = ""
    bold_path: str = ""
    cjk_path: str = ""

    def resolved(self) -> FontConfig:
        return FontConfig(
            regular_path=self.regular_path or os.environ.get(ENV_REGULAR, REGULAR_TTF),
            bold_path=self.bold_path or os.environ.get(ENV_BOLD, BOLD_TTF),
            cjk_path=self.cjk_path or os.environ.get(ENV_CJK, ""),
        )


class MeasuredWidth:
    """FreeType Latin, full-em CJK, floored by the export dialect's estimate."""

    def __init__(self, config: FontConfig | None = None) -> None:
        self.config = (config or FontConfig()).resolved()
        for label, path in (("regular", self.config.regular_path), ("bold", self.config.bold_path)):
            if not os.path.isfile(path):
                raise FontError(
                    f"{label} measurement font not found at {path!r}; install it or point "
                    f"{'RAVEN_PPT_FONT_' + label.upper()} at a TTF -- silent estimation is not allowed"
                )
        if self.config.cjk_path and not os.path.isfile(self.config.cjk_path):
            raise FontError(f"cjk measurement font not found at {self.config.cjk_path!r}")
        self._fonts: dict[tuple[str, int], object] = {}

    def width(self, text: str, font_px: int, bold: bool = False) -> float:
        return max(self._freetype(text, font_px, bold), DEFAULT_MEASURER.width(text, font_px, bold))

    def _font(self, path: str, size: int):
        key = (path, size)
        font = self._fonts.get(key)
        if font is None:
            try:
                from PIL import ImageFont
            except ImportError as exc:  # pragma: no cover -- pillow is a ppt extra
                raise FontError("pillow is required for text measurement (install the 'ppt' extra)") from exc
            try:
                font = ImageFont.truetype(path, size)
            except OSError as exc:
                raise FontError(f"failed to load measurement font {path!r}: {exc}") from exc
            self._fonts[key] = font
        return font

    def _freetype(self, text: str, font_px: int, bold: bool) -> float:
        """Latin through the face, CJK a full em each, run by run.

        Split into runs because one string is often both -- a Chinese heading with
        `TarViS` in it -- and a face that has no Han glyph reports the width of its
        replacement box rather than of the character that will be drawn.
        """
        if not text:
            return 0.0
        latin = self.config.bold_path if bold else self.config.regular_path
        total = 0.0
        for run, run_is_cjk in _runs(text):
            if run_is_cjk and not self.config.cjk_path:
                total += len(run) * font_px
            else:
                total += self._font(self.config.cjk_path if run_is_cjk else latin, font_px).getlength(run)
        return float(total)


def _runs(text: str):
    """`text` split into maximal CJK / non-CJK runs, in order."""
    current: list[str] = []
    current_cjk = False
    for char in text:
        char_cjk = is_cjk_char(char)
        if current and char_cjk != current_cjk:
            yield "".join(current), current_cjk
            current = []
        current.append(char)
        current_cjk = char_cjk
    if current:
        yield "".join(current), current_cjk


_MEASURER: WidthMeasurer | None = None


def measurer(config: FontConfig | None = None) -> WidthMeasurer:
    """The measurer to hand a check, falling back to the estimator if fonts are gone.

    The fallback is here rather than inside `MeasuredWidth` so that it is one
    decision in one place: a caller that must not degrade constructs the class
    directly and gets the exception.
    """
    global _MEASURER
    if config is not None:
        return MeasuredWidth(config)
    if _MEASURER is None:
        try:
            _MEASURER = MeasuredWidth()
        except FontError:
            _MEASURER = DEFAULT_MEASURER
    return _MEASURER
