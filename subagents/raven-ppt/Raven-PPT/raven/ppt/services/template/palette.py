"""The palette an author read off the template's own pages.

Everything else about a template is read from the file: the canvas from the
presentation, the fonts from the theme part, the ground from what the master paints.
Colour is the one reading where the file and the page part company, and not at the
edges. Measured over the sixteen templates that ship here, every one of them states
its second background as `#F0F0F0` or `#E7E6E6` and not one paints that colour on
any page it ships -- 0 of 16, over every solid shape fill in them -- while the accent
is the largest non-ground fill in eight. A palette derived from the declaration is
therefore a grey palette inside templates that are not grey, and every card, band and
title row on every page came out that grey because they all default to one role.

So the reading is taken where the pages are visible. `ppt_template` already returns a
render of each example page; an author that has looked at them can say what it saw,
and what it says is kept here for the deck's whole life rather than re-derived per
build. Nothing about it is checked against a bar: a palette naming a saturated plane
is not an error to refuse, because the ink written on that plane is chosen against
the plane when the shape is drawn. What is checked is that a role name is one of the
roles and a colour is a colour, since neither of those is a judgement.
"""

from __future__ import annotations

import json
from pathlib import Path

PALETTE_FILE = "palette.json"

# The roles the derivation knows how to finish. Naming one of these replaces what
# would have been derived for it; naming anything else adds a colour to the deck, and
# both are stated the same way because to a page they are the same thing: `plane`,
# `card` and `heading` resolve a tint as `theme[tint] if tint in theme else tint`, so
# a role an author invents is a tint like any other.
#
# There is no closed list of role names, then, and the reason is what a page cannot do
# without one. A page is written one call at a time, so the compare-pair a deck sets
# up on page 3 -- `ours` against `theirs` -- does not exist in the script for page 4
# unless something carries it, and the only thing that carries anything between pages
# is this file. A vocabulary of eight would have made the four colours that deck
# actually uses expressible on one page and inconsistent across twenty.
DERIVED = (
    "background",
    "foreground",
    "accent",
    "surface",
    "muted",
    "accent_soft",
    "accent_ink",
    "grid",
)

# What a palette may not say. The faces are read from the theme part, where a template
# states them exactly and an author guessing at them would be overriding a fact with a
# preference; type sizes are the ramp's, and are not colours at all.
NOT_A_COLOUR = ("font_family", "cjk_font_family")

# Ordered, because the order is the contract the charts read: `chart_series[0]` is the
# series a page is about.
SERIES = "chart_series"


class PaletteError(ValueError):
    """A role that is not a role, or a colour that is not a colour."""


def palette_path(project) -> Path:
    from raven.ppt.services.template.inventory import template_dir

    return template_dir(project) / PALETTE_FILE


def as_palette(given: object) -> dict[str, object]:
    """`given` as a palette, or PaletteError saying which part of it is not one.

    A partial palette is the ordinary case and not a degraded one: an author sure of
    the ground and the accent and unsure of the rest states those two, and the roles
    it left out are derived from the ones it stated rather than from the file. So the
    only thing an empty reading means is "derive all of it", which is what a deck
    whose author never looked at a render already gets.

    A role the derivation has never heard of is not an error either -- it is a colour
    the deck wants in every page's theme, and the pages resolve it by name. What is
    refused is a value that is not a colour, and a key that names something read from
    the template rather than chosen.
    """
    if given is None:
        return {}
    if not isinstance(given, dict):
        raise PaletteError(f"a palette is an object of role: colour, not {type(given).__name__}")

    palette: dict[str, object] = {}
    for role, value in given.items():
        if role == SERIES:
            palette[SERIES] = [_colour(f"{SERIES}[{index}]", one) for index, one in enumerate(_series(value))]
            continue
        if role in NOT_A_COLOUR:
            raise PaletteError(
                f"{role} is read from the template itself and is not a palette's to state. "
                f"A palette states colours: {', '.join(DERIVED)}, {SERIES}, or a role of your own"
            )
        if not role or not isinstance(role, str) or not role.replace("_", "").isalnum():
            raise PaletteError(f"{role!r} is not usable as a role name; letters, digits and _ are")
        palette[role] = _colour(role, value)
    return palette


def read_palette(project) -> dict[str, object]:
    """What the author stated for this deck, or {} where it stated nothing."""
    path = palette_path(project)
    if not path.is_file():
        return {}
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A palette that cannot be read is a deck without one, not a deck that fails
        # to build: the derivation still answers and the author can state it again.
        return {}
    try:
        return as_palette(stored)
    except PaletteError:
        return {}


def write_palette(project, palette: dict[str, object]) -> dict[str, object]:
    """Keep `palette` for this deck, merged over whatever it already stated.

    Merged rather than replaced, because an author corrects one role after looking at
    a render again -- "the plane is the blue band, not the grey" -- and a call that
    replaced the palette would silently drop the six roles it did not repeat.
    """
    merged = {**read_palette(project), **palette}
    path = palette_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return merged


def _series(value: object) -> list[object]:
    if not isinstance(value, (list, tuple)):
        raise PaletteError(f"{SERIES} is a list of colours in the order the charts read them, not {value!r}")
    return list(value)


def _colour(role: str, value: object) -> str:
    text = str(value).strip()
    if len(text) == 7 and text.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in text[1:]):
        return "#" + text[1:].upper()
    raise PaletteError(f"{role} is {value!r}, which is not a colour. Colours here are #RRGGBB")
