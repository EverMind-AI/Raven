"""Memory subsystems for the agent host.

The shapes a memory backend implements (``Memory``, ``MemoryBackend``) and the
assembled-context carriers (``AssembledContext``, ``TokenBudget``) are papers
in :mod:`raven.contracts`; this package holds the machinery around them:

- ``contract_test.py``  -- the base test class a backend author inherits to
  prove the backend satisfies the host's expectations.
- ``consolidate/``      -- ``MemoryStore`` (MEMORY.md / HISTORY.md under a
  portable lock) and ``MemoryConsolidator`` (token-driven compaction);
  host-owned, not a plugin concern.
- ``skills/``, ``skill_local/``, ``skill_forge/`` -- the local skill pool, its
  watcher and catalog, and the forge that evolves skills from traces.
- ``store_pipeline.py`` -- the write path from a finished turn into the store.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.memory_engine.contract_test import (
        LifecycleContractTests,
        MemoryBackendContractTests,
    )

__all__ = [
    "LifecycleContractTests",
    "MemoryBackendContractTests",
]


# The contract-test base classes live in ``contract_test``, which imports
# ``pytest`` (a dev-only dependency) at module top level. Importing them
# eagerly here would pull pytest into every ``import raven.memory_engine`` —
# breaking any production install without pytest (e.g. a packaged `raven tui`),
# with ``ModuleNotFoundError: No module named 'pytest'``. Expose them lazily
# (PEP 562) so they resolve only when actually accessed — which happens under
# pytest in the test suite, where the import succeeds.
def __getattr__(name: str):
    if name in ("LifecycleContractTests", "MemoryBackendContractTests"):
        from raven.memory_engine import contract_test

        return getattr(contract_test, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
