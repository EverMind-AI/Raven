"""Deadline-bounded path checks for Raven-Code's post-command diagnostics only."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def eligible(root: Path, candidates: list[tuple[Path, T]], *, deadline: float) -> list[tuple[Path, T]]:
    """Confine diagnostic reads without filesystem operations on the event loop."""
    resolved_root = root.resolve()
    selected = []
    for path, old in candidates:
        if time.monotonic() >= deadline:
            raise TimeoutError("diagnostic path filtering exceeded its deadline")
        if path.suffix == ".py" and not path.is_symlink() and path.resolve().is_relative_to(resolved_root):
            selected.append((path, old))
    return selected
