"""The repo-layout table stays equal to the tree it declares canonical.

The table is the canonical set of commit scopes (AGENTS.md section 3.1), and
commitlint.config.cjs computes its scope enum from the same tree -- so a
package added or removed without its table row would silently split the two.

The table lives on the documentation site rather than in README.md: the page
is the canonical one a reader is sent to, and a second copy in the README
would be the thing that drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TABLE_ROW = re.compile(r"^\| `([a-z0-9_]+)` \|", re.MULTILINE)

REPO_LAYOUT = REPO / "docs-site" / "docs" / "repo-layout.md"


def _repo_layout_page() -> str:
    assert REPO_LAYOUT.exists(), f"the repo-layout page is gone from {REPO_LAYOUT.relative_to(REPO)}"
    return REPO_LAYOUT.read_text(encoding="utf-8")


def test_repo_layout_table_equals_the_packages_on_disk() -> None:
    rows = set(TABLE_ROW.findall(_repo_layout_page()))

    packages = {
        entry.name for entry in (REPO / "raven").iterdir() if entry.is_dir() and (entry / "__init__.py").exists()
    }
    modules = {
        entry.stem for entry in (REPO / "raven").glob("*.py") if entry.name not in {"__init__.py", "__main__.py"}
    }

    assert rows == packages | modules, (
        f"table without a package on disk: {sorted(rows - (packages | modules))}; "
        f"package on disk without a table row: {sorted((packages | modules) - rows)}"
    )


def test_key_directories_block_names_the_product_and_plugin_trees() -> None:
    text = _repo_layout_page()

    assert re.search(r"^agents/\s", text, re.MULTILINE), "Key directories lost its agents/ line"
    assert re.search(r"^plugins-dist/\s", text, re.MULTILINE), "Key directories lost its plugins-dist/ line"
