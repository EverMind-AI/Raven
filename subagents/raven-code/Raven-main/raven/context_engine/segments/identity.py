"""Segment 1 — ``# Raven`` identity / runtime. Host-owned."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render


class IdentitySegmentBuilder:
    name = "identity"
    order = 1
    needs_prefix = False

    def __init__(
        self,
        workspace: Path,
        model: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._workspace = workspace
        self._model = model
        self._now_fn = now_fn

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        return Segment(text=render.identity_text(self._workspace, self._model, now_fn=self._now_fn))
