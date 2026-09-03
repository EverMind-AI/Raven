"""Fold flat wiring names into the bundles ``AgentLoop`` takes.

Test helpers that accept ``**kw`` and hand it to the loop use this so their
callers can keep naming one field (``_loop(ws, restrict_to_workspace=True)``)
while the loop itself only ever sees bundles. The loop's own top-level
keywords pass through; a name that is neither is the typo it used to be.
"""

from __future__ import annotations

from typing import Any

from raven.agent.loop.bundles import (
    FIELD_OWNER,
    EngineWiring,
    HostWiring,
    SubagentWiring,
    ToolWiring,
    TurnPolicy,
)

_CLASSES = {
    "tools": ToolWiring,
    "subagents": SubagentWiring,
    "engine": EngineWiring,
    "policy": TurnPolicy,
    "host": HostWiring,
}
_TOP_LEVEL = {
    "provider",
    "workspace",
    "model",
    "session_manager",
    "provider_pool",
    "router",
    "sandbox_config",
    "mcp_servers",
}


def wire(**fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    grouped: dict[str, dict[str, Any]] = {}
    for name, value in fields.items():
        if name in _CLASSES or name in _TOP_LEVEL:
            out[name] = value
            continue
        owner = FIELD_OWNER.get(name)
        if owner is None:
            raise TypeError(f"AgentLoop got an unexpected keyword argument {name!r}")
        grouped.setdefault(owner, {})[name] = value
    for owner, values in grouped.items():
        bundle = out.get(owner) or _CLASSES[owner]()
        for name, value in values.items():
            setattr(bundle, name, value)
        out[owner] = bundle
    return out
