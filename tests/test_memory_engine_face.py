"""The memory engine is entered through ``raven.memory_engine``, not its layout.

Consumers name what they need on the package; the modules underneath are the
engine's own to rearrange. This pins both halves: no module outside the engine
imports one of its submodules, and every name the face lists resolves.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENGINE = "raven.memory_engine"


def _internal_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").startswith(ENGINE + "."):
            hits.append(f"{path.relative_to(REPO)}:{node.lineno} {node.module}")
        if isinstance(node, ast.Import) and any(a.name.startswith(ENGINE + ".") for a in node.names):
            hits.append(f"{path.relative_to(REPO)}:{node.lineno} import")
    return hits


def test_nothing_outside_the_engine_imports_its_submodules() -> None:
    hits = [
        h
        for p in (REPO / "raven").rglob("*.py")
        if not p.relative_to(REPO).as_posix().startswith("raven/memory_engine/")
        for h in _internal_imports(p)
    ]
    assert hits == [], "reach the memory engine through raven.memory_engine: " + "; ".join(hits)


def test_every_face_name_resolves() -> None:
    import raven.memory_engine as face

    for name in face.__all__:
        assert getattr(face, name) is not None, name
