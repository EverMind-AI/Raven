"""Context Management engine.

One engine — :class:`ContextAssembler` — assembled by
:func:`build_context_engine` from a flat list of :class:`SegmentBuilder`
(seg1–5 + the Curator). The historical ``legacy`` / ``curator`` /
``default`` split has been collapsed.
"""

from raven.context_engine.assembler import ContextAssembler
from raven.context_engine.base import (
    AssembledPrefix,
    AssemblyContext,
    ContextEngine,
    Segment,
    SegmentBuilder,
)
from raven.context_engine.curator import TurnContext
from raven.context_engine.factory import build_context_engine
from raven.context_engine.history_trimmer import HistoryTrimmer

__all__ = [
    "AssembledPrefix",
    "AssemblyContext",
    "ContextAssembler",
    "ContextEngine",
    "HistoryTrimmer",
    "Segment",
    "SegmentBuilder",
    "TurnContext",
    "build_context_engine",
]
