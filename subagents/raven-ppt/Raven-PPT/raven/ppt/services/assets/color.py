"""Colour arithmetic, so contrast is computed rather than asserted.

Every palette in this package was checked against WCAG by hand once, and a
palette the model seeds at runtime gets no such review -- so the same numbers
that were used to check the presets are what derive the runtime ones. That is
the whole reason this module is separate from the themes it serves: the
guarantee "body copy clears 7:1 on its ground" has to be a function, because a
theme assembled from a topic word cannot be reviewed by a person.

Paints are canonical uppercase ``#RRGGBB`` throughout. The export dialect
treats every fill as opaque, so there is no alpha channel anywhere in this
package and a tint is a pre-mixed solid colour, not a transparency.
"""

from __future__ import annotations

import re

HEX_RE = re.compile(r"^#[0-9A-F]{6}$")

BLACK = "#000000"
WHITE = "#FFFFFF"


def is_canonical_hex(value: str) -> bool:
    """Canonical means uppercase ``#RRGGBB``.

    Lowercase would round-trip through the compilers unharmed, but the vendored
    SVG dialect and the icon renderer both compare paint strings, so two
    spellings of one colour read as two colours.
    """
    return bool(HEX_RE.match(value))


def hex_to_rgb(color: str) -> tuple[float, float, float]:
    return int(color[1:3], 16) / 255.0, int(color[3:5], 16) / 255.0, int(color[5:7], 16) / 255.0


def rgb_to_hex(r: float, g: float, b: float) -> str:
    def clamp(value: float) -> int:
        return max(0, min(255, round(value * 255)))

    return f"#{clamp(r):02X}{clamp(g):02X}{clamp(b):02X}"


def relative_luminance(color: str) -> float:
    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = hex_to_rgb(color)
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def mix(color: str, other: str, weight: float) -> str:
    """``weight=0`` yields ``color``, ``weight=1`` yields ``other``."""
    (r1, g1, b1), (r2, g2, b2) = hex_to_rgb(color), hex_to_rgb(other)
    return rgb_to_hex(r1 + (r2 - r1) * weight, g1 + (g2 - g1) * weight, b1 + (b2 - b1) * weight)


def ensure_contrast(color: str, background: str, target: float) -> str:
    """Darken or lighten ``color`` just far enough to clear ``target``.

    Binary search on the mix weight toward whichever pole the background is not,
    which keeps the hue the caller chose: clamping to black would meet the ratio
    and throw away the palette. When even the pole cannot reach the target the
    pole is returned, because the alternative is refusing to draw.
    """
    if contrast_ratio(color, background) >= target:
        return color
    pole = BLACK if relative_luminance(background) >= 0.5 else WHITE
    if contrast_ratio(pole, background) < target:
        return pole
    low, high = 0.0, 1.0
    for _ in range(24):
        mid = (low + high) / 2
        if contrast_ratio(mix(color, pole, mid), background) >= target:
            high = mid
        else:
            low = mid
    return mix(color, pole, high)


def hue(color: str) -> float:
    """Hue in degrees; grey returns 0."""
    r, g, b = hex_to_rgb(color)
    high, low = max(r, g, b), min(r, g, b)
    if high == low:
        return 0.0
    span = high - low
    if high == r:
        sector = ((g - b) / span) % 6
    elif high == g:
        sector = (b - r) / span + 2
    else:
        sector = (r - g) / span + 4
    return sector * 60.0


def hue_distance(a: float, b: float) -> float:
    """Shortest way round the wheel, so 350 and 10 are 20 apart, not 340."""
    delta = abs(a - b) % 360.0
    return min(delta, 360.0 - delta)
