"""The assets, projected into two modules an author's build script can import.

On the script route the author writes a python-pptx program, so there is no
schema to hang a theme field or an icon field on. The assets reach it as files
instead: ``ppt_theme.py`` beside ``themes.json``, ``ppt_icons.py`` beside
``icons.json``, dropped into the build directory where the script runs. The
script does ``from ppt_theme import THEMES, rgb`` and ``from ppt_icons import
add_icon``.

This module produces the *content* only. Creating the build directory and
writing the files there belongs to the script backend, which owns that
directory's layout and lifecycle; assets are stateless and touch no filesystem
of their own. ``script_helper_files()`` returns exactly what to write and under
what name, because the filenames are part of the contract -- the script imports
them by name -- and so they are not the backend's to choose.

The generated modules import nothing from raven. That is deliberate: the build
directory has to be readable and runnable as a plain python-pptx project, the
author reads these two files to learn what it may draw with, and a script that
reached back into raven internals would break the moment those moved.
"""

from __future__ import annotations

import dataclasses
import json

from raven.ppt.services.assets import icons
from raven.ppt.services.assets.themes import THEME_GUIDE, THEMES

THEME_MODULE_FILENAME = "ppt_theme.py"
THEME_DATA_FILENAME = "themes.json"
ICON_MODULE_FILENAME = "ppt_icons.py"
ICON_DATA_FILENAME = "icons.json"

# What a script draws with. The on-dark tokens and the engine's decoration
# dimensions stay behind: pages are white, and the decoration is the author's to
# design rather than a token to look up. No size or ladder is exported, because
# there is none to export -- a theme carries no type scale.
_EXPORTED_THEME_FIELDS = (
    "background",
    "surface",
    "foreground",
    "muted",
    "accent",
    "accent_soft",
    "grid",
    "chart_series",
    "font_family",
)

_THEME_MODULE = '''"""The reviewed deck themes, as plain data.

    from ppt_theme import THEMES, rgb
    T = THEMES["warm-paper"]
    INK, ACCENT, FONT = rgb(T["foreground"]), rgb(T["accent"]), T["font_family"]
    HAN = T["cjk_font_family"]          # the CJK companion for this theme

Every colour is a #RRGGBB string; `chart_series` is six of them, in order.
Pick one theme for the whole deck.

`font_family` is the Latin face and `cjk_font_family` is its CJK companion. A
deck in Chinese needs both named on the same run -- `ppt_layout.write` does it for
you -- or the Han characters are set in whatever the viewer falls back to, which
on this renderer has no Han glyphs at all.
"""

import json
from pathlib import Path

from pptx.dml.color import RGBColor

THEMES = json.loads((Path(__file__).parent / "themes.json").read_text(encoding="utf-8"))
THEME_NAMES = sorted(THEMES)


def rgb(value):
    """'#RRGGBB' -> RGBColor, so a theme colour drops straight into python-pptx."""
    if isinstance(value, RGBColor):
        return value
    text = str(value).lstrip("#")
    return RGBColor(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
'''

_ICON_MODULE = '''"""Draw packaged Tabler Outline icons onto a python-pptx slide.

    from ppt_icons import add_icon, ICON_NAMES
    add_icon(slide, "target", Inches(1), Inches(2), Inches(0.4), ACCENT)

Icons are strokes on a 24x24 grid, drawn as unfilled freeform shapes so they
stay vector and editable after export. `colour` takes either a '#RRGGBB' string
or an RGBColor, so a theme colour goes in as it comes out of ppt_theme.
"""

import difflib
import json
from pathlib import Path

from pptx.dml.color import RGBColor
from pptx.util import Emu

_DATA = json.loads((Path(__file__).parent / "icons.json").read_text(encoding="utf-8"))
ICON_NAMES = sorted(_DATA)
_GRID = 24.0
_CURVE_STEPS = 8


def _cubic(p0, p1, p2, p3, steps=_CURVE_STEPS):
    points = []
    for step in range(1, steps + 1):
        t = step / steps
        u = 1 - t
        points.append(
            (
                u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
                u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
            )
        )
    return points


def _polylines(name):
    """Flatten one icon's paths into polylines on the 24x24 grid."""
    lines = []
    for kind, commands in _DATA[name]:
        if kind != "path":
            continue
        current = None
        run = []
        for op, args in commands:
            if op == "M":
                if len(run) > 1:
                    lines.append(run)
                current = (args[0], args[1])
                run = [current]
            elif op == "L":
                current = (args[0], args[1])
                run.append(current)
            elif op == "C" and current is not None:
                p1, p2, p3 = (args[0], args[1]), (args[2], args[3]), (args[4], args[5])
                run.extend(_cubic(current, p1, p2, p3))
                current = p3
            elif op == "Z" and run:
                run.append(run[0])
        if len(run) > 1:
            lines.append(run)
    return lines


def find_icons(term):
    """The names nearest to `term`, best guess first. Empty if nothing is close."""
    key = str(term).strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")
    if not key:
        return []
    tokens = [part for part in key.split("_") if part]
    ranked = []

    def add(candidate):
        if candidate not in ranked:
            ranked.append(candidate)

    for candidate in ICON_NAMES:
        if any(token in candidate.split("_") for token in tokens):
            add(candidate)
    for candidate in ICON_NAMES:
        if key in candidate or candidate in key:
            add(candidate)
    for candidate in difflib.get_close_matches(key, ICON_NAMES, n=8, cutoff=0.62):
        add(candidate)
    for token in tokens:
        for candidate in difflib.get_close_matches(token, ICON_NAMES, n=3, cutoff=0.7):
            add(candidate)
    return ranked[:8]


def _resolve(name):
    """Accept the upstream spelling, and name the near misses when there is none.

    Tabler publishes these as `map-pin`; they are stored here as `map_pin`, so an
    author writing what it knows would otherwise be refused over a separator. And
    a name outside the set entirely -- `switch-horizontal`, `arrows-move` -- is
    worth answering with what is close, because the alternative is a round spent
    guessing. `find_icons` runs the same search without raising.
    """
    key = str(name).strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")
    if key in _DATA:
        return key
    near = find_icons(key)
    if near:
        hint = "; closest: " + ", ".join(near)
    else:
        hint = f"; nothing close -- all {len(ICON_NAMES)} names are in ICON_NAMES"
    raise LookupError(f"unknown icon {name!r}{hint}")


def _line_color(colour):
    """A '#RRGGBB' string or an RGBColor, because ppt_theme hands out strings."""
    if isinstance(colour, RGBColor):
        return colour
    text = str(colour).lstrip("#")
    return RGBColor(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def add_icon(slide, name, left, top, size, colour, width_pt=1.75):
    """Draw `name` in a square of side `size` with its top-left at (left, top)."""
    name = _resolve(name)
    colour = _line_color(colour)
    scale = float(size) / _GRID
    shapes = []
    for polyline in _polylines(name):
        (x0, y0), rest = polyline[0], polyline[1:]
        if not rest:
            continue
        builder = slide.shapes.build_freeform(Emu(int(left + x0 * scale)), Emu(int(top + y0 * scale)))
        builder.add_line_segments(
            [(Emu(int(left + x * scale)), Emu(int(top + y * scale))) for x, y in rest],
            close=False,
        )
        shape = builder.convert_to_shape()
        shape.fill.background()
        shape.line.color.rgb = colour
        shape.line.width = Emu(int(width_pt * 12700))
        shapes.append(shape)
    return shapes
'''


def theme_catalog() -> dict[str, dict]:
    """The reviewed themes reduced to what a script draws with.

    The ten presets are a tested set: each accent clears contrast on its own
    ground, each pairs with a face the renderer does not substitute at a
    different width, and each has a six-colour series for data. A script that
    invents RGB values from scratch throws all of that away and usually lands on
    something worse, so the presets travel into the build directory as data.
    """
    catalog: dict[str, dict] = {}
    for theme_id, theme in THEMES.items():
        present = {field.name for field in dataclasses.fields(theme)}
        entry: dict[str, object] = {}
        for name in _EXPORTED_THEME_FIELDS:
            if name not in present:
                continue
            value = getattr(theme, name)
            entry[name] = list(value) if isinstance(value, tuple) else value
        # A property rather than a field, and the one the author actually needs for
        # a deck in Chinese: `font_family` alone leaves Han to the viewer's fallback.
        entry["cjk_font_family"] = theme.cjk_family
        catalog[theme_id] = entry
    return catalog


def theme_catalog_json() -> str:
    return json.dumps(theme_catalog(), indent=1)


def theme_module_source() -> str:
    """Source of the `ppt_theme` module a build script imports."""
    return _THEME_MODULE


def icon_catalog_json() -> str:
    return icons.catalog_json()


def icon_module_source() -> str:
    """Source of the `ppt_icons` module a build script imports."""
    return _ICON_MODULE


def script_helper_files() -> dict[str, str]:
    """Filename -> text, the complete set to write into a build directory."""
    from raven.ppt.services.assets.layout import LAYOUT_MODULE_FILENAME, layout_module_source

    return {
        THEME_MODULE_FILENAME: theme_module_source(),
        THEME_DATA_FILENAME: theme_catalog_json(),
        ICON_MODULE_FILENAME: icon_module_source(),
        ICON_DATA_FILENAME: icon_catalog_json(),
        LAYOUT_MODULE_FILENAME: layout_module_source(),
    }


def theme_summary() -> str:
    """One line per theme, for a caller that has to describe them in a prompt."""
    return "\n".join(f"- {theme_id}: {THEME_GUIDE[theme_id]}" for theme_id in sorted(theme_catalog()))


def icon_summary() -> str:
    """Every icon name, for a caller that cannot let the model read the directory.

    The design pass gets a render and a code block and no tools, so an icon it
    cannot name is an icon it cannot use -- and the whole set is under two
    kilobytes, cheaper than the round it wastes guessing a name that misses.
    """
    names = icons.icon_names()
    return (
        f"`ppt_icons.add_icon(slide, name, left, top, size, colour)` draws any of these {len(names)} "
        "outline icons, sized and coloured as you ask. `ppt_icons.find_icons(term)` returns the "
        "nearest names if you are unsure.\n\n" + ", ".join(names)
    )
