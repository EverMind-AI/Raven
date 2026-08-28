"""The context paper: segments, builders, and the engine surface.

The definitions moved to :mod:`raven.contracts.context` (the papers own the
shapes); this module re-exports them so existing import paths keep resolving.
"""

from raven.contracts.context import (  # noqa: F401
    AssembledPrefix,
    AssemblyContext,
    ContextEngine,
    Segment,
    SegmentBuilder,
)
from raven.memory_engine.base import AssembledContext, TokenBudget  # noqa: F401

__all__ = ["AssembledContext", "TokenBudget", "AssembledPrefix", "AssemblyContext", "ContextEngine", "Segment", "SegmentBuilder"]
