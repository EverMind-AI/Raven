#!/usr/bin/env python3
"""Render every bundled deck template's cover into the engine's package.

A cover is a LibreOffice conversion and a rasterise, and the gateway used to
start ten of them shortly after boot on every host -- including the ones whose
reader never opens the template picker. This renders them once, at build time,
into ``raven_ppt/assets/templates/covers/``; the ppt-engine wheel carries that
directory and ``deck_templates.shipped_cover`` finds them there, so the warm-up
has nothing left to draw and the picker opens on files that are already local.

The inputs are the template directory's own contents -- every ``.pptx`` beside
every phrasebook under ``i18n/`` -- so a template or a language added later is
covered by adding that file, not by editing this script. What it writes is
ignored by git (AGENTS section 7 admits no image assets to the repository) and
asserted in the built wheel by ``release.yml``.

Exit codes: 0 every cover written; 1 the engine, LibreOffice or the rasteriser
is missing, or a conversion failed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from raven.rpc import deck_templates  # noqa: E402
from raven.utils.office import find_soffice, install_hint  # noqa: E402


def languages(root: Path) -> list[str]:
    """The source language, then every language a phrasebook beside the templates names."""
    return [deck_templates.SOURCE_LANGUAGE, *sorted(p.stem for p in (root / "i18n").glob("*.json"))]


async def bake(covers: Path) -> int:
    """Draw one cover per template per language into ``covers``; the number written."""
    root = covers.parent
    written = 0
    seen: set[str] = set()
    for template in deck_templates.bundled():
        for language in languages(root):
            # A phrasebook that is absent or unreadable leaves the reader on the
            # shipped file, which is the cover already drawn for the source
            # language and under that name -- not a second one to draw.
            name = deck_templates.shipped_cover_name(template, language)
            if name in seen:
                continue
            seen.add(name)
            target = covers / f"{name}.jpg"
            await deck_templates.draw_cover(template, target, language)
            print(f"{target.relative_to(root)}  {target.stat().st_size // 1024} K")
            written += 1
    return written


def main() -> int:
    root = deck_templates.templates_dir()
    if root is None:
        print("no deck engine installed: nothing to bake", file=sys.stderr)
        return 1
    if find_soffice() is None:
        print(f"LibreOffice is not on PATH. Install it with: {install_hint()}", file=sys.stderr)
        return 1
    if not deck_templates._rasteriser_available():
        print("PyMuPDF is not importable: covers cannot be rasterised", file=sys.stderr)
        return 1
    covers = root / "covers"
    covers.mkdir(parents=True, exist_ok=True)
    written = asyncio.run(bake(covers))
    print(f"OK: {written} cover(s) under {covers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
