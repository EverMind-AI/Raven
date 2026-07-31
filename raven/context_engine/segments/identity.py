"""Segment 1 — ``# Raven`` identity / runtime. Host-owned."""

from __future__ import annotations

from pathlib import Path

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render


class IdentitySegmentBuilder:
    name = "identity"
    order = 1
    needs_prefix = False

    def __init__(self, workspace: Path, profile: str = "assistant") -> None:
        self._workspace = workspace
        self._profile = profile

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        return Segment(text=render.identity_text(self._workspace, self._profile))
