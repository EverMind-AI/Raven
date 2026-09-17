"""Assemble ui-web/dist/index.html from the page skeleton, the stylesheet and the bundle.

Sources under ``src/``:

- ``page.html``   -- the document skeleton (markup only), with two markers:
                     ``/*__STYLE__*/`` inside its ``<style>`` tag and
                     ``/*__MODERN__*/`` inside its ``<script>`` tag.
- ``styles/page.css`` -- the stylesheet, injected at the style marker.
- ``.modern/modern.iife.js`` -- the bundle Vite builds from ``src/main.tsx``,
                     injected at the script marker.

The two layers under ``src/legacy/`` are ES modules now, reached from
``src/main.tsx`` through ``src/legacy/index.js`` and bundled by Vite with
everything else, so this script no longer assembles them and no longer inlines
the message catalogue either (``src/legacy/demo/010-kernel.js`` imports
``i18n/messages.json`` directly). The manifests below stay: they are the order
``src/legacy/index.js`` installs the parts in, and one Python test outside this
directory calls ``_concat``. No sandbox test reads them any more -- the
harnesses import the parts (``ui-web/scripts/legacy-part.mjs``), and the two
shape gates take the list of parts from ``src/legacy/index.js``.
Run:

    python ui-web/build.py
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STYLE_MARK = "/*__STYLE__*/"
MODERN_MARK = "/*__MODERN__*/"
#: Replaced with a digest of the asset tree, which the page hands to the icon
#: helpers as a query on every asset URL.
#:
#: These files live at one unversioned path each, so a replaced drawing lands at
#: exactly the URL its predecessor is cached under -- and a client that decided
#: the old one was fresh keeps showing it through a rebuild, a restart and a
#: hard reload. A digest in the URL makes a changed file a different URL, which
#: no cache can answer from what it already has.
ASSETV_MARK = "__ASSETV__"


# Install order is semantics: each part's install() does what the part used to
# do while the concatenated script ran, and several of them read what an earlier
# one wrote. src/legacy/index.js calls them in exactly this order -- regenerate
# it (scripts/legacy-index.mjs) after renaming, adding or removing a part.
#
# The live manifest is down to one part. The page's own wiring and boot are
# src/state/{install,boot}.ts now; what is left here is the settings chrome,
# which holds the whole-page redraw the Python test outside this directory reads
# by source text -- so this file name and this entry stay until that test goes.
_DEMO_PARTS = [
    "010-kernel.js",
    "040-state.js",
    "050-rail.js",
    "060-conversation.js",
    "070-transcript.js",
    "090-composer.js",
    "100-workspace.js",
    "120-capabilities.js",
    "130-settings.js",
    "140-schedule.js",
    "145-connections.js",
    "150-chrome.js",
    "152-skills.js",
    "153-plugins.js",
    "154-playbooks.js",
    "155-bridge.js",
    "160-boot.js",
]
_LIVE_PARTS = [
    "120-settings.js",
]


def _concat(subdir: str, manifest: list[str]) -> str:
    """The layer's parts as one text, in manifest order.

    Nothing in the build reads this any more; it is what the Python test
    outside this directory searches for a function it then evaluates. The layer
    names stay "seam" / "demo" / "live" for every caller.
    """
    layer = ROOT / "src" / "legacy" / subdir
    found = {p.name for p in layer.glob("*.js")}
    if found != set(manifest):
        extra = sorted(found - set(manifest))
        missing = sorted(set(manifest) - found)
        raise SystemExit(
            f"src/legacy/{subdir} does not match its manifest in build.py"
            + (f"; not in manifest: {extra}" if extra else "")
            + (f"; missing: {missing}" if missing else "")
        )
    text = "".join((layer / name).read_text(encoding="utf-8") for name in manifest)
    return text[:-1] if text.endswith("\n") else text


def _assets_stamp() -> str:
    """A short digest of everything under ``src/assets``, or "dev" if it is gone.

    Content, not mtime: a rebuild that changes nothing should not invalidate
    every icon in every client, and a checkout that restores an old file should
    go back to that file's old URL.
    """
    src_assets = ROOT / "src" / "assets"
    if not src_assets.is_dir():
        return "dev"
    digest = hashlib.sha1(usedforsecurity=False)
    for path in sorted(src_assets.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(src_assets).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


#: The two load modes, each with its own golden. Stub mode is the demo shell on
#: its fixtures; live mode is the page a reader gets when the gateway is not
#: there, which settles into a different tree and would otherwise be unguarded
#: -- a live-only boot break passes the stub snapshot untouched.
_BOOT_SNAPSHOTS = (
    ("http://127.0.0.1:18792/?stub=1", "boot-stub.txt"),
    ("http://127.0.0.1:18792/", "boot-live-noserver.txt"),
)


def main() -> None:
    page = (ROOT / "src" / "page.html").read_text(encoding="utf-8")
    style = (ROOT / "src" / "styles" / "page.css").read_text(encoding="utf-8")
    style = style[:-1] if style.endswith("\n") else style
    # The island bundle (React features) is built by Vite, not committed:
    # page assembly now has a node step ahead of this python one. Absence is
    # an error rather than a warning because a page without the bundle ships
    # working navigation to a blank schedules page.
    modern_path = ROOT / ".modern" / "modern.iife.js"
    if not modern_path.is_file():
        raise SystemExit(
            "ui-web/.modern/modern.iife.js not found -- run `npm ci --prefix ui-web` "
            "then `npm run --prefix ui-web build` before ui-web/build.py"
        )
    modern = modern_path.read_text(encoding="utf-8")
    modern = modern[:-1] if modern.endswith("\n") else modern
    for mark, text in ((STYLE_MARK, style), (MODERN_MARK, modern)):
        if page.count(mark) != 1:
            raise SystemExit(f"page.html: expected exactly one {mark} marker")
        page = page.replace(mark, text, 1)
    out = page.replace(ASSETV_MARK, _assets_stamp(), 1)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    # newline="" so the bytes are the same on every platform: the default
    # translates "\n" to "\r\n" on Windows, which would hand a checkout there a
    # differently-hashed page than the one CI and the wheel carry.
    (dist / "index.html").write_text(out, encoding="utf-8", newline="")
    print(f"built dist/index.html ({len(out):,} bytes)", flush=True)

    # Static assets stay files rather than data: URIs -- inlining 320 KB of
    # artwork would grow the page by a third again in base64 and re-download it
    # on every load. The server mounts dist/assets at /assets, and the release
    # wheel force-includes the whole dist directory.
    src_assets = ROOT / "src" / "assets"
    if src_assets.is_dir():
        out_assets = dist / "assets"
        shutil.rmtree(out_assets, ignore_errors=True)
        shutil.copytree(src_assets, out_assets)
        shutil.copy2(ROOT / "icon" / "raven.svg", out_assets / "raven.svg")
        total = sum(p.stat().st_size for p in out_assets.rglob("*") if p.is_file())
        print(f"copied dist/assets ({total:,} bytes)", flush=True)
    for url, golden in _BOOT_SNAPSHOTS:
        _check_boot_snapshot(dist / "index.html", url, golden)


def _check_boot_snapshot(index: Path, url: str, golden: str) -> None:
    """Refuse a page whose booted DOM shape moved; see scripts/boot-snapshot.mjs."""
    node = shutil.which("node")
    if node is None:
        raise SystemExit("ui-web/build.py: node is not on PATH; the boot snapshot gate needs it")
    result = subprocess.run(
        [
            node,
            str(ROOT / "scripts" / "boot-snapshot.mjs"),
            str(index),
            "--url",
            url,
            "--golden",
            str(ROOT / "scripts" / "__golden__" / golden),
        ],
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"ui-web/build.py: booted DOM shape differs from scripts/__golden__/{golden}")


if __name__ == "__main__":
    main()
