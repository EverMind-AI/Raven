"""A temporary directory an agent works in, removed only once the agent has ended.

Windows will not remove a directory that a live process has as its working
directory: the removal fails with ``WinError 32`` even when the directory is
empty, where POSIX lets the process keep it. So a check that hands an agent a
fresh directory removes it after ending the agent's process tree, and retries
for a moment, because a tree that was just ended can still be exiting.

A directory that outlasts the retries is logged and left in the temp folder,
never raised: the caller reports the agent's answer, and a teardown failure
reported in its place says that an agent which answered did not.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

_REMOVE_ATTEMPTS = 5
_REMOVE_PAUSE_S = 0.2


class ScratchDir:
    """A fresh temporary directory, and its removal once nothing runs in it.

    ``async with`` yields the path and removes the directory when the block
    ends, for a caller whose agent has ended inside the block. A caller that
    ends its agent in a ``finally`` of its own calls `remove` after that.
    """

    def __init__(self, prefix: str) -> None:
        self._held = tempfile.TemporaryDirectory(prefix=prefix)
        self.path = Path(self._held.name)

    async def __aenter__(self) -> Path:
        return self.path

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.remove()

    async def remove(self) -> None:
        """Remove the directory and everything in it; what cannot be removed is logged."""
        attempt = 0
        while True:
            attempt += 1
            try:
                # The stdlib's own removal, read-only entries included: Windows
                # will not delete a read-only file, and git marks its objects so.
                await asyncio.to_thread(self._held.cleanup)
                return
            except OSError as exc:
                if attempt >= _REMOVE_ATTEMPTS:
                    logger.warning("could not remove {} after {} attempts, leaving it: {}", self.path, attempt, exc)
                    return
            await asyncio.sleep(_REMOVE_PAUSE_S)


__all__ = ["ScratchDir"]
