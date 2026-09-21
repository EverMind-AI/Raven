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


def walk(base: Path, *, deadline_s: float | None = None) -> Iterator[tuple[str, str, bool]]:
    """Every entry under ``base``, one at a time, pruned and under a deadline.

    Yields ``(root, name, is_dir)`` top-down: a directory's sub-directories
    first, in listing order, then its files in name order. An ignored directory
    is neither yielded nor entered, and symbolic links to directories are
    yielded but not followed. The deadline is checked before every entry, not
    once per directory: whatever the caller does with one entry -- a ``stat``,
    a file read -- is covered by the check on the next, so one large or slow
    directory cannot run past the budget on the caller's side of the yield.
    Raises ``TimeoutError`` at the first entry due after ``deadline_s`` seconds
    (the module default when ``None``); what the caller collected before that
    is its partial result.
    """
    if deadline_s is None:
        deadline_s = WALK_DEADLINE_S
    deadline = time.monotonic() + deadline_s
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for name, is_dir in [(d, True) for d in dirs] + [(n, False) for n in sorted(names)]:
            if time.monotonic() > deadline:
                raise TimeoutError(f"traversal deadline of {deadline_s:g}s exceeded under {base}")
            yield root, name, is_dir
