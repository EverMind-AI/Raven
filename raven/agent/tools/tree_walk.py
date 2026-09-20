"""One bounded, pruned directory walk for every tool that covers a tree.

``find``, ``list_dir`` and the pure-Python ``grep`` fallback all take a
directory the model names, and a model will name a home directory. Three
things keep that from wedging the gateway: the walk never descends into the
noise directories where most of a tree's inodes live, it stops at a
wall-clock deadline and lets the caller say so, and every caller runs it off
the event loop. The registry's ``wait_for`` ceiling cannot preempt
synchronous code, so the bound has to live inside the walk itself.
"""

import os
import time
from collections.abc import Iterator
from pathlib import Path

IGNORE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".coverage",
        "htmlcov",
    }
)

# Wall-clock cap on one traversal, so an allowed-but-huge tree still returns.
# ripgrep has its own timeout and does not go through here.
WALK_DEADLINE_S = 20.0


def walk(base: Path, *, deadline_s: float | None = None) -> Iterator[tuple[str, list[str], list[str]]]:
    """``os.walk`` with the noise directories pruned and a deadline enforced.

    Yields ``(root, dirs, names)`` top-down with ``dirs`` already pruned, so an
    ignored directory is neither listed nor entered. Symbolic links to
    directories are listed, not followed. Raises ``TimeoutError`` at the first
    directory reached after ``deadline_s`` seconds (the module default when
    ``None``); whatever the caller collected before that is its partial result.
    """
    if deadline_s is None:
        deadline_s = WALK_DEADLINE_S
    deadline = time.monotonic() + deadline_s
    for root, dirs, names in os.walk(base):
        if time.monotonic() > deadline:
            raise TimeoutError(f"traversal deadline of {deadline_s:g}s exceeded under {base}")
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        yield root, dirs, names
