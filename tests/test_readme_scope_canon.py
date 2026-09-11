"""README's repo-layout table stays equal to the tree it declares canonical.

The table is the canonical set of commit scopes (AGENTS.md section 3.1), and
commitlint.config.cjs computes its scope enum from the same tree -- so a
package added or removed without its table row would silently split the two.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TABLE_ROW = re.compile(r"^\| `([a-z0-9_]+)` \|", re.MULTILINE)


def _repo_layout_section() -> str:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    heading = re.search(r"^## (?:\S+\s+)?Repo layout\s*$", text, re.MULTILINE)
    assert heading is not None, "README.md lost its 'Repo layout' section"
    after = text[heading.end() :]
    section, _, _ = after.partition("\n## ")
    assert section, "README.md lost its 'Repo layout' section"
    return section


def test_repo_layout_table_equals_the_packages_on_disk() -> None:
    rows = set(TABLE_ROW.findall(_repo_layout_section()))

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
    text = (REPO / "README.md").read_text(encoding="utf-8")

    assert re.search(r"^agents/\s", text, re.MULTILINE), "Key directories lost its agents/ line"
    assert re.search(r"^plugins-dist/\s", text, re.MULTILINE), "Key directories lost its plugins-dist/ line"
