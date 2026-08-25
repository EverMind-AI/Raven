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

from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from raven.ppt.services.assets.fonts import SERIF_FACES, cjk_face
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

    # What the master actually paints wins over what the map says it would paint.
    # One template maps `bg1` to a #2F2F2F it never uses and covers every page with
    # accent1 instead: read off the map alone, a purple deck came back as a grey one,
    # and every plane derived from that grey landed on the purple looking dirty.
    painted = inventory.rendered_ground or _painted(inventory, palette, mapping)
    if painted:
        entry["background"] = painted
        # A ground that is the accent leaves nothing to accent with. The template
        # itself answers this -- it alternates accent1 grounds with accent2 ones --
        # so take the next slot that is a different colour.
        if str(entry["accent"]).upper() == painted.upper():
            entry["accent"] = _contrasting_accent(palette, painted) or entry["accent"]

    ground, ink, accent = str(entry["background"]), str(entry["foreground"]), str(entry["accent"])
    entry["surface"] = _mixed(ground, ink, _SURFACE_MIX)
    entry["muted"] = _receded(ink, ground, str(entry["surface"]))
    entry["accent_soft"] = _mixed(accent, ground, _SOFT_MIX)
    entry["accent_ink"] = _readable(accent, str(entry["accent_soft"]), ink)
    entry["grid"] = _mixed(ink, ground, _GRID_MIX)

    series = [palette[slot] for slot in _SERIES if slot in palette]
    entry["chart_series"] = series or [accent, str(entry["muted"]), str(entry["grid"])]
    entry["font_family"], entry["cjk_font_family"] = _faces(inventory.fonts)
    return entry


def _faces(fonts: Sequence[str]) -> tuple[str, str]:
    """The Latin face to set, and the CJK face to set beside it.

    Two defects were in the one line this replaces, and both showed on the same
    Chinese template, whose scheme names ('Arial', '微软雅黑').

    It took the *last* face as the one to set copy in, on the reasoning that a
    scheme lists its major face first and body copy is set in the minor. True of a
    Latin scheme; on this one it made 微软雅黑 the Latin face -- and that name
    resolves to DejaVu Sans on this renderer, which has no Han glyphs, so the face
    the engine recommended could not set the language the deck was in. So the
    minor-face preference stays, applied among the Latin faces only.

    And it emitted no CJK face at all, while `ppt_theme`, `ppt_layout.write` and
    the authoring skill each tell an author to read `cjk_font_family` from the
    theme. With a template bound the key was absent, so the documented path was a
    KeyError and one run hand-rolled its own -- through an attribute that does not
    exist, so the deck it shipped declared no East-Asian face on any of 330 runs
    and left every Han character to the viewer's fallback.

    The template's own CJK face is preferred over the reviewed default, because it
    is the face its designer chose and it is what a viewer's Office resolves. It
    costs nothing here: measured on this renderer, a page naming 微软雅黑 and the
    same page naming Noto Sans CJK SC come out with the same ink to the pixel --
    LibreOffice substitutes per glyph, so an uninstalled Han face is not tofu.
    """
    latin = [face for face in fonts if not _is_cjk_face(face)]
    cjk = [face for face in fonts if _is_cjk_face(face)]
    face = latin[-1] if latin else "Arial"
    return face, (cjk[-1] if cjk else cjk_face(serif=face in SERIF_FACES))


# Han-family names that are spelled in ASCII, so "not ASCII" alone would miss them.
# Matched on the leading words rather than exactly: the same family ships as
# "PingFang SC", "PingFang TC", "Source Han Sans CN", "Microsoft YaHei UI".
_CJK_FAMILIES = (
    "microsoft yahei",
    "microsoft jhenghei",
    "pingfang",
    "source han",
    "noto sans cjk",
    "noto serif cjk",
    "simsun",
    "simhei",
    "simkai",
    "songti",
    "heiti",
    "kaiti",
    "fangsong",
    "hiragino",
    "yu gothic",
    "ms gothic",
    "ms mincho",
    "meiryo",
    "malgun gothic",
    "nanum",
)


def _is_cjk_face(name: str) -> bool:
    """Whether this family name is a Han/Kana face rather than a Latin one."""
    if any(ord(character) > 0x2E7F for character in name):
        return True
    lowered = name.strip().lower()
    return any(lowered.startswith(family) for family in _CJK_FAMILIES)


# How coarsely a page's colours are counted before the commonest is taken. A ground
# is never one exact value across a render -- JPEG artefacts, a gradient, antialiased
# artwork over it -- and counting exact triples finds a thousand near-identical
# neighbours instead of one ground.
_QUANTUM = 12
# Under this share of the page there is no ground worth naming: a spread of
# photographs, or a page whose artwork covers it.
_GROUND_SHARE = 0.09


def ground_of(pages: Sequence[Path]) -> str | None:
    """The colour the most of these rendered pages are, or None when there is no one
    colour.

    This is the only reading of a template's ground that cannot be lied to. What the
    file declares is a claim about the ground and the two part company often: of 119
    templates measured against their own renders, the declared ground was right for
    73. Eleven were not merely off but inverted -- white declared over a blue page,
    black declared over a white one -- because the colour a reader sees is painted by
    a shape on the layout and the theme underneath it was never touched.
    """
    from PIL import Image

    tally: Counter[tuple[int, int, int]] = Counter()
    counted = 0
    for page in pages:
        try:
            image = Image.open(page).convert("RGB").resize((200, 112))
        except OSError:
            continue
        counted += 1
        tally.update(
            (r // _QUANTUM * _QUANTUM, g // _QUANTUM * _QUANTUM, b // _QUANTUM * _QUANTUM)
            for r, g, b in image.getdata()
        )
    if not counted:
        return None
    (red, green, blue), seen = tally.most_common(1)[0]
    if seen < _GROUND_SHARE * counted * 200 * 112:
        return None
    return f"#{red:02X}{green:02X}{blue:02X}"


def _painted(inventory: TemplateInventory, palette: dict, mapping: dict) -> str | None:
    """`painted_ground` resolved to a hex, or None when there is nothing to resolve."""
    stated = inventory.painted_ground
    if not stated:
        return None
    if stated.startswith("#"):
        return stated
    return palette.get(mapping.get(stated, stated))


def _contrasting_accent(palette: dict, ground: str) -> str | None:
    """The first accent slot the template holds that is not the ground itself."""
    for slot in _SERIES:
        value = palette.get(slot)
        if value and value.upper() != ground.upper():
            return value
    return None


def theme_name(inventory: TemplateInventory) -> str:
    """What the single theme is called, so a script can name it in one place."""
    return inventory.path.stem.replace(" ", "-").replace("_", "-").lower() or "template"


# What secondary copy has to clear. It is set small -- a caption, a sub-line, a
# unit -- so it answers to the body target rather than the large-type one.
_LEGIBLE = 4.5


def _receded(ink: str, ground: str, surface: str) -> str:
    """`ink`, taken back towards the ground as far as it stays readable on both.

    `_MUTED_MIX` alone assumes a ground at one end of the range: mix 38% of a white
    ground into black ink and the result still reads. A mid-tone coloured ground
    reaches neither end -- #C2B1FF on the #6E46FF it sits on measured 2.77:1, and a
    model handed that palette overrode `muted` to flat white in its own build script
    rather than use it. So the mix is a starting point and the contrast decides how
    much of it survives.
    """
    steps = int(_MUTED_MIX * 100)
    for share in range(steps, -1, -2):
        candidate = _mixed(ink, ground, share / 100)
        if min(_contrast(candidate, ground), _contrast(candidate, surface)) >= _LEGIBLE:
            return candidate
    return ink


# What the contrast check asks of large type, which is what an accent is used for:
# a key number, a card's heading, a marker. Below it the check reports the page.
_READABLE = 3.0
# How far towards the ink an accent may be darkened before it stops being the
# template's colour and starts being a colour of ours.
_DARKEST = 0.75


def _readable(accent: str, on: str, ink: str) -> str:
    """`accent`, darkened until it can be read on `on`.

    An accent is mixed towards the background to make `accent_soft`, so the two share
    a hue and the accent cannot be written on its own tint. Measured on one template:
    #78A4AF on its own #E7EFF1 is 2.33:1, and the same accent on pure white is 2.69:1
    -- there is no ground in the deck it reads on. So the palette carries a second
    form of it for type, darkened along the way to the template's own ink rather than
    replaced by that ink, which keeps the hue the template chose.

    Returns the accent itself when it already reads, so a template with a strong
    accent gets exactly the colour it stated.
    """
    for step in range(0, int(_DARKEST * 20) + 1):
        candidate = _mixed(accent, ink, step / 20)
        if _contrast(candidate, on) >= _READABLE:
            return candidate
    return _mixed(accent, ink, _DARKEST)


def _contrast(one: str, two: str) -> float:
    first, second = _luminance(one), _luminance(two)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def _luminance(colour: str) -> float:
    parts = _rgb(colour)
    if parts is None:
        return 0.0
    channels = []
    for value in parts:
        share = value / 255
        channels.append(share / 12.92 if share <= 0.03928 else ((share + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


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
