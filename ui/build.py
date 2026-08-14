"""Assemble ui/dist/index.html = src/base.html + src/live.js + i18n.

The base is the validated design-spec shell (demo v5); live.js swaps its
canned replay for the /rpc WebSocket when served over http. The message
catalogue in ``i18n/messages.json`` (shared with the TUI, which generates a
TypeScript copy from it) is inlined at the ``__I18N__`` marker. Run:

    python ui/build.py
"""

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CATALOG = ROOT.parent / "i18n" / "messages.json"
MARK = "</script>\n</body>"
I18N_MARK = '/*__I18N__*/{ "slash": {}, "ui": {} }'


def main() -> None:
    base = (ROOT / "src" / "base.html").read_text(encoding="utf-8")
    live = (ROOT / "src" / "live.js").read_text(encoding="utf-8")
    if MARK not in base:
        raise SystemExit("base.html: closing script marker not found")
    if I18N_MARK not in base:
        raise SystemExit("base.html: i18n catalogue marker not found")
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    catalog.pop("_readme", None)
    base = base.replace(I18N_MARK, json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), 1)
    out = base.replace(MARK, f"\n{live}\n{MARK}", 1)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(out, encoding="utf-8")
    print(f"built dist/index.html ({len(out):,} bytes)")

    # Binary assets stay files rather than data: URIs -- inlining 320 KB of
    # artwork would grow the page by a third again in base64 and re-download it
    # on every load. The server mounts dist/assets at /assets, and the release
    # wheel force-includes the whole dist directory.
    src_assets = ROOT / "src" / "assets"
    if src_assets.is_dir():
        out_assets = dist / "assets"
        shutil.rmtree(out_assets, ignore_errors=True)
        shutil.copytree(src_assets, out_assets)
        total = sum(p.stat().st_size for p in out_assets.rglob("*") if p.is_file())
        print(f"copied dist/assets ({total:,} bytes)")


if __name__ == "__main__":
    main()
