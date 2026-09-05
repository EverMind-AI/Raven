"""The kernel's size budget, pinned to the numbers the L0 booklet froze.

raven/spine is the frozen kernel. Frozen means the promise is not broken while
the implementation may change, and additions are safe -- but only inside a
budget, or "frozen" stops meaning anything as the tree around it grows. The L0
booklet ruled four clauses and measured each true by hand on the two trees it
compared (1,347 and 1,355 lines; this branch measured 1,360 when the guard
landed):

1. the package holds at most LINE_CEILING lines of Python;
2. it imports no other raven package -- every import is its own, the stdlib,
   or an allowed third-party name, in-function imports included;
3. the third-party names are limited to THIRD_PARTY_ALLOWED (tiktoken lives in
   utils, not here);
4. no file carries a TODO, FIXME or HACK marker.

The two numbers were set tight on purpose. Loose (5,000 lines, four third-party
packages) would let the kernel grow to three and a half times its size before
anything went red, which three years on is no guard at all; tight means adding
anything to the kernel passes one explicit review. The booklet also names the
signal that a threshold was set wrong: loose, and the assertion never once
fails; tight, and someone starts bumping the number in passing -- the moment
that happens the guard has lost its authority. So a change to a constant below
is a reviewed change to this file, never a drive-by, and this docstring is
where the reviewer reads why the number is what it is.

Clause 2 overlaps `the kernel stands alone` (import-linter) and
tests/test_kernel_closure.py, which ask about the whole kernel set; this file
asks the narrower question the booklet asked, of spine alone.

One addendum (2026-08-31) extends clause 1's discipline to raven/contracts:
the papers are additions-safe by design, but additions inside a budget --
1,922 lines when the ceiling landed -- so a new paper passes the same
explicit review a kernel line does, in the same file the reviewer already
reads for why the numbers are what they are.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

LINE_CEILING = 2_000
CONTRACTS_LINE_CEILING = 2_500
THIRD_PARTY_ALLOWED = frozenset({"loguru"})
DEBT_MARKER = re.compile(r"\b(TODO|FIXME|HACK)\b")

REPO = Path(__file__).resolve().parent.parent
KERNEL_PACKAGE = "raven.spine"
SPINE = REPO / "raven" / "spine"
CONTRACTS = REPO / "raven" / "contracts"


def _spine_files() -> list[Path]:
    files = sorted(p for p in SPINE.rglob("*.py") if "__pycache__" not in p.parts)
    assert files, "raven/spine has no Python files; a budget measured on nothing is met by nothing"
    return files


def _imports() -> list[tuple[str, str]]:
    """Every import in the package as (module, where), in-function ones
    included; a relative import counts as the package itself."""
    found = []
    for p in _spine_files():
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] if node.level == 0 else [KERNEL_PACKAGE]
            else:
                continue
            found += [(m, f"{p.relative_to(REPO)}:{node.lineno} -> {m}") for m in mods]
    assert found, "raven/spine imports nothing; the walk found no statements to judge"
    return found


def test_the_kernel_stays_under_its_line_ceiling() -> None:
    lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in _spine_files())

    assert 0 < lines <= LINE_CEILING, (
        f"raven/spine is {lines} lines against a ceiling of {LINE_CEILING}; "
        "growing the kernel is a reviewed change to this ceiling, not a bump in passing"
    )


def test_the_kernel_imports_no_other_raven_package() -> None:
    strays = [
        where
        for mod, where in _imports()
        if mod.split(".")[0] == "raven" and mod != KERNEL_PACKAGE and not mod.startswith(KERNEL_PACKAGE + ".")
    ]

    assert strays == [], f"the kernel reaches outside itself: {strays}"


def test_the_kernel_names_no_third_party_beyond_the_allowed() -> None:
    beyond = sorted(
        where
        for mod, where in _imports()
        if (root := mod.split(".")[0]) not in sys.stdlib_module_names
        and root != "raven"
        and root not in THIRD_PARTY_ALLOWED
    )

    assert beyond == [], f"the kernel took on a dependency outside {sorted(THIRD_PARTY_ALLOWED)}: {beyond}"


def test_the_kernel_carries_no_debt_markers() -> None:
    marked = [
        f"{p.relative_to(REPO)}:{n}"
        for p in _spine_files()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if DEBT_MARKER.search(line)
    ]

    assert marked == [], f"the frozen kernel carries deferred work: {marked}"


def test_the_papers_stay_under_their_line_ceiling() -> None:
    files = sorted(p for p in CONTRACTS.rglob("*.py") if "__pycache__" not in p.parts)
    assert files, "raven/contracts has no Python files; a budget measured on nothing is met by nothing"
    lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in files)

    assert 0 < lines <= CONTRACTS_LINE_CEILING, (
        f"raven/contracts is {lines} lines against a ceiling of {CONTRACTS_LINE_CEILING}; "
        "a new paper is a reviewed change to this ceiling, not a bump in passing"
    )
