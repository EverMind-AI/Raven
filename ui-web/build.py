"""Assemble ui-web/dist/index.html from the page skeleton, the stylesheet and the bundle.

Sources under ``src/``:

- ``page.html``   -- the document skeleton (markup only), with two markers:
                     ``/*__STYLE__*/`` inside its ``<style>`` tag and
                     ``/*__MODERN__*/`` inside its ``<script>`` tag.
- ``styles/page.css`` -- the page's own stylesheet, injected at the style marker.
- ``.modern/domains.css`` -- the domains' own stylesheets, which Vite collects
                     from every ``features/<domain>/styles.css`` an App imports;
                     injected into the same ``<style>`` block, after page.css.
                     Vite closes that asset with a ``/*$vite$:N*/`` marker
                     comment of its own, which rides along as written.
- ``.modern/modern.iife.js`` -- the bundle Vite builds from ``src/main.tsx``,
                     injected at the script marker.

Three substitutions and a copy, and no assembly of its own: every line of
JavaScript the page runs comes from that one bundle, which Vite builds from
``src/main.tsx``. The message catalogue is in it too (``src/i18n/t.ts``
imports ``i18n/messages.json``).

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


#: The two load modes, each with its own golden. Stub mode is the page on its
#: own fixtures (``?stub=1``); live mode is the page a reader gets when the
#: gateway is not there, which settles into a different tree and would
#: otherwise be unguarded -- a live-only boot break passes the stub snapshot
#: untouched.
_BOOT_SNAPSHOTS = (
    ("http://127.0.0.1:18792/?stub=1", "boot-stub.txt"),
    ("http://127.0.0.1:18792/", "boot-live-noserver.txt"),
)


def _domain_styles() -> str:
    """The domains' collected stylesheet, held to the domains that declare one.

    A page with exactly one ``<style>`` block is the served contract
    (``scripts/check-page.mjs``), so this is inlined rather than linked. Checked
    both ways: a tree with a ``features/<domain>/styles.css`` and no built asset
    would ship those domains unstyled with no error, and an asset with no source
    is a stale ``.modern/``.
    """
    declared = sorted((ROOT / "src" / "features").glob("*/styles.css"))
    built = ROOT / ".modern" / "domains.css"
    if declared and not built.is_file():
        names = ", ".join(p.parent.name for p in declared)
        raise SystemExit(
            f"ui-web/.modern/domains.css not found, but {names} declare a styles.css -- "
            "run `npm run --prefix ui-web build` before ui-web/build.py"
        )
    if built.is_file() and not declared:
        raise SystemExit(
            "ui-web/.modern/domains.css exists but no features/<domain>/styles.css does; "
            "delete ui-web/.modern and rebuild"
        )
    if not declared:
        return ""
    return "\n" + built.read_text(encoding="utf-8").rstrip("\n")


def main() -> None:
    page = (ROOT / "src" / "page.html").read_text(encoding="utf-8")
    style = (ROOT / "src" / "styles" / "page.css").read_text(encoding="utf-8")
    style = style[:-1] if style.endswith("\n") else style
    style += _domain_styles()
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
