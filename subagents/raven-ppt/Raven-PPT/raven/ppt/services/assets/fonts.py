"""The faces a deck may name, and the ones measurement is done with.

Two different questions live here and they are easy to confuse. The *named*
face is what the theme writes into the deck and what the viewer's machine will
resolve; the *measurement* face is the TTF this process loads to find out how
wide a line is. They are deliberately not the same font, and neither list is
open: font size and font name are never exposed to the model (see the hard
invariants in raven/ppt/AGENTS.md), so both are engine-side data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Faces a theme may name, and why the list is closed.
#
# Text fitting measures every line with the bundled DejaVu Sans and holds only a
# few percent in reserve for renderer-side substitution, so a face wider than
# the measurement font pushes approved lines past their slot on the viewer's
# machine. Each entry below was measured against that baseline over
# representative deck text and lands at 0.81-1.02 of it -- inside the reserve,
# and mostly under it, so real lines end early rather than overflowing.
#
# Each name also resolves two ways that matter. On this renderer fontconfig maps
# it to a metric-compatible libre clone (Liberation, URW, Nimbus), so the review
# renders the model inspects match what it measured. On the viewer's Windows or
# macOS the same name resolves to the real face, whose metrics the clone was
# built to match. A face that only exists on one of those two sides would make
# the review renders lie, which is why the list is short.
MEASURED_SAFE_FONTS: dict[str, str] = {
    "Arial": "neutral grotesque; Liberation Sans here, Arial on the viewer",
    "Helvetica": "precise grotesque; Nimbus Sans here, Helvetica/Arial on the viewer",
    "Times New Roman": "conventional academic serif; Liberation Serif here",
    "Cambria": "warm text serif with sturdy counters; DejaVu Serif here",
    "Century Schoolbook": "wide-counter text serif, high legibility; C059 here",
    "Bookman Old Style": "heavy editorial serif; URW Bookman here",
}

# CJK faces a theme may name, and why these two.
#
# The Latin list above works because a metric-compatible libre clone exists on
# this renderer for every name -- so a review render matches what was measured,
# and the viewer's real face matches the clone. No such clone exists for CJK: the
# names a Chinese deck would reach for first, 微软雅黑 (Microsoft YaHei) and
# 苹方 (PingFang SC), resolve to DejaVu here, which has no Han glyphs at all, so
# every review render would show tofu and the model would be inspecting a lie.
#
# Noto CJK resolves properly on this renderer (fonts-noto-cjk), is the same
# design as Source Han Sans/Serif, and on a viewer's Windows or macOS a missing
# name falls back to that platform's own CJK sans or serif -- a substitution
# within the same class, at the same em width, rather than to a face with no
# glyphs. That is the trade this list takes: the loop the model corrects with is
# honest, and the delivered file degrades sideways rather than into boxes.
#
# A deployment that knows its audience can override the pair with the env vars
# below -- naming 微软雅黑 for an all-Windows readership is a legitimate choice,
# it just cannot be the default here.
CJK_SAFE_FONTS: dict[str, str] = {
    "Noto Sans CJK SC": "CJK grotesque; pairs with the Latin sans faces",
    "Noto Serif CJK SC": "CJK serif with Song-style stems; pairs with the Latin serifs",
}

# Which of the Latin faces are serifs, so a theme's CJK companion matches its
# class without every theme having to name one.
SERIF_FACES = frozenset({"Times New Roman", "Cambria", "Century Schoolbook", "Bookman Old Style"})

ENV_CJK_SANS_NAME = "RAVEN_PPT_CJK_SANS"
ENV_CJK_SERIF_NAME = "RAVEN_PPT_CJK_SERIF"

# The face a theme gets when it names none, by whether its Latin face is a serif.
CJK_SANS = "Noto Sans CJK SC"
CJK_SERIF = "Noto Serif CJK SC"


def cjk_face(serif: bool) -> str:
    """The CJK family to name, honouring a deployment's override."""
    if serif:
        return os.environ.get(ENV_CJK_SERIF_NAME) or CJK_SERIF
    return os.environ.get(ENV_CJK_SANS_NAME) or CJK_SANS


# Bundled fonts remove environment variance: measurement must produce the same
# metrics on a dev machine and in an eval container, and a system font that
# differs by a point release silently changes what fits. System paths are the
# fallback for a checkout whose packaged copies are missing.
BUNDLED_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_SYSTEM_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_SYSTEM_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# Not bundled -- a CJK face is 20MB and the deterministic full-em advance is
# correct for Han. Used when present because it is not correct for CJK
# punctuation, which is where a measured line differs from a counted one.
_SYSTEM_CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

ENV_REGULAR = "RAVEN_PPT_FONT_REGULAR"
ENV_BOLD = "RAVEN_PPT_FONT_BOLD"
ENV_CJK = "RAVEN_PPT_FONT_CJK"


class FontError(RuntimeError):
    """A measurement font is missing or unloadable.

    Raised rather than estimated: a fitting pass that quietly falls back to
    character counting reports slots as fitting that do not, and the failure
    only surfaces in the exported deck.
    """


def _bundled_or_system(filename: str, system_path: str) -> str:
    bundled = BUNDLED_FONT_DIR / filename
    return str(bundled) if bundled.is_file() else system_path


@dataclass(frozen=True)
class MeasurementFonts:
    """The TTF paths a measurement pass loads.

    ``cjk`` is empty by default and that is a supported state, not a missing
    asset: no CJK face is bundled, and CJK glyphs measure exactly one em anyway,
    so the deterministic full-em advance is used when no path is configured.
    """

    regular: str
    bold: str
    cjk: str = ""


def measurement_fonts(
    *,
    regular: str = "",
    bold: str = "",
    cjk: str = "",
) -> MeasurementFonts:
    """Resolve measurement fonts: explicit argument, then env var, then bundled.

    Resolved on call rather than at import so a test or an eval container can
    point the env vars somewhere and have it take effect.
    """
    return MeasurementFonts(
        regular=regular or os.environ.get(ENV_REGULAR) or _bundled_or_system("DejaVuSans.ttf", _SYSTEM_REGULAR),
        bold=bold or os.environ.get(ENV_BOLD) or _bundled_or_system("DejaVuSans-Bold.ttf", _SYSTEM_BOLD),
        cjk=cjk or os.environ.get(ENV_CJK) or (_SYSTEM_CJK if Path(_SYSTEM_CJK).is_file() else ""),
    )
