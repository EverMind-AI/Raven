"""Every module an import contract names is a module that exists.

import-linter reads the static import graph and answers the question the
contract asks. It does not answer a question nobody asked: whether the modules
in ``source_modules`` / ``forbidden_modules`` are real. A contract naming a
module that never existed -- a typo, or a package renamed by a later refactor --
is reported KEPT with exit code 0, so the gate goes green while checking
nothing, and nothing about the output says so.

The nine contracts name 63 module paths between them. Each one is a place a
rename can silently retire a contract, which is why this is a test and not a
convention. It resolves the dotted names against the tree rather than importing
them: ``importlib.util.find_spec`` would import every parent package, and a
guard over the contract roster should not depend on the runtime it guards.

The docs carry one live example of the drift being guarded against:
docs/TRACING_STANDARD_API.md still names raven.tracing.storage and
raven.tracing.viewer, which the tracing package no longer has. A contract
written from that text would be one of these permanent green lights.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE_KEYS = ("source_modules", "forbidden_modules", "modules")


def _contracts() -> list[dict]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["importlinter"]["contracts"]


def _named_modules() -> dict[str, list[str]]:
    """Module path -> the contracts that name it."""
    named: dict[str, list[str]] = {}
    for contract in _contracts():
        for key in MODULE_KEYS:
            for module in contract.get(key, []):
                named.setdefault(module, []).append(contract["name"])
    return named


def _exists(module: str) -> bool:
    path = REPO.joinpath(*module.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file() or path.is_dir()


def test_the_roster_is_not_empty() -> None:
    """An empty roster would make the check below pass over nothing."""
    named = _named_modules()

    assert len(named) >= 40, sorted(named)


def test_every_named_module_exists() -> None:
    named = _named_modules()

    missing = {m: sorted(set(cs)) for m, cs in sorted(named.items()) if not _exists(m)}

    assert not missing, (
        "these contracts name modules that are not in the tree, so they are "
        f"vacuous -- KEPT without checking anything: {missing}"
    )
