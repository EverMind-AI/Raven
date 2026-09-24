"""Watching for files a run's own tool calls made vanish.

No tool deletes a file as its purpose, so a deletion is only ever visible as a
path that was there before a call and is not there after. The shell tool watches
the paths its own command names; this watches the paths the run itself wrote,
which is the other half -- a file ``write_file`` created and a later command
removed under a name the fence could not resolve is seen by neither side alone.
"""

from __future__ import annotations

import os
from typing import Any

from raven.contracts.tool import FileRemoval

#: Per path, the most a watch holds of what was last written there. Past it the
#: text is dropped rather than truncated, for the reason an oversized file change
#: is dropped: half a file reads as a smaller change than the one that happened.
WATCHED_TEXT_MAX_CHARS = 512 * 1024

#: Across the whole watch. A turn that writes hundreds of files would otherwise
#: hold every one of them whole until it ends, on top of what the loop already
#: carries. Past this the path is still watched and only its text is dropped --
#: the same "path without a body" a removal already has a shape for.
WATCHED_TOTAL_MAX_CHARS = 4 * 1024 * 1024


class RemovalWatch:
    """What one run has written, so a later call that unlinks it is seen.

    Scoped to a turn (the main lane) or a run (the sub-agent lane): the paths are
    absolute and the texts are whole files, so a watch that outlived its run would
    both grow without bound and report another run's deletions as this one's.
    """

    def __init__(self) -> None:
        self._touched: dict[str, str | None] = {}
        self._chars = 0

    def note_write(self, change: Any) -> None:
        """Remember what a call left at a path, against a later call removing it."""
        path = getattr(change, "path", None)
        after = getattr(change, "after", None)
        if not isinstance(path, str) or not path or not isinstance(after, str):
            return
        self._forget(path)
        keep = len(after) <= WATCHED_TEXT_MAX_CHARS and self._chars + len(after) <= WATCHED_TOTAL_MAX_CHARS
        self._touched[path] = after if keep else None
        self._chars += len(after) if keep else 0

    def settle(self, reported: Any = ()) -> list[FileRemoval]:
        """This call's removals: what the tool reported, then what vanished unseen.

        A path that is gone leaves the watch, so one deletion is reported once; a
        later write of the same path puts it back and it can be reported again.
        """
        removals = [
            removal for removal in (reported or ()) if isinstance(getattr(removal, "path", None), str) and removal.path
        ]
        already = {removal.path for removal in removals}
        for path in list(self._touched):
            if path in already:
                self._forget(path)
                continue
            if not os.path.exists(path):
                removals.append(FileRemoval(path=path, before=self._forget(path)))
        return removals

    def _forget(self, path: str) -> str | None:
        """Drop a path from the watch, giving back whatever text it held."""
        text = self._touched.pop(path, None)
        if isinstance(text, str):
            self._chars -= len(text)
        return text


__all__ = ["WATCHED_TEXT_MAX_CHARS", "WATCHED_TOTAL_MAX_CHARS", "RemovalWatch"]
