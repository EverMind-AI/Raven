"""Two-layer long-term memory: a profile and the episodes behind it.

``MemoryStore`` reads and writes ``user.md`` (the profile) and ``episodes.md``
(the episode log) under a portable file lock; ``MemoryConsolidator`` is the
token-pressure path that annotates evicted conversation into episodes and
folds them back into the profile.
"""

from raven.memory_engine.consolidate.consolidator import (
    MemoryConsolidator,
    MemoryStore,
)

__all__ = ["MemoryStore", "MemoryConsolidator"]
