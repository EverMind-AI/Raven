"""Context Management engine.

One engine — :class:`ContextAssembler` — assembled by
:func:`build_context_engine` from a flat list of :class:`SegmentBuilder`
(seg1–5 + the Curator).
"""

from raven.context_engine.assembler import ContextAssembler
from raven.context_engine.factory import build_context_engine
from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.contracts.context import (
    AssembledPrefix,
    AssemblyContext,
    ContextEngine,
    Segment,
    SegmentBuilder,
    TurnContext,
)

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
