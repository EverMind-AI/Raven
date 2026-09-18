"""The documentation site's pages keep the two properties a broken move costs.

A repository-relative link in a .md page is already caught by mkdocs build
--strict; this guard catches non-.md targets (e.g., docker/.env) that --strict
misses, and runs in the ordinary test suite without needing the docs uv group.
A page added in one language only leaves the other language's reader on a
fallback they cannot tell from a translation.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SITE = REPO / "docs-site" / "docs"

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _pages() -> list[Path]:
    return sorted(SITE.glob("*.md"))


def test_every_page_has_both_languages() -> None:
    english = {path.stem for path in _pages() if not path.name.endswith(".zh.md")}
    chinese = {path.name[: -len(".zh.md")] for path in _pages() if path.name.endswith(".zh.md")}

    assert english == chinese, (
        f"English page without a Chinese twin: {sorted(english - chinese)}; "
        f"Chinese page without an English twin: {sorted(chinese - english)}"
    )
    assert english, "no pages found -- did SITE move?"


def test_no_page_links_into_the_repository_by_a_relative_path() -> None:
    pages = _pages()
    assert pages, "no pages found -- did SITE move?"
    offenders: list[str] = []
    for page in pages:
        for target in LINK.findall(page.read_text(encoding="utf-8")):
            link = target.split("#", 1)[0].split(" ", 1)[0]
            if not link or link.startswith(("http://", "https://", "mailto:")):
                continue
            if "/" not in link and link.endswith(".md"):
                continue
            offenders.append(f"{page.name} -> {target}")

    assert not offenders, (
        "repository-relative links in non-.md targets are not caught by mkdocs "
        "build --strict; make them absolute github.com URLs: "
        f"{offenders}"
    )
