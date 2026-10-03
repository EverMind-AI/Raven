"""What a command wrote, read off its directory either side of the call.

``exec`` returns a command's output and nothing else, so the files it created,
rewrote or removed leave no record unless the directory is looked at before and
after it runs. Two looks, both taken by the tool around the command itself:

- a listing (:mod:`raven.agent.tools.snapshot`): which paths changed, by size
  and mtime;
- the checkpoint's shadow repo, where one covers the directory: a tree staged
  just before the command, which holds what a rewritten or removed file held.
  Without it a rewrite is still reported, with no counts and no diff.

The shadow repo is handed to the tool (:data:`ShadowFor`) rather than imported:
it belongs to the loop shell, which no tool imports.
"""

from __future__ import annotations

import asyncio
import difflib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Mapping, Protocol

from loguru import logger

from raven.agent.tools import snapshot
from raven.contracts.tool import FileRemoval, FileWrite

#: A file is read for its text only this size or under. Past it the count and
#: the diff are unknown rather than wrong: reading a gigabyte to number it
#: would cost the call more than the row it draws is worth.
TEXT_MAX_BYTES = 256 * 1024

#: What the diffs of one call may add up to, the budget a call's removed bodies
#: and whole-file changes already share. Past it the counts still go and the
#: diff is dropped whole: half a diff reads as a smaller change than happened.
DIFF_BUDGET_CHARS = 512 * 1024

#: How long the shadow repo may take to say, after a command, which files may
#: be shown and what they held. Past it the files go out without their text.
READ_WAIT_SECONDS = 60.0

#: How long everything after a command may take: the second walk, those reads,
#: and reading the written files. Past it the call returns with the command's
#: output and no record of its files, rather than be cut off by the registry's
#: ceiling with neither.
AFTER_WAIT_SECONDS = 120.0

#: The most measuring adds to a call: the shadow repo's wait for a staging (the
#: checkpoint's ``_STAGE_WAIT_SECONDS``) and :data:`AFTER_WAIT_SECONDS`. A tool
#: that measures raises its registry ceiling by it.
MEASURE_SECONDS = 120.0 + AFTER_WAIT_SECONDS


#: Directories a staging has failed in, warned about once each.
_UNSTAGED: set[Path] = set()


class ShadowTree(Protocol):
    """The part of the checkpoint's shadow repo a command is measured against.

    ``stage_tree`` raises :class:`TimeoutError` when the staging did not finish
    within its wait, and returns ``None`` when the tree cannot be staged at all.
    """

    async def warm(self) -> None: ...

    async def stage_tree(self) -> str | None: ...

    async def read_blobs(self, tree: str, paths: Collection[str], *, max_bytes: int) -> dict[str, bytes]: ...

    async def trackable(self, paths: Collection[str]) -> set[str]: ...


#: The shadow repo that covers a directory, or ``None`` where there is none.
ShadowFor = Callable[[Path], "ShadowTree | None"]


@dataclass(frozen=True)
class Before:
    """The directory as it was when the command started."""

    root: Path
    listing: snapshot.Snapshot | None
    #: The repo that rules on what may be shown, wherever one covers ``root``.
    shadow: ShadowTree | None = None
    #: What it staged just before the command, or ``None`` where that failed.
    tree: str | None = None


async def before(root: Path, shadow_for: ShadowFor | None) -> Before:
    """Look at ``root`` just before a command runs in it.

    A tree that could not be staged, or did not finish staging within its wait,
    leaves the command to run without one: it is still listed, so what it wrote
    is reported, only not what those files held. The command matters more than
    its diff. A directory too large to list is not staged at all -- no listing
    means nothing is reported, and a staging would be paid for nothing.

    Off the event loop: the walk is tens of milliseconds of a turn, and every
    other session on this process waits behind whatever the loop does.
    """
    listing = await asyncio.to_thread(snapshot.take, root)
    shadow = shadow_for(root) if listing is not None and shadow_for is not None else None
    try:
        tree = await shadow.stage_tree() if shadow is not None else None
    except TimeoutError:
        # Left running rather than abandoned: what it has hashed is what makes
        # the next command's staging fast.
        logger.warning("exec measured without a diff: staging {} did not finish in time", root)
        tree = None
    else:
        if shadow is not None and tree is None and root not in _UNSTAGED:
            # Once per directory: a broken repo fails every command the same way.
            _UNSTAGED.add(root)
            logger.warning("exec measured without a diff: the shadow repo could not stage {}", root)
    # The repo is kept without a tree: what a file held needs the tree, but
    # whether its text may be shown is the repo's rules, which need none.
    return Before(root, listing, shadow, tree)


async def after(
    start: Before, *, named: tuple[FileRemoval, ...] = ()
) -> tuple[tuple[FileWrite, ...], tuple[FileRemoval, ...]]:
    """What the command changed under ``start.root``: files written, files removed.

    ``named`` are the removals the command's own watch caught by name, with the
    text it read before the command ran. The listing sees them as well, and
    reporting one again would draw a single deletion twice, so they come back
    first and the listing adds only what they miss.

    One rule decides whether a file's contents may be shown, whichever way it
    changed: the shadow repo's (``trackable``), the rules it stores by. A
    ``.env``, a key, anything the user's ``.gitignore`` keeps out is what the
    checkpoint keeps out of storage, and a diff is stored with the conversation.
    The rules rather than the tree: a file staged before an ignore rule named it
    stays in the index, and its text must not be shown for that. Where there is
    no shadow repo to ask, a created file keeps its counts only, and a rewrite
    or listed removal has no earlier text to show.

    Bounded (:data:`AFTER_WAIT_SECONDS`): the command has already run, and its
    output must reach the model whatever the measuring costs. Past the bound
    nothing is reported, and a named removal keeps its text only where no
    shadow repo could have ruled on it; elsewhere it goes out ``withheld``.
    """
    try:
        return await asyncio.wait_for(_after(start, named), AFTER_WAIT_SECONDS)
    except TimeoutError:
        logger.warning("exec files not recorded: measuring {} did not finish in time", start.root)
        if start.shadow is None:
            return (), named
        return (), tuple(FileRemoval(path=removal.path, withheld=True) for removal in named)


async def _after(
    start: Before, named: tuple[FileRemoval, ...]
) -> tuple[tuple[FileWrite, ...], tuple[FileRemoval, ...]]:
    listing = await asyncio.to_thread(snapshot.take, start.root)
    accounted = {os.path.realpath(removal.path) for removal in named}
    created, modified, deleted = (
        [path for path in paths if os.path.realpath(path) not in accounted]
        for paths in snapshot.diff(start.listing, listing)
    )
    held: dict[str, bytes] = {}
    shown: set[str] = set()
    if start.shadow is not None:
        subjects = [*created, *modified, *deleted, *(removal.path for removal in named)]
        try:
            shown, held = await asyncio.wait_for(
                _shown_and_held(start.shadow, start.tree, subjects, [*modified, *deleted]), READ_WAIT_SECONDS
            )
        except TimeoutError:
            # Nothing the repo did not rule on goes out: every file stays bare.
            logger.warning("exec diffs dropped: the shadow repo for {} did not answer in time", start.root)
            shown, held = set(), {}
        named = tuple(
            removal if removal.path in shown else FileRemoval(path=removal.path, withheld=True) for removal in named
        )
    # Off the loop too: this reads every written file, and one command can write hundreds.
    written = (
        await asyncio.to_thread(_writes, created, modified, listing or {}, held, shown) if created or modified else ()
    )
    # A body the rules kept back is marked so, and no other record of the file
    # may put it back; one that is merely unknown is left for them to supply.
    ruled = start.shadow is not None
    removed = named + tuple(
        FileRemoval(path=path, before=_decoded(held.get(path)), withheld=ruled and path not in shown)
        for path in deleted
    )
    return written, removed


async def _shown_and_held(
    shadow: ShadowTree, tree: str | None, subjects: list[str], changed: list[str]
) -> tuple[set[str], dict[str, bytes]]:
    """The subjects the repo's rules would store, and what those of ``changed`` held."""
    shown = await shadow.trackable(subjects) if subjects else set()
    readable = [path for path in changed if path in shown]
    held = await shadow.read_blobs(tree, readable, max_bytes=TEXT_MAX_BYTES) if tree is not None and readable else {}
    return shown, held


def _writes(
    created: Collection[str],
    modified: Collection[str],
    listing: snapshot.Snapshot,
    held: Mapping[str, bytes],
    shown: Collection[str],
) -> tuple[FileWrite, ...]:
    budget = DIFF_BUDGET_CHARS
    out: list[FileWrite] = []
    for path, was_created in [*((path, True) for path in created), *((path, False) for path in modified)]:
        size = listing.get(path, (0, 0))[0]
        old = "" if was_created else _decoded(held.get(path))
        text = None if old is None else _small_text(path, size)
        lines = len(text.splitlines()) if was_created and text is not None else None
        if text is None or old is None:
            out.append(FileWrite(path=path, created=was_created, size=size, lines=lines))
            continue
        diff, added, removed = _line_diff(old, text, os.path.basename(path))
        if was_created and path not in shown:
            diff = None
        if diff is not None and len(diff) <= budget:
            budget -= len(diff)
        else:
            diff = None
        out.append(
            FileWrite(path=path, created=was_created, size=size, lines=lines, added=added, removed=removed, diff=diff)
        )
    return tuple(out)


def _small_text(path: str, size: int) -> str | None:
    """A listed file as text, or ``None`` when it is not worth reading."""
    if size > TEXT_MAX_BYTES:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _decoded(raw: bytes | None) -> str | None:
    if raw is None or len(raw) > TEXT_MAX_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _line_diff(old: str, new: str, name: str) -> tuple[str | None, int, int]:
    """A unified diff of ``old`` to ``new`` with its added and removed line counts."""
    rows = list(difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=name, tofile=name, lineterm=""))
    body = rows[2:]
    added = sum(1 for row in body if row.startswith("+"))
    removed = sum(1 for row in body if row.startswith("-"))
    return ("\n".join(rows) if rows else None), added, removed


__all__ = [
    "AFTER_WAIT_SECONDS",
    "DIFF_BUDGET_CHARS",
    "MEASURE_SECONDS",
    "READ_WAIT_SECONDS",
    "TEXT_MAX_BYTES",
    "Before",
    "ShadowFor",
    "ShadowTree",
    "after",
    "before",
]
