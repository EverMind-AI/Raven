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

**Four of the nine fields are relationships, not colours, so what the template
states is checked before it is taken.** What a build script needs of `surface` is a
plane that sits on the ground, of `muted` type that recedes from the main type, of
`accent_soft` a quiet tile, of `grid` a findable hairline. OOXML names none of those
relationships.

For the last two it names nothing at all, so they stay mixed. A scheme has twelve
slots and none of them is a hairline: `hlink` and `folHlink` are type colours with a
legibility contract, which is the opposite of what a rule wants, and `folHlink`
being grey on every bundled template is one vendor's habit rather than anything the
format says. `accent2` is the next series colour and not a tint of the first --
measured across the ten it is up to 176 degrees off `accent1`'s hue, it is the
louder of the two against the ground on two of them, and it is `chart_series[1]` on
all ten, so a tile painted in it would read as data.

The first two the scheme does name, as `bg2` and `tx2`, a "second pair" that carries
the category but not the amount: on the deck the mixing was written for, `bg2` is a
mid blue-grey that as a card on black is louder than the accent, and `tx2` is within
a percent of white. So the stated colour is preferred and the mix becomes what it
falls back to, because the amount is measurable here and the authors were already
reaching past us for the template's own: across five 20-page decks built inside
`5407617`, the `#F0F0F0` its `bg2` states was the dominant plane on two of them, 45%
and 78% of all fill area, and the `#778495` of its `tx2` appears 41 times in a
third. What is honoured is the declaration and that reach rather than a measured
house plane -- the twelve pages `5407617` itself ships paint almost none of its own
`bg2` -- and what decides is a comparison against the palette rather than a
threshold of ours: `_stated_muted` says which. On those ten
`bg2` is taken and `tx2` is not.

The one thing not derived is the accent. That is the template's own, always.
"""

from __future__ import annotations

import colorsys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from raven_ppt.services.assets.fonts import SERIF_FACES, cjk_face
from raven_ppt.services.template.inventory import TemplateInventory

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
# on light grounds and dark ones alike -- see the measurement in the test. The first
# two are also the reference the template's own `bg2` and `tx2` are measured against
# before either is preferred over them; the last two are the only source there is.
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


def theme_of(inventory: TemplateInventory, stated: dict[str, object] | None = None) -> dict[str, object]:
    """The template as one theme entry, in the catalogue's own shape.

    `stated` is what an author read off the template's own renders, in whole or in
    part, and it wins over every reading taken from the file -- see `palette.py` for
    why colour is the one reading where the file and the page part company. What it
    leaves out is derived, and derived from what it stated rather than from the slots:
    an author that names the accent and nothing else gets its tile, its ink and its
    plane in that accent, because a palette half read off the page and half off a
    declaration the page contradicts is neither.
    """
    said = dict(stated or {})
    palette = dict(inventory.theme_colours)
    mapping = dict(inventory.colour_map)
    resolved = {name: _slot(palette, mapping, scheme) for name, scheme in _ROLES}
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

    # The three the author is surest of, and the three every other role is built on,
    # so they are taken before anything is mixed from them.
    for role in ("background", "foreground", "accent"):
        if role in said:
            entry[role] = said[role]

    ground, ink, accent = str(entry["background"]), str(entry["foreground"]), str(entry["accent"])
    entry["surface"] = _visible(str(said.get("surface") or _plane(accent, ground)), accent, ground)
    surface = str(entry["surface"])
    receded = _receded(ink, ground, surface)
    entry["muted"] = (
        said.get("muted") or _stated_muted(_slot(palette, mapping, "tx2"), ground, surface, receded) or receded
    )
    entry["accent_soft"] = _louder(
        str(said.get("accent_soft") or _mixed(accent, ground, _SOFT_MIX)), surface, accent, ground
    )
    entry["accent_ink"] = said.get("accent_ink") or _readable(accent, str(entry["accent_soft"]), ink)
    entry["grid"] = said.get("grid") or _mixed(ink, ground, _GRID_MIX)

    series = [palette[slot] for slot in _SERIES if slot in palette]
    entry["chart_series"] = (
        said.get("chart_series")
        or series
        or [
            accent,
            str(entry["muted"]),
            str(entry["grid"]),
        ]
    )
    entry["font_family"], entry["cjk_font_family"] = _faces(inventory.fonts)
    # And whatever else the author named. A role the derivation does not know is a
    # colour this deck wants every page to be able to reach by name -- the pair a
    # comparison is drawn in, a second accent -- and a page reaches it through the
    # theme it is handed, so it has to be in the theme.
    for role, colour in said.items():
        entry.setdefault(role, colour)
    return entry


def _slot(palette: dict, mapping: dict, scheme: str) -> str | None:
    """What this scheme name paints with here, or None when the scheme omits it."""
    return palette.get(mapping.get(scheme, scheme))


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


def _plane(accent: str, ground: str) -> str:
    """The palette's own quiet plane: half the tile's tint of the accent.

    Mixing the ink into the ground answers with a grey, and a grey is not in the
    palette. Every one of the sixteen bundled templates declares its second
    background as #F0F0F0 or #E7E6E6, and not one of the sixteen paints that colour
    on any page it ships -- 0 of 16, over every solid shape fill in them. What they
    paint is the accent: the largest non-ground fill in eight, at 79% of the painted
    area in 5407011 and 57% in 5407013. So a deck derived from the declaration came
    out grey inside a template that is not, and because every card, band and title
    row defaults to this one role, grey was the whole page.

    A stated `bg2` is not read at all now, and that same 0 of 16 is the whole of its
    case: a declaration no page of its own template honours is not evidence about
    that template. A file whose `bg2` really is its card colour loses it here and
    gets a tint of its own accent -- a plane that belongs to the palette either way.

    Half of `accent_soft`'s tint rather than a presence of its own, because two
    planes measured on different scales trade places. Reaching for the contrast a
    tint of the ink would have had put the plane above the tile that carries the
    page's answer in eight of the sixteen and level with it in a ninth: a fixed mix
    of a pale accent lands at 1.06 against the ground where the same mix of a navy
    one lands at 1.39, so no single target can sit under both. Stated as a share of
    the tile's own tint the order holds for every accent there is, and the plane is
    quieter than the tile by construction rather than by measurement.
    """
    return _mixed(accent, ground, 1 - (1 - _SOFT_MIX) / 2)


# The least a plane may stand off its ground and still be a plane. Measured on the
# beige template: its pages paint a #F2E3CB ground, the plane derived from a
# terracotta accent landed at #FAF1E2, 1.10 against it, and every card on two live
# pages was a card only to the file -- the render showed copy floating on beige.
PLANE_FLOOR = 8.0
_TILE_LEAD = 4.0


def _visible(plane: str, accent: str, ground: str) -> str:
    """`plane`, or the nearest tint of the accent that stands off the ground.

    Stood off in CIE76 colour difference and not in luminance contrast: the beige
    template's plane sat at 1.13:1 against its ground, which is the ratio a blue
    plane reads perfectly well at on white, and two live pages' cards were cards
    only to the file -- the two tints differed by a dE of 7 where the blue's is 9.
    So the floor is a dE, and a plane under it is walked from its own mix towards
    the accent in small steps until it clears. One already clear is returned exactly
    as it was, so a stated plane keeps its value.
    """
    if _distance(plane, ground) >= PLANE_FLOOR:
        return plane
    towards = 1 - (1 - _SOFT_MIX) / 2
    while towards > 0.0:
        candidate = _mixed(accent, ground, towards)
        if _distance(candidate, ground) >= PLANE_FLOOR:
            return candidate
        towards -= 0.04
    return accent


def _louder(tile: str, plane: str, accent: str, ground: str) -> str:
    """`tile`, or the mix of the accent that stands further off the ground than `plane`.

    The plane is the quiet zone and the tile carries the answer, and a plane walked
    off its ground by `_visible` can walk past a pale accent's tile. So the tile is
    asked to lead the plane by `_TILE_LEAD` of dE and by luminance contrast both,
    and is deepened towards the accent until it does.
    """
    lead = _distance(plane, ground) + _TILE_LEAD
    if _distance(tile, ground) >= lead and _contrast(tile, ground) > _contrast(plane, ground):
        return tile
    towards = _SOFT_MIX
    while towards > 0.0:
        candidate = _mixed(accent, ground, towards)
        if _distance(candidate, ground) >= lead and _contrast(candidate, ground) > _contrast(plane, ground):
            return candidate
        towards -= 0.04
    return accent


def _stated_muted(stated: str | None, ground: str, surface: str, receded: str) -> str | None:
    """`tx2` when it is the receded ink, or None to keep the mix.

    Two ways again, and what `_receded` came back with states both amounts. Quiet
    enough: a `tx2` within a percent of its own ink recedes from nothing, so the
    stated colour has to sit no further off the ground than the recession does. And
    still legible, on the ground and on the surface both -- the bar `_receded` walks
    its mix back to hold, and the one the `#778495` all ten bundled templates state
    does not clear on their white page, at 3.81:1 where `_LEGIBLE` asks 4.5. Their
    own pages agree with the mix and not with what they declare: sampled over the
    twelve `5407617` ships, its secondary type is `#595959` at 7.01:1 and `#778495`
    is not on any of them.
    """
    if stated is None or _rgb(stated) is None:
        return None
    if _contrast(stated, ground) > _contrast(receded, ground):
        return None
    if min(_contrast(stated, ground), _contrast(stated, surface)) < _LEGIBLE:
        return None
    return stated.upper()


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


def _distance(one: str, two: str) -> float:
    """CIE76 colour difference, which tells two tints apart where a luminance ratio cannot."""
    first, second = _lab(one), _lab(two)
    return sum((a - b) ** 2 for a, b in zip(first, second, strict=True)) ** 0.5


def _lab(colour: str) -> tuple[float, float, float]:
    parts = _rgb(colour)
    if parts is None:
        return (0.0, 0.0, 0.0)
    linear = []
    for value in parts:
        share = value / 255
        linear.append(share / 12.92 if share <= 0.04045 else ((share + 0.055) / 1.055) ** 2.4)
    red, green, blue = linear
    x = (0.4124 * red + 0.3576 * green + 0.1805 * blue) / 0.95047
    y = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    z = (0.0193 * red + 0.1192 * green + 0.9505 * blue) / 1.08883
    fx, fy, fz = (t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in (x, y, z))
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


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


@dataclass(frozen=True)
class Washed:
    """One pair of colours a borrowed label would arrive in and land on, and its ratio."""

    ink: str
    ground: str
    fill: str
    tint: tuple[float, float]
    alpha: float
    ratio: float
    seen: int = 0
    order: int = 0

    def said(self) -> str:
        """The card named the way an author would go and change it."""
        shade = "" if self.tint == (1.0, 0.0) else f" tinted {self.tint[0] * 100:.0f}/{self.tint[1] * 100:.0f}"
        washed = "" if self.alpha >= 1.0 else f" at {self.alpha * 100:.0f}% alpha"
        named = self.fill if self.fill.startswith("#") else f"`{self.fill}`{shade}{washed}"
        return f"a {self.ink} label on {named}, which renders {self.ground} here, at {self.ratio:.1f}:1"


def unreadable_grounds(inventory: TemplateInventory) -> tuple[Washed, ...]:
    """The colours a borrowed card label would arrive in and land on that cannot be read, worst first.

    Empty when every colour these reference pages fill a card with carries every colour
    they label one in, once both are resolved in this deck's palette.

    This used to ask one question -- is white readable on `accent1` -- and it is the
    wrong one three times over. Measured over 486 clones of the 54 reference pages into
    the ten bundled templates, rendered and read by `measure.contrast`, 89 pages came
    back with copy under the ratio, spread across every one of the ten hosts. `accent1`
    was the ground of 24 of them; the other 65 landed on `accent2` (30), the near-white
    second plane (20), `accent3` (3), `accent5` (1) and the page ground (3), and 22 of
    the 89 landed on a *tint* of one of those rather than the swatch. And white was only
    68 of the inks: 6 more were the near-white literal beside it and 15 were a scheme
    colour, which is this deck's own once the page is cloned in. So asking about
    `accent1` against pure white answers for the one bundled template whose `accent1` is
    pale and calls the other nine safe, the dark one 17 of the failures are in included.

    Both halves come from the measurement's own facts. The ink is what the run states,
    read off the file where it is stated exactly, and the ground is a colour the deck
    declares -- not the fill under any particular text box, because that fill is a stack
    of a tint over a panel over the master and resolving it by hand reproduces the
    renderer, which is what `measure.contrast` reads a render for. What is asked is
    therefore every (label, card) pair these pages actually state, resolved in this
    deck's palette: `defaults` holds the three measured sets, this holds the arithmetic.
    """
    from raven_ppt.services.assets.color import contrast_ratio
    from raven_ppt.services.template.defaults import BORROWED_INK_READS, BORROWED_LABEL_PAIRS

    palette = dict(inventory.theme_colours)
    mapping = dict(inventory.colour_map)

    def stated(spec: str) -> str | None:
        colour = spec if spec.startswith("#") else _slot(palette, mapping, spec)
        if not colour:
            return None
        colour = colour if str(colour).startswith("#") else f"#{colour}"
        return colour.upper() if _rgb(colour) else None

    behind = stated("bg1") or "#FFFFFF"
    found: dict[tuple[str, str], Washed] = {}
    for order, (label, card, lum_mod, lum_off, alpha, seen) in enumerate(BORROWED_LABEL_PAIRS):
        ink, swatch = stated(label), stated(card)
        if ink is None or swatch is None:
            continue
        tinted = _tinted(swatch, lum_mod, lum_off)
        # Both ends of the wash, because the renderer sits between them and neither end
        # is a colour of ours: the tint is what the page declares and the composite is
        # what a reader sees once it is laid on the deck's own ground. Measured, the two
        # disagree in both directions -- a 10% card on a light deck renders near-white
        # and a 90% one is the swatch, and LibreOffice paints a fill's alpha stronger
        # than PowerPoint does, so a single answer here would be ours and not the file's.
        for ground in dict.fromkeys((tinted, _over(tinted, alpha, behind))):
            ratio = contrast_ratio(ink, ground)
            if ratio >= BORROWED_INK_READS:
                continue
            # One entry per pair of colours. Several of the pinned pairs resolve to one
            # pair in a given deck -- a light template paints `lt1`, `bg1` and the white
            # literal the same white -- and three rows saying one card is too pale is
            # the boilerplate a caveat dies of.
            key = (ink, ground)
            standing = found.get(key)
            if standing is None or ratio < standing.ratio:
                found[key] = Washed(ink, ground, card, (lum_mod, lum_off), alpha, round(ratio, 2), seen, order)
    # Commonest card first. Sorted by ratio it led with a 5%-alpha card at 1.0:1 and
    # buried the swatch every reference page paints, which is the one an author meets.
    return tuple(sorted(found.values(), key=lambda one: (one.order, one.ratio)))


def _over(colour: str, alpha: float, behind: str) -> str:
    """`colour` painted at `alpha` on `behind`, which is the colour a faint card renders."""
    if alpha >= 1.0:
        return colour
    one, two = _rgb(colour), _rgb(behind)
    if one is None or two is None:
        return colour
    return "#{:02X}{:02X}{:02X}".format(
        *(round(first * alpha + second * (1 - alpha)) for first, second in zip(one, two, strict=True))
    )


def _tinted(colour: str, lum_mod: float, lum_off: float) -> str:
    """`colour` with OOXML's `lumMod`/`lumOff` applied, which is how a card gets its tint."""
    if (lum_mod, lum_off) == (1.0, 0.0):
        return colour
    parts = _rgb(colour)
    if parts is None:
        return colour
    hue, light, saturation = colorsys.rgb_to_hls(*(value / 255 for value in parts))
    light = min(1.0, max(0.0, light * lum_mod + lum_off))
    return "#{:02X}{:02X}{:02X}".format(*(round(value * 255) for value in colorsys.hls_to_rgb(hue, light, saturation)))


def labels_accent_in_ink(source: Path, accent: str = "accent1") -> int | None:
    """A page of this template that sets dark type on an `accent` fill, by page number.

    The evidence half of `unreadable_grounds`. Telling an author their deck cannot carry
    white labels leaves them to invent the alternative; naming the page where the
    template's own designer solved it makes the fix something to copy. None when the
    template never does it, and the caller then says less rather than claiming a page
    that does not exist.
    """
    from pptx import Presentation

    from raven_ppt.services.assets.color import contrast_ratio

    prs = Presentation(str(source))
    palette = _palette_of(prs)
    filled = palette.get(accent)
    if not filled:
        return None
    ground = f"#{filled.lstrip('#')}"
    for number, slide in enumerate(prs.slides, start=1):
        for shape in _walk(slide.shapes):
            if _fill_scheme(shape) != accent:
                continue
            for ink in _run_inks(shape, palette):
                if contrast_ratio(ink, ground) >= 3.0 and contrast_ratio(ink, "#FFFFFF") >= 3.0:
                    return number
    return None


def _palette_of(presentation) -> dict[str, str]:
    from raven_ppt.services.template.decompile import _colour_map, _palette

    colours = {name: f"#{value.lstrip('#')}" for name, value in _palette(presentation).items()}
    for name, slot in _colour_map(presentation).items():
        if slot in colours:
            colours[name] = colours[slot]
    return colours


def _walk(shapes):
    for shape in shapes:
        yield shape
        if getattr(shape, "shape_type", None) is not None and hasattr(shape, "shapes"):
            yield from _walk(shape.shapes)


_DML = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_PML = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _fill_scheme(shape) -> str | None:
    """The scheme name this shape is filled with, or None when it states a literal or nothing."""
    properties = shape._element.find(f"{_PML}spPr")
    if properties is None:
        return None
    for tag in ("solidFill", "gradFill"):
        node = properties.find(f"{_DML}{tag}")
        if node is None:
            continue
        named = node.find(f".//{_DML}schemeClr")
        return named.get("val") if named is not None else None
    return None


def _run_inks(shape, palette: dict[str, str]):
    """Every colour the shape's runs state, resolved through `palette`."""
    body = shape._element.find(f"{_PML}txBody")
    if body is None:
        return
    for run in body.iter(f"{_DML}r"):
        fill = run.find(f"{_DML}rPr/{_DML}solidFill")
        if fill is None:
            continue
        literal = fill.find(f"{_DML}srgbClr")
        if literal is not None:
            yield f"#{literal.get('val')}"
            continue
        named = fill.find(f"{_DML}schemeClr")
        if named is not None and named.get("val") in palette:
            yield palette[named.get("val")]


# How many grounds a reply names. Measured over the ten bundled templates, the question
# finds one to five per deck and the worst two carry the failures: naming all five spends
# the sentence on the pairs no reference page puts together.
_GROUNDS_SAID = 3


def _card_fills(shape) -> tuple[tuple[str, float, float, float], ...]:
    """Every (colour, lumMod, lumOff, alpha) this shape's fill paints, as the file states it.

    The reader `BORROWED_LABEL_PAIRS` was pinned with, kept here so the pinning is
    re-runnable against the shipped code rather than against a script nobody has. A
    scheme name comes back as itself, unresolved: which colour it is belongs to the deck
    the page lands in, and resolving it here would answer for the wrong one.

    `fillRef` is the last rung and not an afterthought. Fifteen of these pages' shapes
    state no fill of their own and take one from their shape style, and reading only
    `spPr` called them unfilled -- which put a label on the page ground and reported a
    card that renders in the deck's accent as white on white.
    """
    properties = shape._element.find(f"{_PML}spPr")
    node = None
    if properties is not None:
        if properties.find(f"{_DML}noFill") is not None:
            return ()
        for tag in ("solidFill", "gradFill"):
            node = properties.find(f"{_DML}{tag}")
            if node is not None:
                break
    if node is None:
        style = shape._element.find(f"{_PML}style")
        reference = style.find(f"{_DML}fillRef") if style is not None else None
        if reference is None or reference.get("idx") in (None, "0"):
            return ()
        node = reference
    found = []
    for stated in list(node.iter(f"{_DML}schemeClr")) + list(node.iter(f"{_DML}srgbClr")):
        named = stated.get("val") if stated.tag.endswith("schemeClr") else f"#{str(stated.get('val', '')).upper()}"
        found.append((named, *_modifiers(stated)))
    return tuple(found)


def _modifiers(stated) -> tuple[float, float, float]:
    """This colour's `lumMod`, `lumOff` and `alpha`, as fractions."""
    modified = stated.find(f"{_DML}lumMod")
    offset = stated.find(f"{_DML}lumOff")
    alpha = stated.find(f"{_DML}alpha")
    return (
        int(modified.get("val", "100000")) / 100000 if modified is not None else 1.0,
        int(offset.get("val", "0")) / 100000 if offset is not None else 0.0,
        int(alpha.get("val", "100000")) / 100000 if alpha is not None else 1.0,
    )


def _label_inks(shape) -> tuple[str, ...]:
    """Every colour this shape's copy runs state, as the file states it, unresolved.

    A run that states nothing falls back to the shape style's `fontRef`, which is where
    a template's card headings routinely put their colour. One-character runs are left
    out: a numeral or a bullet set in the accent reads at 1.8:1 by design, and measuring
    those as copy is what called four of one template's own pages unreadable.
    """
    body = shape._element.find(f"{_PML}txBody")
    if body is None:
        return ()
    if len("".join(node.text or "" for node in body.iter(f"{_DML}t")).strip()) < 2:
        return ()
    style = shape._element.find(f"{_PML}style")
    inherited = style.find(f"{_DML}fontRef") if style is not None else None
    found = []
    for run in body.iter(f"{_DML}r"):
        fill = run.find(f"{_DML}rPr/{_DML}solidFill")
        if fill is None:
            fill = inherited
        if fill is None:
            continue
        named = fill.find(f"{_DML}schemeClr")
        if named is not None:
            found.append(named.get("val"))
            continue
        literal = fill.find(f"{_DML}srgbClr")
        if literal is not None:
            found.append(f"#{str(literal.get('val', '')).upper()}")
    return tuple(dict.fromkeys(found))


def borrow_ink_note(inventory: TemplateInventory, source: Path) -> str:
    """The one sentence a deck whose own colours cannot carry a borrowed label needs, or "".

    Here rather than in the tool that says it because it is a statement about a
    template, and two tools say it -- the bind reply that first offers the reference
    pages and the outline reply that names one per page. Two spellings of it would
    come to disagree about the number.

    It names the grounds and not the pages. A borrowed page's label is stated on its
    run, nothing recolours ink to suit the ground it lands on, and which of this deck's
    colours the label lands on is decided by the page's own arrangement -- so the fact
    an author can act on before cloning anything is which of their colours will not
    carry it. Named per offer it would read as boilerplate; said once for the deck it is
    a property of the deck, which is what it is.
    """
    from raven_ppt.services.template.defaults import BORROWED_INK_READS

    washed = unreadable_grounds(inventory)
    if not washed:
        return ""
    said = "; ".join(one.said() for one in washed[:_GROUNDS_SAID])
    rest = len(washed) - _GROUNDS_SAID
    # The example is worth having and not worth a reply for: a template python-pptx
    # cannot walk still needs to be told which of its colours will not carry a label.
    try:
        shown = labels_accent_in_ink(Path(source), washed[0].fill)
    except Exception:  # noqa: BLE001 -- see above
        shown = None
    where = f", the way this template's own page {shown} does" if shown else ""
    return (
        "A borrowed page states its card labels on the run and nothing recolours ink to suit the ground it "
        f"lands on, so in this deck {len(washed)} of the (label, card) pairs those pages state cannot be read: "
        f"{said}{f'; and {rest} more' if rest > 0 else ''} -- under the {BORROWED_INK_READS:g}:1 at which the "
        f"build refuses a page. Reset those labels in this deck's ink{where}, or fill the card with one of its "
        "darker colours."
    )
