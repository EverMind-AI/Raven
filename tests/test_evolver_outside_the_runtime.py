"""The evolver is a tool over the library, not part of it.

``evolver/`` sits beside ``raven/`` in the checkout, imports raven, and is not
shipped in the wheel. Nothing under ``raven/`` reaches back into it; the fifth
import-linter contract says the same to the linter, this test says it to pytest.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _imports_evolver(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "evolver" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "evolver":
            return True
    return False


def test_the_runtime_imports_nothing_from_the_evolver() -> None:
    offenders = sorted(str(p.relative_to(REPO)) for p in (REPO / "raven").rglob("*.py") if _imports_evolver(p))
    assert offenders == []


def test_the_evolver_lives_beside_the_package_and_outside_the_wheel() -> None:
    assert (REPO / "evolver" / "__init__.py").exists()
    assert not (REPO / "raven" / "evolver" / "__init__.py").exists()
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    wheel = re.search(r"\[tool\.hatch\.build\.targets\.wheel\]\npackages = \[(.*?)\]", pyproject)
    assert wheel is not None
    assert "evolver" not in wheel.group(1)


def test_the_evolver_imports_from_the_checkout() -> None:
    import evolver

    assert Path(evolver.__file__).resolve().parent == (REPO / "evolver").resolve()
