"""Agent core module."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.agent.context import ContextBuilder
    from raven.agent.loop import AgentLoop
    from raven.memory_engine.consolidate.consolidator import MemoryStore

__all__ = ["AgentLoop", "ContextBuilder", "MemoryStore"]

# Lazy re-exports (PEP 562): importing a ``raven.agent`` submodule must not
# eagerly construct ``AgentLoop`` -> litellm, which dominates CLI cold start.
_LAZY_EXPORTS = {
    "ContextBuilder": "raven.agent.context",
    "AgentLoop": "raven.agent.loop",
    "MemoryStore": "raven.memory_engine.consolidate.consolidator",
}


def __getattr__(name: str) -> object:
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)


def __dir__() -> list[str]:
    return sorted(__all__)
