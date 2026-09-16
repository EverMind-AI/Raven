"""Every provider mark ships, and every tone declared on one is true of its file.

The settings page draws a brand mark per provider and per model family. The
tables live in ``ui-web/src/shell/provider-mark.tsx`` and the files live beside
them, so nothing in either language notices when the two disagree: a renamed
file leaves a row addressing a 404, and a mark no table names ships to every
user forever inside the wheel.

This is also the only gate over those tables that runs on a merge request.
GitLab's ``tests`` job runs the Python unit suite and nothing else -- the vitest
guards beside this one need an image that job does not have (see
``tests/test_ui_css_token_gate.py``), which is why the same properties are
asserted here rather than only there.

``tone`` says how a file answers the theme, and the stylesheet filters each one
differently, so a wrong tone renders as a black mark on a black ground or as a
recoloured brand:

- absent: the mark carries its own colours. Filtered not at all, which is where
  almost every vendor logo belongs.
- ``mono``: every shape is drawn in black or in ``currentColor``, which an
  ``<img>`` resolves against the SVG's own document and so renders black.
  Inverted by the dark theme.
- ``mono-white``: the same, drawn white. AMD publishes the white rendering of
  its wordmark, so the light theme is the one that lifts it.
- ``hybrid``: some shapes adapt and some carry a colour. Inverted with the hue
  rotated back, or the brand colour inverts along with the adaptive part.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TABLE = _ROOT / "ui-web" / "src" / "shell" / "provider-mark.tsx"
_ASSETS = _ROOT / "ui-web" / "src" / "assets" / "providers"

_PAINT = re.compile(r'(?:fill|stroke|stop-color)="([^"]+)"')
_CLASS_FILL = re.compile(r"\.[\w-]+\s*\{\s*fill:\s*([^;}]+)")
_ACHROMATIC_DARK = {"black", "#000", "#000000", "currentColor"}
_WHITE = {"white", "#fff", "#ffffff"}


def _table(name: str, open_ch: str, close_ch: str) -> str:
    """One table's body, read out of the source.

    Read rather than executed: a Python suite cannot import a tsx module, and
    restating the tables here would be the duplication this file exists to
    catch.
    """
    body = _TABLE.read_text(encoding="utf-8")
    start = body.index(f"const {name}")
    opened = body.index(open_ch, start)
    return body[opened : body.index(f"\n{close_ch}", opened)]


def _icon_values() -> set[str]:
    """Every file the two icon maps and the family patterns address."""
    named = set()
    for table in ("ICONS", "VENDOR_ICONS"):
        named |= {m.group(1) for m in re.finditer(r":\s*'([a-z0-9-]+)'", _table(table, "{", "}"))}
    named |= {m.group(1) for m in re.finditer(r",\s*'([a-z0-9-]+)'\]", _table("VENDOR_BY_NAME", "[", "]"))}
    assert named, "the icon tables did not parse -- has their shape changed?"
    return named


def _declared_tones() -> dict[str, str]:
    found = dict(re.findall(r"\s([a-z0-9_-]+):\s*'(mono|hybrid|mono-white)'", _table("TONES", "{", "}")))
    assert found, "the tone table did not parse -- has its shape changed?"
    return found


def _dark_paired() -> set[str]:
    """Marks that ship the vendor's own dark drawing beside the light one."""
    found = {m.group(1) for m in re.finditer(r"'([a-z0-9-]+)'", _table("DARK_PAIRED", "[", "]"))}
    assert found, "the pair set did not parse -- has its shape changed?"
    return found


def _paints(svg: str) -> list[str]:
    """The ink, ignoring what says nothing about tone.

    ``none`` paints nothing and ``url(#..)`` defers to a gradient the file
    carries itself. A fill set through a stylesheet class counts: AMD's wordmark
    colours itself that way and carries no fill attribute at all.
    """
    values = [v for v in _PAINT.findall(svg) if v != "none" and not v.startswith("url(")]
    values += [v.strip() for v in _CLASS_FILL.findall(svg)]
    return values


def _rgb(value: str) -> tuple[float, float, float] | None:
    if not value.startswith("#"):
        return None
    raw = value.lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    if len(raw) < 6:
        return None
    return tuple(int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _is_dark_ink(value: str) -> bool:
    """Ink a theme has to lift, as against a colour it must not touch.

    Both halves matter. Dark alone is not enough: Xirang's mark is a dark red,
    and inverting that gives cyan rather than a legible red. So near-black AND
    near-grey, which is what an invert turns into near-white.
    """
    if value in _ACHROMATIC_DARK:
        return True
    channels = _rgb(value)
    if channels is None:
        return False
    luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    return luminance < 0.22 and (max(channels) - min(channels)) < 0.10


def _tone_of(icon: str) -> str | None:
    svg = (_ASSETS / f"{icon}.svg").read_text(encoding="utf-8", errors="ignore")
    values = _paints(svg)
    if not values:
        return None
    if all(v in _WHITE for v in values):
        return "mono-white"
    ink = [v for v in values if _is_dark_ink(v)]
    if len(ink) == len(values):
        return "mono"
    return "hybrid" if ink else None


def test_every_mark_names_a_file_that_ships() -> None:
    missing = sorted(icon for icon in _icon_values() if not (_ASSETS / f"{icon}.svg").is_file())
    assert not missing, f"addressed but absent from the bundle: {missing}"


def test_no_asset_is_orphaned() -> None:
    """An unreferenced file is dead weight the wheel still carries.

    ``ui-web/build.py`` copies the whole assets tree into the bundle, so a mark
    left behind by a rename ships to every user forever. A ``<name>-dark.svg``
    is named by the pair set rather than by an icon map, so it is folded in
    under the one spelling the component builds.
    """
    named = _icon_values() | {f"{icon}-dark" for icon in _dark_paired()}
    orphans = sorted(path.stem for path in _ASSETS.glob("*.svg") if path.stem not in named)
    assert not orphans, f"in the bundle but named by nothing: {orphans}"


def test_the_tone_agrees_with_the_file() -> None:
    """Declared against derived, both directions.

    A tone is a fact about the shapes, not a preference, and getting it wrong
    is invisible in review: the mark renders, just illegibly or in the wrong
    colour, and only in one theme.
    """
    declared = _declared_tones()
    wrong = {icon: (tone, _tone_of(icon)) for icon, tone in declared.items() if _tone_of(icon) != tone}
    assert not wrong, f"declared tone does not match the file: {wrong}"

    paired = _dark_paired()
    undeclared = sorted(
        icon for icon in _icon_values() if _tone_of(icon) is not None and icon not in declared and icon not in paired
    )
    assert not undeclared, f"the file needs a tone and the table does not give it one: {undeclared}"

    # A tone and a dark drawing are two answers to one question, and the
    # stylesheet applies both: the pair hides the light file, then the filter
    # recolours whichever survives.
    both = sorted(set(declared) & paired)
    assert not both, f"declared a tone and a dark pair: {both}"
