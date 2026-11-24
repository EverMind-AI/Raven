"""Segment 2 — bootstrap files (soul / agent / TOOLS). Host-owned."""

from __future__ import annotations

from pathlib import Path

from raven.context_engine.segments import render
from raven.contracts.context import AssemblyContext, Segment


class BootstrapSegmentBuilder:
    name = "bootstrap"
    order = 2
    needs_prefix = False
    # The bootstrap files as they are on disk, read the same way each turn.
    stable = True

    def __init__(self, workspace: Path, bootstrap_files: list[str] | None = None) -> None:
        self._workspace = workspace
        # soul.md opens the identity segment instead (``IdentitySegmentBuilder``).
        self._bootstrap_files = [f for f in (bootstrap_files or render.BOOTSTRAP_FILES) if Path(f).name != "soul.md"]

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        text = render.load_bootstrap_files(self._workspace, self._bootstrap_files)
        return Segment(text=text) if text else None
