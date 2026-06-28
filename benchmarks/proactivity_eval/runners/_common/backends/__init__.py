"""Backend registry. One factory per system; ``get_backend(name, mode)``
picks the right implementation based on the global + per-agent yaml configs.
"""

from __future__ import annotations

from typing import Any

from ..backend import AgentBackend
from ..config import get_config

_AGENT_DEFAULT_MODES: dict[str, str | None] = {
    "raven": "agent",   # "planner" | "agent" | "sentinel"
    "hermes": None,
    "openclaw": None,
}


def get_backend(name: str, mode: str | None = None,
                overrides: dict[str, Any] | None = None) -> AgentBackend:
    """Instantiate a backend. ``overrides`` lets a CLI flag beat the yaml default."""
    name = name.lower()
    overrides = overrides or {}

    if name == "raven":
        from .raven import make_raven_backend
        mode = (mode or _AGENT_DEFAULT_MODES["raven"] or "agent").lower()
        return make_raven_backend(mode, overrides=overrides)

    if name == "hermes":
        from .hermes import HermesBackend
        return HermesBackend(overrides=overrides)

    if name == "openclaw":
        from .openclaw import OpenClawBackend
        return OpenClawBackend(overrides=overrides)

    raise ValueError(f"Unknown agent '{name}'. Registered: raven, hermes, openclaw")


__all__ = ["AgentBackend", "get_backend", "get_config"]
