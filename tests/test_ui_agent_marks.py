"""Every third-party sub-agent preset has a brand mark, and every tone on it is true.

The roster draws a brand mark per agent, chosen by preset key. The preset list
lives in Python and the mark table lives in
``ui-web/src/shell/agent-mark.tsx``, so nothing in either language can notice
when they disagree: adding a preset leaves its row wearing the generic glyph,
and renaming a file leaves a row addressing a 404. Both are invisible in
review and neither breaks a build.

This is also the only gate over that table that runs on a merge request.
GitLab's ``tests`` job runs the Python unit suite and nothing else -- the
stylesheet and DOM guards beside this one are vitest and node, which that
image does not have (see ``tests/test_ui_css_token_gate.py``).

The ``tone`` is derived from the file rather than trusted. It says how the SVG
answers the theme, and the dark theme filters each tone differently, so a wrong
one renders as a black mark on a black surface or as a recoloured brand:

- absent: every shape carries its own fill. Theme-independent, filtered not at
  all.
- ``mono``: every shape draws in ``currentColor``, which an ``<img>`` resolves
  against the SVG's own document and so renders black. Inverted wholesale.
- ``hybrid``: some shapes do and some do not. Inverted with the hue rotated
  back, or the brand colour inverts along with the adaptive tone.
"""

from __future__ import annotations

import re
from pathlib import Path

from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS

_ROOT = Path(__file__).resolve().parents[1]
_TABLE = _ROOT / "ui-web" / "src" / "shell" / "agent-mark.tsx"
_ASSETS = _ROOT / "ui-web" / "src" / "assets" / "agents"

_ENTRY = re.compile(
    r"^\s{2}(\w+):\s*\{\s*file:\s*'([^']+)'(?:,\s*tone:\s*'(mono|hybrid)')?\s*\},?$",
    re.MULTILINE,
)
_SHAPE = re.compile(r"<(?:path|rect|circle|ellipse|polygon)\b[^>]*>")


def _marks() -> dict[str, tuple[str, str | None]]:
    """The mark table as the stylesheet and the roster see it.

    Read out of the source rather than executed: a Python suite cannot import
    a tsx module, and the alternative -- restating the table here -- would be
    the very duplication this file exists to catch.
    """
    body = _TABLE.read_text(encoding="utf-8")
    start = body.index("const MARKS")
    end = body.index("\n}", start)
    found = {key: (file, tone or None) for key, file, tone in _ENTRY.findall(body[start:end])}
    assert found, "the mark table did not parse -- has its shape changed?"
    return found


def _tone_of(file: str) -> str | None:
    """What the SVG itself says its tone is.

    A shape draws in ``currentColor`` either by naming it or by carrying no
    fill of its own while the root does -- qoder's second path is the latter,
    which is why a plain substring test over the file is not enough to tell a
    hybrid from a mono.
    """
    svg = (_ASSETS / f"{file}.svg").read_text(encoding="utf-8")
    root = svg[: svg.index(">") + 1]
    root_adapts = "currentColor" in root
    shapes = _SHAPE.findall(svg)
    adaptive = sum(1 for el in shapes if "currentColor" in el or ("fill=" not in el and root_adapts))
    if not adaptive:
        return None
    return "mono" if adaptive == len(shapes) else "hybrid"


def test_every_preset_has_a_mark() -> None:
    assert set(_marks()) == set(THIRD_PARTY_SUBAGENT_PRESETS)


def test_every_mark_names_a_file_that_ships() -> None:
    missing = {key: file for key, (file, _) in _marks().items() if not (_ASSETS / f"{file}.svg").is_file()}
    assert not missing


def test_no_asset_is_orphaned() -> None:
    """An unreferenced file is dead weight the wheel still carries.

    ``ui-web/build.py`` copies the whole assets tree into the bundle, so a mark
    left behind by a removed preset ships to every user forever.
    """
    named = {f"{file}.svg" for file, _ in _marks().values()}
    assert {path.name for path in _ASSETS.glob("*.svg")} == named


def test_the_tone_agrees_with_the_file() -> None:
    wrong = {
        key: (declared, _tone_of(file)) for key, (file, declared) in _marks().items() if declared != _tone_of(file)
    }
    assert not wrong


def test_the_hybrid_tone_is_not_spent_on_a_file_that_does_not_need_it() -> None:
    """A hybrid is the only tone whose filter changes a colour on purpose.

    Both directions matter and neither is visible in the rendered page: a
    hybrid spelled ``mono`` recolours the brand, and a mono spelled ``hybrid``
    rotates the hue of a shape that has none, so the assertion is that the set
    is exactly the files that mix the two kinds of shape.
    """
    declared = {key for key, (_, tone) in _marks().items() if tone == "hybrid"}
    derived = {key for key, (file, _) in _marks().items() if _tone_of(file) == "hybrid"}
    assert declared == derived
