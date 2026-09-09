"""What one config reconcile did, for the callers that must act on it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApplyReport:
    """The outcome of reconciling live MCP connections with config.

    ``reloaded`` counts records touched -- attached, detached, or reconnected.
    ``tools_changed`` says whether the model-facing surface moved, which is the
    half a caller has to act on: the tool list is the first thing in the cached
    prompt prefix, so moving it costs the conversation its cache.
    """

    reloaded: int = 0
    tools_changed: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The wire shape. ``reload.mcp`` answers with exactly these keys."""
        return {"reloaded": self.reloaded, "tools_changed": self.tools_changed}


__all__ = ["ApplyReport"]
