"""Assemble ui/dist/index.html from the page sources + i18n.

Sources under ``src/``:

- ``page.html``   -- the document skeleton (markup only), with two markers:
                     ``/*__STYLE__*/`` inside its ``<style>`` tag and
                     ``/*__DEMO__*/`` inside its ``<script>`` tag.
- ``styles/page.css`` -- the stylesheet, injected at the style marker.
- ``seam/*.js``   -- the DataSource seam, concatenated ahead of the demo
                     shell so both layers can register into it.
- ``demo/*.js``   -- the demo shell (fixture data + renderers), concatenated
                     in filename order and injected at the demo marker.
                     Serves the design-review canvas on ``file://`` /
                     ``?stub=1``.
- ``live/*.js``   -- live mode; swaps the canned replay for the /rpc
                     WebSocket when served over http. Concatenated in
                     filename order and appended after the demo shell,
                     inside the same script tag. The parts are fragments of
                     one IIFE (opened in the first, closed in the last).
                     Order is load-bearing for BOTH directories and is
                     pinned by the explicit manifests below; the numeric
                     prefixes only mirror them for the reader.

The message catalogue in ``i18n/messages.json`` (shared with the TUI, which
generates a TypeScript copy from it) is inlined at the ``__I18N__`` marker.
Run:

    python ui/build.py
"""

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CATALOG = ROOT.parent / "i18n" / "messages.json"
MARK = "</script>\n</body>"
I18N_MARK = '/*__I18N__*/{ "slash": {}, "ui": {} }'
STYLE_MARK = "/*__STYLE__*/"
MODERN_MARK = "/*__MODERN__*/"
DEMO_MARK = "/*__DEMO__*/"


# Load order is semantics: parts declare bindings earlier parts reference at
# load time, so a reorder can build, parse, and still throw at boot (TDZ).
# No static check can validate semantic order, so the order is pinned the
# only way it can be: explicitly. Renaming, adding, or removing a part must
# update the matching manifest here, where review can see the order change.
_SEAM_PARTS = [
    "000-datasource.js",
]
_DEMO_PARTS = [
    "010-kernel.js",
    "020-prose.js",
    "030-fixtures.js",
    "040-state.js",
    "050-rail.js",
    "060-conversation.js",
    "070-transcript.js",
    "080-replay.js",
    "090-composer.js",
    "100-workspace.js",
    "110-subagents.js",
    "112-browser.js",
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
    "010-boot-guard.js",
    "020-rpc.js",
    "030-sessions.js",
    "040-history.js",
    "050-turn.js",
    "060-parked.js",
    "070-notify.js",
    "080-overrides.js",
    "090-extensions.js",
    "100-schedules.js",
    "110-connections.js",
    "120-settings.js",
    "130-writes.js",
    "140-skills.js",
    "150-plugins.js",
    "160-memory.js",
    "165-knowledge.js",
    "167-playbooks.js",
    "170-workspace.js",
    "180-attachments.js",
    "190-session-actions.js",
    "200-boot.js",
    "210-update-notice.js",
    "220-browser.js",
    "230-tabs.js",
    "240-external-agents.js",
]


def _concat(subdir: str, manifest: list[str]) -> str:
    found = {p.name for p in (ROOT / "src" / subdir).glob("*.js")}
    if found != set(manifest):
        extra = sorted(found - set(manifest))
        missing = sorted(set(manifest) - found)
        raise SystemExit(
            f"src/{subdir} does not match its manifest in build.py"
            + (f"; not in manifest: {extra}" if extra else "")
            + (f"; missing: {missing}" if missing else "")
        )
    texts = [(ROOT / "src" / subdir / name).read_text(encoding="utf-8") for name in manifest]
    if subdir == "live":
        # The live parts are fragments of ONE IIFE: first part opens it,
        # last part closes it. This anchors the wrapper only -- everything
        # between is ordered by the manifest above, not by any brace math.
        if "(() => {" not in texts[0]:
            raise SystemExit(f"src/live: first part {manifest[0]} does not open the IIFE")
        if not texts[-1].rstrip("\n").endswith("})();"):
            raise SystemExit(f"src/live: last part {manifest[-1]} does not close the IIFE")
    text = "".join(texts)
    return text[:-1] if text.endswith("\n") else text


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
            "ui/.modern/modern.iife.js not found -- run `npm ci --prefix ui` "
            "then `npm run --prefix ui build` before ui/build.py"
        )
    modern = modern_path.read_text(encoding="utf-8")
    modern = modern[:-1] if modern.endswith("\n") else modern
    for mark, text in (
        (STYLE_MARK, style),
        (MODERN_MARK, modern),
        (DEMO_MARK, _concat("seam", _SEAM_PARTS) + "\n" + _concat("demo", _DEMO_PARTS)),
    ):
        if page.count(mark) != 1:
            raise SystemExit(f"page.html: expected exactly one {mark} marker")
        page = page.replace(mark, text, 1)
    live = _concat("live", _LIVE_PARTS) + "\n"
    if MARK not in page:
        raise SystemExit("page.html: closing script marker not found")
    if I18N_MARK not in page:
        raise SystemExit("page.html: i18n catalogue marker not found")
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    catalog.pop("_readme", None)
    page = page.replace(I18N_MARK, json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), 1)
    out = page.replace(MARK, f"\n{live}\n{MARK}", 1)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(out, encoding="utf-8")
    print(f"built dist/index.html ({len(out):,} bytes)")

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
        print(f"copied dist/assets ({total:,} bytes)")


if __name__ == "__main__":
    main()
