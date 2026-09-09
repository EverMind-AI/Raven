"""A local-filesystem implementation of the DAG core's duck-typed file backend.

The DAG core (store / render) is written against a small backend contract so it
can run over any workspace. This is the concrete Raven-side adapter over the
local filesystem (node executors run on the host in v1). Only file I/O is needed
— node execution is the SubagentBackend's job, not the backend's.
"""

from __future__ import annotations

import os
from pathlib import Path


class LocalFileBackend:
    def join_path(self, *parts: str) -> str:
        return os.path.join(*parts)

    def abspath(self, path: str, cwd: str | None = None) -> str:
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(cwd or os.getcwd(), path))

    async def read_file(self, path: str) -> bytes:
        return Path(path).read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    async def file_exists(self, path: str) -> bool:
        # `is_file`, not `exists`: every caller is asking whether there is a file
        # here to read, and a directory answering yes sent `{{ ref:<dir> }}` past
        # the existence check into `read_bytes`, which raised IsADirectoryError
        # out of the tool instead of returning the shaped advice a mistyped
        # reference gets. The RPC-side backend already agreed on `is_file`.
        return Path(path).is_file()
