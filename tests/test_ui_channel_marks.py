"""Every channel adapter has an app mark, and every mark file is one the web UI can draw.

The channels section of the settings dialog draws each entrance's app icon,
chosen by channel id. The adapters live in Python (one package each under
``raven/channels/adapters/``) and the mark table lives in
``ui-web/src/components/ChannelMark.tsx``, so nothing in either language notices
when they disagree: a new adapter's row wears the plain mark, and a renamed
file leaves a row asking for a 404 -- which the component also answers with the
plain mark, so neither is visible in review.

The files themselves come from ``scripts/refresh_channel_marks.py``, which
records where each one is published. The rasters are held to the size it
writes: a store's 512px artwork dropped in by hand renders the same at 38px and
costs the bundle ten times the bytes, and the size is what says the script made
the file rather than a download.
"""

from __future__ import annotations

import io
import re
import struct
from pathlib import Path

from raven.channels.registry import discover_channel_names
from scripts import refresh_channel_marks

_ROOT = Path(__file__).resolve().parents[1]
_TABLE = _ROOT / "ui-web" / "src" / "components" / "ChannelMark.tsx"
_ASSETS = _ROOT / "ui-web" / "src" / "assets" / "channels"

_ENTRY = re.compile(
    r"^\s{2}(\w+):\s*\{\s*file:\s*'([^']+)'(?:,\s*inset:\s*true)?\s*\},?$",
    re.MULTILINE,
)
_GENERIC = re.compile(r"^const GENERIC = new Set\(\[([^\]]*)\]\)$", re.MULTILINE)


def _marks() -> dict[str, str]:
    """The mark table as ChannelMark reads it, channel id to file name.

    Read out of the source rather than executed: a Python suite cannot import a
    tsx module, and restating the table here would be the duplication this file
    exists to catch.
    """
    body = _TABLE.read_text(encoding="utf-8")
    start = body.index("const MARKS")
    end = body.index("\n}", start)
    found = dict(_ENTRY.findall(body[start:end]))
    assert found, "the mark table did not parse -- has its shape changed?"
    return found


def _generic() -> set[str]:
    """The entrances that are a protocol rather than a brand, drawn with no one's mark."""
    found = _GENERIC.findall(_TABLE.read_text(encoding="utf-8"))
    assert len(found) == 1, "the generic set did not parse -- has its shape changed?"
    return set(re.findall(r"'([a-z0-9_]+)'", found[0]))


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", head[16:24])


def test_every_adapter_has_a_mark() -> None:
    assert set(_marks()) | _generic() == set(discover_channel_names())


def test_no_entrance_is_both_marked_and_generic() -> None:
    assert not set(_marks()) & _generic()


def test_every_mark_names_a_file_that_ships() -> None:
    missing = {cid: file for cid, file in _marks().items() if not (_ASSETS / file).is_file()}
    assert not missing


def test_no_asset_is_orphaned() -> None:
    """An unreferenced file is dead weight the wheel still carries.

    ``ui-web/build.py`` copies the whole assets tree into the bundle, so a mark
    left behind by a removed adapter ships to every user forever.
    """
    assert {path.name for path in _ASSETS.iterdir()} == set(_marks().values())


def test_every_mark_is_one_the_refresh_script_writes() -> None:
    """The script is the provenance: a file it does not know came from nowhere recorded."""
    written = {cid: f"{cid}.png" for cid in refresh_channel_marks.LISTINGS}
    written |= {cid: f"{cid}.svg" for cid in refresh_channel_marks.VERBATIM}
    assert _marks() == written


def test_the_rasters_are_the_size_the_script_writes() -> None:
    wrong = {
        file: _png_size(_ASSETS / file)
        for file in _marks().values()
        if file.endswith(".png") and _png_size(_ASSETS / file) != (refresh_channel_marks.SIDE,) * 2
    }
    assert not wrong


def test_resampling_keeps_the_artwork_opaque_and_square() -> None:
    """A store icon is an opaque square, and the mark is drawn as one.

    No alpha channel: the tile clips its corners itself, so transparency in the
    file would only be bytes -- and a PNG that carried it would render the
    tile's own ground through it.
    """
    from PIL import Image

    source = io.BytesIO()
    Image.new("RGBA", (512, 512), (7, 193, 96, 255)).save(source, format="PNG")
    out = Image.open(io.BytesIO(refresh_channel_marks.resample(source.getvalue(), 96)))
    assert (out.format, out.size, out.mode) == ("PNG", (96, 96), "RGB")
