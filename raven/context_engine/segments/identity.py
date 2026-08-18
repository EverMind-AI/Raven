"""Segment 1 — ``# Raven`` identity / runtime. Host-owned."""

from __future__ import annotations

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
        playbook_listing: Callable[[], list[tuple[str, str]]] | None = None,
    ) -> None:
        self._workspace = workspace
        # A callable, not a snapshot: the loop wires this before the playbook
        # runtime exists, and the library can change between turns.
        self._playbook_listing = playbook_listing

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        text = render.identity_text(self._workspace)
        listing = self._playbook_listing() if self._playbook_listing is not None else []
        if listing:
            rows = "\n".join(f"- {pid}: {desc}" for pid, desc in listing)
            text += (
                "\n## Playbooks\n"
                "Stored multi-step procedures the user triggers by asking. When a request "
                "matches one, say so and offer to run it; disabled entries run only on an "
                "explicit ask.\n"
                f"{rows}\n"
            )
        return Segment(text=text)
