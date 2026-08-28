"""Segment 2 — bootstrap rules files (AGENTS.md / CLAUDE.md / CONTEXT.md).

Layered by ``render.load_bootstrap_files``: machine-level rules, then the
agent home this builder was constructed on, then the per-turn working
directory chain (git root down to the bound workdir)."""

from __future__ import annotations

from pathlib import Path

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render


class BootstrapSegmentBuilder:
    name = "bootstrap"
    order = 2
    needs_prefix = False

    def __init__(self, workspace: Path, bootstrap_files: list[str] | None = None) -> None:
        self._workspace = workspace
        self._bootstrap_files = bootstrap_files

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        # Always the agent home: load_bootstrap_files reads the bound working
        # directory chain itself. Substituting workdir.current() here made
        # layers 2 and 3 the same directory, and the dedup then dropped the
        # agent home rules entirely.
        text = render.load_bootstrap_files(self._workspace, self._bootstrap_files)
        return Segment(text=text) if text else None
