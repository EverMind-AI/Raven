"""The memory paper: one remembered item and the backend that serves it.

The definitions moved to :mod:`raven.contracts.memory` (the papers own the
shapes); this module re-exports them so existing import paths keep resolving.
"""

from raven.contracts.memory import (  # noqa: F401
    Memory,
    MemoryBackend,
)

__all__ = ["Memory", "MemoryBackend"]
