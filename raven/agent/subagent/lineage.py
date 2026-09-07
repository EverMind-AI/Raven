"""Runtime-owned delegation ancestry shared by in-process and process children."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

MAX_SPAWN_DEPTH = 1


@dataclass(frozen=True)
class SpawnLineage:
    parent_id: str | None = None
    depth: int = 0


_lineage: ContextVar[SpawnLineage | None] = ContextVar("raven_spawn_lineage", default=None)


def current_lineage() -> SpawnLineage:
    bound = _lineage.get()
    if bound is not None:
        return bound
    parent_id = os.environ.get("RAVEN_PARENT_ID") or None
    raw_depth = os.environ.get("RAVEN_SPAWN_DEPTH", "1" if parent_id else "0")
    try:
        depth = int(raw_depth)
    except ValueError:
        depth = MAX_SPAWN_DEPTH
    if depth < 0 or (parent_id and depth == 0):
        depth = MAX_SPAWN_DEPTH
    return SpawnLineage(parent_id, depth)


@contextmanager
def child_run(parent_id: str) -> Iterator[None]:
    token = _lineage.set(SpawnLineage(parent_id, current_lineage().depth + 1))
    try:
        yield
    finally:
        _lineage.reset(token)


def lineage_env() -> dict[str, str]:
    lineage = current_lineage()
    return {
        "RAVEN_PARENT_ID": lineage.parent_id or f"raven:{os.getpid()}",
        "RAVEN_SPAWN_DEPTH": str(max(1, lineage.depth)),
    }
