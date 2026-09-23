"""Fetch every channel's app mark and write it at the size the channels page draws it.

The channels section of the settings dialog shows each entrance's own app icon
(``ui-web/src/components/ChannelMark.tsx``). Nine of them are the icon the vendor
publishes on its App Store listing, which is the one place the five Chinese apps
publish a colour icon at all: the open icon sets carry them only as single-colour
glyphs, and their own sites serve favicons of 16 to 48 pixels. The listing's
512px artwork is resampled here, so a refreshed icon is this script rerun rather
than a hand-edited binary, and ``LICENSES/README.md`` can name where each file
came from. Matrix and Mochat publish a vector mark on their own sites, and those
two are copied byte for byte.

    uv run python scripts/refresh_channel_marks.py
"""

from __future__ import annotations

import io
import json
import re
import sys
import urllib.request
from pathlib import Path

#: Twice the 38px tile the page draws, with headroom for a 3x display, and the
#: same edge as the one other raster mark (scripts/refresh_dmxapi_mark.py).
SIDE = 96

ROOT = Path(__file__).resolve().parents[1]
DST = ROOT / "ui-web" / "src" / "assets" / "channels"

#: Channel id -> (App Store track id, storefront). The storefront is where the
#: app is listed: the Chinese store does not carry the four blocked in China.
LISTINGS: dict[str, tuple[int, str]] = {
    "dingtalk": (930368978, "cn"),
    "discord": (985746746, "us"),
    "feishu": (1401729613, "cn"),
    "qq": (444934666, "cn"),
    "slack": (618783545, "us"),
    "telegram": (686449807, "us"),
    "wecom": (1087897068, "cn"),
    "weixin": (414478124, "cn"),
    "whatsapp": (310633997, "us"),
}

#: Channel id -> the vendor's own file, copied verbatim.
VERBATIM: dict[str, str] = {
    "matrix": "https://matrix.org/assets/favicon.svg",
    "mochat": "https://mochat-landingpage.vercel.app/logo.svg",
}

LOOKUP = "https://itunes.apple.com/lookup?id={id}&country={country}"


def _get(url: str) -> bytes:
    # matrix.org answers urllib's default agent with a 403.
    request = urllib.request.Request(url, headers={"User-Agent": "raven-refresh-channel-marks"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def resample(data: bytes, side: int) -> bytes:
    """The artwork as an opaque ``side`` x ``side`` PNG.

    A store icon has no transparency to keep, and the tile rounds its own
    corners, so the alpha channel is dropped rather than carried as bytes.
    """
    from PIL import Image

    image = Image.open(io.BytesIO(data)).convert("RGB")
    buffer = io.BytesIO()
    image.resize((side, side), Image.LANCZOS).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _listing(track: int, country: str) -> dict:
    results = json.loads(_get(LOOKUP.format(id=track, country=country)))["results"]
    if not results:
        raise SystemExit(f"App Store {country} lists no app {track}; has it moved storefront?")
    return results[0]


def main() -> int:
    DST.mkdir(parents=True, exist_ok=True)
    for cid, (track, country) in sorted(LISTINGS.items()):
        app = _listing(track, country)
        # The PNG rendition of the same artwork, so the resample starts lossless.
        artwork = re.sub(r"\.jpg$", ".png", app["artworkUrl512"])
        out = resample(_get(artwork), SIDE)
        (DST / f"{cid}.png").write_bytes(out)
        print(f"{cid}.png  {len(out):>6} B  {app['trackName']} / {app['sellerName']}")
    for cid, url in sorted(VERBATIM.items()):
        body = _get(url)
        (DST / f"{cid}.svg").write_bytes(body)
        print(f"{cid}.svg  {len(body):>6} B  {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
