"""Which band of a template's grid a shape belongs to.

The grid is data (`contracts.masters.Bands`: four edges and a canvas); deciding
what sits in which band is a measurement policy, so it lives here with the other
measurements rather than on the data it reads. `title_band_figures` asks it
whether a picture has climbed into the title row.
"""

from __future__ import annotations

from raven_ppt.contracts.masters import BODY, FOOTER, SPANNING, TITLE, Bands

# What share of a shape has to land in one band for the shape to belong to it.
# Below this it is a shape that crosses the grid rather than one that sits in it,
# and the two are different findings: a full-bleed background is not "in the body
# band", it is over all three.
MOSTLY = 0.8


def band_of(bands: Bands, top: float, bottom: float) -> str:
    """Which band a shape spanning `top`..`bottom` belongs to.

    By overlapping area rather than by midpoint: a card that starts in the title
    row and ends halfway down the page has its midpoint in the body and is not a
    body shape. The bands run the full width of the canvas, so every one of them
    shares the shape's own width and comparing overlapped heights orders them
    exactly as comparing areas would.

    Returns `spanning` when no band holds most of the shape. A shape with no
    height at all -- a rule is one -- overlaps nothing, so it is placed by the
    point it sits on instead.
    """
    low, high = (top, bottom) if top <= bottom else (bottom, top)
    overlaps = [
        (name, max(min(high, edge) - max(low, start), 0.0))
        for name, start, edge in (
            (TITLE, bands.title_top, bands.title_bottom),
            (BODY, bands.title_bottom, bands.body_bottom),
            (FOOTER, bands.body_bottom, bands.footer_bottom),
        )
    ]
    total = sum(share for _name, share in overlaps)
    if total <= 0:
        return _at(bands, (low + high) / 2)
    name, share = max(overlaps, key=lambda pair: pair[1])
    return name if share / total >= MOSTLY else SPANNING


def _at(bands: Bands, y: float) -> str:
    if y < bands.title_bottom:
        return TITLE
    if y < bands.body_bottom:
        return BODY
    return FOOTER
