"""Segment 1 — ``# Raven`` identity / runtime. Host-owned."""

from __future__ import annotations

from pathlib import Path

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render


class IdentitySegmentBuilder:
    name = "identity"
    order = 1
    needs_prefix = False

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        # The playbook library used to be listed here as well as in the tool.
        # Two resident surfaces describing the same thing is how they drift, and
        # this one drifted first: it told the model that playbooks are "triggered
        # by asking" and that a disabled one runs on an explicit ask, both of
        # which described the passive matcher that no longer exists. The listing
        # lives in ``load_playbook``'s description, where it can also carry the
        # parameter table and be narrowed per turn -- and where the model is
        # standing when it has to choose.
        return Segment(text=render.identity_text(self._workspace))
