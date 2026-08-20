"""The template's own palette, in the shape a build script draws with.

The build directory carries `ppt_theme.py` and ten reviewed themes, and the author
is told to take its colours from one of them. With a template bound that
instruction is wrong, and the correction used to be a sentence in a tool
description -- which is exactly the kind of guarantee this package keeps out of
prose. An author that picked `ink-graphite` inside a green corporate template would
produce a deck that fails nothing.

So the ten become one. `THEMES` still exists, `rgb` still works, and whatever the
author picks out of it *is* the template's, because it is the only thing in there.
By construction rather than by refusal -- removing the module outright would take
the icon helper with it, since that is where `rgb` lives.

Two things about the mapping, both learned the hard way.

**Scheme names, resolved through the master's colour map.** A dark template is dark
by mapping `bg1` to `dk1`, and its master paints `schemeClr val="bg1"`. Reading the
theme's scheme straight off -- `background` from `lt1`, `foreground` from `dk1` --
therefore says "white page, black type" about a black deck, which is what it did:
an author was handed exactly the inverse of the house style, painted white panels
and black text onto a black master, and the deck came back unreadable. The map is
the whole difference and `decompile` had been applying it for months.

**Four of the nine fields are relationships, not colours, so they are mixed rather
than read.** What a build script needs of `surface` is a plane that sits on the
ground, of `muted` type that recedes from the main type, of `accent_soft` a quiet
tile, of `grid` a findable hairline. OOXML states none of those. It states `bg2` and
`tx2`, a "second pair" that guarantees nothing and that templates fill in casually:
on the deck this was found on, `bg2` is a mid blue-grey that as a card on black is
louder than the accent, and `tx2` is within a percent of white. Mixing towards the
ground guarantees the relationship, and it is what the templates do themselves --
their cards are `lt2` at 15% over black.

The one thing not derived is the accent. That is the template's own, always.
"""

from __future__ import annotations

from raven.ppt.services.template.inventory import TemplateInventory

# Which scheme name answers each field a build script draws with. Scheme names
# rather than theme slots, because the slot a name means is the master's decision.
_ROLES = (
    ("background", "bg1"),
    ("foreground", "tx1"),
    ("accent", "accent1"),
)

_SERIES = ("accent1", "accent2", "accent3", "accent4", "accent5", "accent6")

# How far each mixed colour travels from its source towards the other pole. Chosen
# so the derived palette clears the same contrast targets the reviewed presets do,
# on light grounds and dark ones alike -- see the measurement in the test.
_SURFACE_MIX = 0.10  # the ground, lifted just enough to read as a plane on itself
_MUTED_MIX = 0.38  # the ink, receded but still comfortably legible
_SOFT_MIX = 0.82  # the accent, quietened to a tile
_GRID_MIX = 0.62  # the ink, down to a hairline that is findable and not a border

# What a field falls back to when the template's scheme does not carry its slot.
# Black on white rather than a colour of our own: a missing slot means the template
# did not state one, and inventing an accent here would put a colour in the deck
# that is in nobody's house style.
_FALLBACK = {
    "background": "#FFFFFF",
    "foreground": "#111111",
    "accent": "#111111",
}


def theme_of(inventory: TemplateInventory) -> dict[str, object]:
    """The template as one theme entry, in the catalogue's own shape."""
    palette = dict(inventory.theme_colours)
    mapping = dict(inventory.colour_map)
    resolved = {name: palette.get(mapping.get(scheme, scheme)) for name, scheme in _ROLES}
    entry: dict[str, object] = {name: resolved.get(name) or _FALLBACK[name] for name, _ in _ROLES}

    ground, ink, accent = str(entry["background"]), str(entry["foreground"]), str(entry["accent"])
    entry["surface"] = _mixed(ground, ink, _SURFACE_MIX)
    entry["muted"] = _mixed(ink, ground, _MUTED_MIX)
    entry["accent_soft"] = _mixed(accent, ground, _SOFT_MIX)
    entry["grid"] = _mixed(ink, ground, _GRID_MIX)

    series = [palette[slot] for slot in _SERIES if slot in palette]
    entry["chart_series"] = series or [accent, str(entry["muted"]), str(entry["grid"])]
    # The minor face, which is the one body copy is set in; `_fonts` reports the
    # major first, so the last entry is the closer answer when there are two.
    entry["font_family"] = inventory.fonts[-1] if inventory.fonts else "Arial"
    return entry


def theme_name(inventory: TemplateInventory) -> str:
    """What the single theme is called, so a script can name it in one place."""
    return inventory.path.stem.replace(" ", "-").replace("_", "-").lower() or "template"


def _mixed(colour: str, ground: str, towards: float) -> str:
    """`colour` moved `towards` the way to `ground`, as #RRGGBB."""
    one, two = _rgb(colour), _rgb(ground)
    if one is None or two is None:
        return colour
    blended = tuple(round(a + (b - a) * towards) for a, b in zip(one, two, strict=True))
    return "#{:02X}{:02X}{:02X}".format(*blended)


def _rgb(colour: str) -> tuple[int, int, int] | None:
    text = str(colour).lstrip("#")
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None
