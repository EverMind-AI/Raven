"""Segment 1 — ``# Raven`` identity / runtime. Host-owned."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render

if TYPE_CHECKING:
    from raven.memory_engine.backend import MemoryBackend


class IdentitySegmentBuilder:
    name = "identity"
    order = 1
    needs_prefix = False

    def __init__(self, workspace: Path, backend: "MemoryBackend | None" = None) -> None:
        self._workspace = workspace
        self._backend = backend

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        return Segment(text=render.identity_text(self._workspace, has_memory_backend=self._backend is not None))
