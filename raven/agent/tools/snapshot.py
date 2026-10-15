"""A before/after listing of a working directory, for the changes no result names.

A file tool reports what it wrote, so the lane recording a run's files can read
the change off the call itself. A command cannot: ``exec`` returns its output and
nothing else, and an external agent's own shell is even quieter -- a file the
command created, rewrote or removed leaves no trace in anything the host is
handed. The only account left is what the directory looked like on either side
of the call, which is what this takes.

Size and mtime rather than contents: the question is which paths changed, and
reading every file of a tree around every command would cost more than the run.
Bounded on purpose -- a run pointed at a large tree stops walking and reports
nothing, because a partial listing would read as a run that deleted everything
the walk did not reach.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

#: Directories a walk never enters. Machinery rather than work: a run that
#: installs a dependency or lands a commit would otherwise report thousands of
#: files it did not author. ``.raven`` is the same kind of thing one level in:
#: the checkpoint keeps a shadow git repo inside the very directory a command
#: runs in, and a turn that commits into it while another turn's command is
#: running would surface as that command's files.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".raven",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
    }
)

#: Past this the walk gives up and the run records nothing from snapshots. The
#: ceiling is on the listing, not on what changed: holding a dict this large
#: twice per call is already more than a file record is worth.
MAX_ENTRIES = 20000

#: Absolute path -> (size, mtime_ns), for the regular files of one tree.
Snapshot = dict[str, tuple[int, int]]


def take(root: Path | str) -> Snapshot | None:
    """List ``root``'s regular files with what would change if one were written.

    ``None`` means no listing: the tree is past :data:`MAX_ENTRIES`, or the root
    is not a directory this process can walk. Not an empty one -- an empty
    listing compared against a real one reports every file in the tree as
    created. Symlinks are skipped on both counts: a link is not a file this run
    wrote, and following one leaves the tree being described.
    """
    try:
        start = Path(root).resolve()
    except OSError:
        return None
    if not start.is_dir():
        return None
    entries: Snapshot = {}
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            entries[path] = (info.st_size, info.st_mtime_ns)
            if len(entries) > MAX_ENTRIES:
                return None
    return entries


def diff(before: Snapshot | None, after: Snapshot | None) -> tuple[list[str], list[str], list[str]]:
    """Created, modified and deleted paths between two listings, each sorted.

    Either side missing is no comparison at all rather than a total one: a walk
    that gave up must not report every file in the tree as created or deleted.
    """
    if before is None or after is None:
        return [], [], []
    created = sorted(path for path in after if path not in before)
    modified = sorted(path for path in after if path in before and after[path] != before[path])
    deleted = sorted(path for path in before if path not in after)
    return created, modified, deleted


def root_for(tool: Any, params: dict[str, Any], fallback: Path | str) -> Path | None:
    """The directory to list around one call of ``tool`` with ``params``.

    A tool that can say where its files land answers for itself through
    ``listing_root``: a per-call working directory moves the root, and a
    command sent to a registered machine leaves nothing on this disk, which is
    ``None`` -- no listing rather than an empty one. A tool that cannot say is
    taken to run where the lane's own tools run, ``fallback``.
    """
    ask = getattr(tool, "listing_root", None)
    return ask(params) if callable(ask) else Path(fallback)
