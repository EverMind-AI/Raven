"""Bench registry: name -> ``module:function`` building a BenchBundle."""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import Callable, Optional, Union

BENCHES: dict[str, str] = {
    "appworld": "benchmarks.appworld.evolve.entry:build",
}


def load_bench(name: str, repo_root: Optional[Union[str, Path]] = None) -> Callable:
    """Import the bench plugin registered under ``name``.

    Bench plugins live in the subject repo (repo-root ``benchmarks/``), not in
    the installed raven package, so ``repo_root`` — the subject checkout — is
    put first on ``sys.path`` before importing. Omitting it only works when
    the checkout root is already importable (e.g. cwd is the checkout).
    """
    target = BENCHES.get(name)
    if target is None:
        raise ValueError(
            f"unknown bench {name!r}; registered: {sorted(BENCHES)} "
            "(add yours to raven.evolver.launch.registry.BENCHES)"
        )
    if repo_root is not None:
        root = str(Path(repo_root).resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
    mod_name, _, fn_name = target.partition(":")
    return getattr(import_module(mod_name), fn_name)


__all__ = ["BENCHES", "load_bench"]
