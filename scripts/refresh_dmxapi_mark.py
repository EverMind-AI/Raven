"""Re-encode DMXAPI's brand mark at the size the provider rail draws it.

Every other mark under ``ui-web/src/assets/providers`` is a vector copied
verbatim, and ``LICENSES/README.md`` stakes the attribution on that: each is
byte-identical to what ``@cherrystudio/ui`` ships, checkable with ``cmp``.
DMXAPI is the one upstream publishes as a raster inside an SVG wrapper -- a
342x342 PNG for a mark the rail draws 20px wide, and 143 KB of the bundle for
one row, a third again the whole icon set.

So this file is the exception, and this script is what makes the exception
reviewable rather than a hand-edited blob. The wrapper is upstream's, byte for
byte; the artwork keeps its design and its colours; only the embedded PNG is
resampled. Rerunning against a newer upstream reproduces the shipped file.

    uv run python scripts/refresh_dmxapi_mark.py [--cherry <path to cherry-studio>]
"""

from __future__ import annotations

import argparse
import base64
import io
import re
import sys
from pathlib import Path

#: Generous for a mark drawn at roughly 11 CSS px -- the artwork fills 65 of a
#: 120 viewBox in a 20px slot -- with headroom for a 3x display and for any
#: surface that later draws it larger.
SIDE = 96

ROOT = Path(__file__).resolve().parents[1]
DST = ROOT / "ui-web" / "src" / "assets" / "providers" / "dmxapi.svg"
UPSTREAM = Path("packages/ui/icons/providers/light/dmxapi.svg")


def _without_payload(text: str) -> str:
    """The wrapper alone, so it can be compared across a re-encode."""
    return re.sub(r'base64,[^"]+"', 'base64,PAYLOAD"', text)


def rebuild(svg: str, side: int) -> bytes:
    from PIL import Image

    payload = re.search(r'base64,([^"]+)"', svg)
    if not payload:
        raise SystemExit("upstream no longer embeds a base64 raster; check whether it ships a vector now")
    image = Image.open(io.BytesIO(base64.b64decode(payload.group(1)))).convert("RGBA")
    buffer = io.BytesIO()
    image.resize((side, side), Image.LANCZOS).save(buffer, format="PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return re.sub(r'base64,[^"]+"', f'base64,{encoded}"', svg, count=1).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cherry", type=Path, default=Path.home() / "github" / "cherry-studio")
    args = parser.parse_args()

    source = args.cherry / UPSTREAM
    if not source.is_file():
        print(f"upstream mark not found at {source}", file=sys.stderr)
        return 1

    svg = source.read_text(encoding="utf-8")
    out = rebuild(svg, SIDE)
    DST.write_bytes(out)

    # The geometry is upstream's; only the payload differs.
    if _without_payload(svg) != _without_payload(out.decode("utf-8")):
        raise SystemExit("the wrapper changed, which this script is not allowed to do")
    print(f"{len(svg):,} bytes upstream -> {len(out):,} bytes shipped, wrapper unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
