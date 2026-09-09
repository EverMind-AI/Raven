"""Tripwire for the symbols external installers import.

The agent products (``agents/raven-*/install.py``) run against the INSTALLED
raven package and import from it to register themselves as third-party
subagents -- and the copies that matter are the ones a wheel carried out to a
user's raven home, which are not in this repo's CI. This tripwire stands in
for them: renaming or moving any of the names they import breaks five
installed products at their next ``install.py`` run, and the break surfaces at
their install time rather than here.

The list is read out of the installers rather than restated here, because a
restated list is the thing that goes stale: the tripwire pinned four names while
the installers imported six, so two config classes and a remover could have been
renamed with this file green. An installer that reaches for a new name is
covered the moment it does.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

PRODUCTS = Path(__file__).resolve().parent.parent / "agents"
#: The installers write their raven calls into scripts they feed to raven's own
#: interpreter -- a heredoc in install.sh, a string literal in each install.py --
#: so the imports live in text rather than in any file's own import list. Read
#: the text.
IMPORT = re.compile(r"from (raven\.[\w.]+) import ([\w, ]+)")


def _promised_names(path: Path) -> set[tuple[str, str]]:
    return {
        (module, name.split(" as ")[0].strip())
        for module, names in IMPORT.findall(path.read_text(encoding="utf-8"))
        for name in names.split(",")
        if name.strip()
    }


def _external_surface() -> set[tuple[str, str]]:
    """Every raven name the product trees reach for: what the installers run,
    plus what the charter hands an operator to run by hand."""
    sources = [
        *sorted(PRODUCTS.glob("*/install.py")),
        PRODUCTS / "README.md",
    ]
    return {pair for path in sources for pair in _promised_names(path)}


def test_the_installers_are_where_this_tripwire_thinks_they_are() -> None:
    """A glob that matches nothing would make every assertion below vacuous."""
    installers = sorted(PRODUCTS.glob("*/install.py"))

    assert len(installers) >= 5, [p.name for p in installers]
    assert len(_external_surface()) >= 1, sorted(_external_surface())


@pytest.mark.parametrize("module,name", sorted(_external_surface()))
def test_an_external_installer_still_finds_what_it_imports(module: str, name: str) -> None:
    """A change here must be coordinated with the product teams first, because
    the installed copies of these installers pin these names."""
    assert hasattr(importlib.import_module(module), name), f"{module}.{name} is gone"


def test_config_loader_surface_is_stable():
    from raven.config.loader import get_config_path, read_raw_or_raise

    assert callable(get_config_path)
    assert callable(read_raw_or_raise)
