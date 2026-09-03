"""Inner layers render nothing: no rich / typer / click import outside the surfaces.

import-linter enforces the layer rule for raven's own packages but cannot see
third-party toolkits, so a rich Console inside a shelf is invisible to it. This
guard walks every module outside cli / rpc / acp and refuses any toolkit import,
function-level ones included. The three files below still render through rich
today (a card, SEAT-3); they are listed so the debt is visible, and the list may
only shrink -- a file that stops importing the toolkit fails the second check
until it is removed here.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SURFACES = ("cli", "rpc", "acp")
TOOLKITS = ("rich", "typer", "click")

# No inner module renders through a toolkit today. A file that must, for a
# reason argued in its commit, goes here -- and the second test makes the entry
# expire the day the import is gone.
ALLOWED_TODAY: set[str] = set()


def _toolkit_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(errors="replace"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.split(".")[0] in TOOLKITS]
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in TOOLKITS:
            hits.append(node.module)
    return hits


def _inner_files() -> list[Path]:
    out = []
    for p in (REPO / "raven").rglob("*.py"):
        rel = p.relative_to(REPO).parts
        if "__pycache__" in rel or (len(rel) > 2 and rel[1] in SURFACES):
            continue
        out.append(p)
    return sorted(out)


def test_no_inner_module_imports_a_terminal_toolkit_outside_the_known_three() -> None:
    offenders = {
        str(p.relative_to(REPO)): hits
        for p in _inner_files()
        if (hits := _toolkit_imports(p)) and str(p.relative_to(REPO)) not in ALLOWED_TODAY
    }
    assert not offenders, f"inner modules rendering through a terminal toolkit: {offenders}"


def test_the_known_three_still_need_their_entry_or_the_list_shrinks() -> None:
    stale = [rel for rel in sorted(ALLOWED_TODAY) if not _toolkit_imports(REPO / rel)]
    assert not stale, f"no longer import a toolkit; remove from ALLOWED_TODAY: {stale}"
