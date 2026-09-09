"""The horizontal grid a template's content pages agree on.

A deck built inside somebody's template has a coordinate system whether or not
anything reads it: the strip the title row occupies, the strip the page numbers
and the footer rule occupy, and everything between them. Checks that want to say
"the main visual fills 40% of the content area" or "this card has climbed into
the title row" need that grid stated once, in the template's own inches, rather
than re-derived by each of them off whatever it happens to see on one page.

This module is only the shape of that statement. Which band a given shape belongs
to is a measurement, `services/measure/bands.band_of`. Deriving the grid from a
template is `services/template/bands.py`, which writes it beside the template as `bands.json`;
consumers read it with `Bands.from_json(json.loads(path.read_text()))` and get
None when nothing derived it. A consumer holding None has no coordinate system,
which is not the same as a page being fine -- it is the absence of a judgement,
and the caller returns no findings rather than falling back to numbers of its own.
"""

from __future__ import annotations

from dataclasses import dataclass

TITLE = "title"
BODY = "body"
FOOTER = "footer"
SPANNING = "spanning"

_FIELDS = ("title_top", "title_bottom", "body_bottom", "footer_bottom", "canvas_w", "canvas_h")


@dataclass(frozen=True)
class Bands:
    """The three horizontal bands a deck's content pages agree on, in inches.

    Content lives in `body`. A page that puts content in `title` or `footer` is
    not a different design, it is a page that lost the deck's own grid.
    """

    title_top: float
    title_bottom: float
    body_bottom: float
    footer_bottom: float
    canvas_w: float
    canvas_h: float

    @property
    def body(self) -> tuple[float, float]:
        """(top, bottom) of the content band, in inches."""
        return (self.title_bottom, self.body_bottom)

    @property
    def body_area(self) -> float:
        """The content band's area in square inches, the denominator of a share.

        A predicate like "the main visual carries 40% of the page" is about the
        page's content area and not about the page: measured against the whole
        canvas, a picture filling the body band of this template reads as 78% of
        one and 100% of the other, and only one of those numbers is answerable.
        """
        return max(self.canvas_w, 0.0) * max(self.body_bottom - self.title_bottom, 0.0)

    def to_json(self) -> dict:
        return {name: getattr(self, name) for name in _FIELDS}

    @classmethod
    def from_json(cls, data: dict) -> Bands:
        """`data` as bands, or ValueError saying it is not a grid.

        The edges have to be in order and the canvas has to have a size, because
        a caller that reads a corrupt file into a grid does not fail -- it starts
        answering questions about a coordinate system nothing measured.
        """
        try:
            values = {name: float(data[name]) for name in _FIELDS}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"not a band grid: {exc}") from exc
        edges = [values["title_top"], values["title_bottom"], values["body_bottom"], values["footer_bottom"]]
        if edges != sorted(edges):
            raise ValueError(f"band edges are not in order: {edges}")
        if values["canvas_w"] <= 0 or values["canvas_h"] <= 0:
            raise ValueError(f"a canvas is {values['canvas_w']}x{values['canvas_h']}in, which is not a canvas")
        return cls(**values)
