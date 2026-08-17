"""Memory subsystems for the agent host.

Post-Phase-B layout:

- ``backend.py``        — :class:`MemoryBackend` Protocol + :class:`Memory`
  (the public plugin contract; what the bundled everos backend and any
  third-party plugin implements).
- ``contract_test.py``  — base test class plugin authors inherit to
  verify their backend satisfies the host's expectations.
- ``base.py``           — shared data carriers (``AssembledContext``,
  ``TokenBudget``) used by :class:`ContextEngine`. The L4
  ``MemoryEngine`` ABC + ``DefaultMemoryEngine`` facade that once lived
  here were deleted — the indirection leaked too much surface for the
  plugin contract.
- ``consolidate/``      — :class:`MemoryStore` (MEMORY.md / HISTORY.md
  read/write under fcntl lock) + :class:`MemoryConsolidator`
  (token-driven compaction). Host-owned, not a plugin concern.
- ``skill/``            — local-pool primitive layer:
  ``SkillRegistry``, ``LocalPool``, the SKILL.md watcher and shared
  types. Mass-pool + ``Retrieval`` + ``Reranker`` + ``SqliteStore``
  were deleted in Phase B-2 (the remote :class:`MassSkillSource` HTTP
  client replaces them).
- ``skill_router/``     — ``SkillForgeRouter`` + 3 hardcoded sources
  (Local / Mass / Everos) plus :class:`LocalSkillCatalog`, the single
  owner of the local pool (rendering + feedback; absorbed the retired
  ``SkillService``).
"""

from typing import TYPE_CHECKING

from raven.memory_engine.backend import Memory, MemoryBackend
from raven.memory_engine.base import AssembledContext, TokenBudget

if TYPE_CHECKING:
    from raven.memory_engine.contract_test import (
        LifecycleContractTests,
        MemoryBackendContractTests,
    )

__all__ = [
    "AssembledContext",
    "LifecycleContractTests",
    "Memory",
    "MemoryBackend",
    "MemoryBackendContractTests",
    "TokenBudget",
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
