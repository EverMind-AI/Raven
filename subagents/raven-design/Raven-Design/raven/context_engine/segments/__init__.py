"""System-prompt segment builders.

Each module here defines one :class:`SegmentBuilder` (seg1–5 plus the
Curator). They all share the same interface and are assembled uniformly
by :class:`ContextAssembler`. ``render.py`` holds the shared low-level
rendering helpers (formerly ``ContextBuilder`` methods).
"""

from raven.context_engine.segments.active_skills import ActiveSkillsSegmentBuilder
from raven.context_engine.segments.bootstrap import BootstrapSegmentBuilder
from raven.context_engine.segments.identity import IdentitySegmentBuilder
from raven.context_engine.segments.memory import MemorySegmentBuilder
from raven.context_engine.segments.skills import SkillsSegmentBuilder

__all__ = [
    "ActiveSkillsSegmentBuilder",
    "BootstrapSegmentBuilder",
    "IdentitySegmentBuilder",
    "MemorySegmentBuilder",
    "SkillsSegmentBuilder",
]
