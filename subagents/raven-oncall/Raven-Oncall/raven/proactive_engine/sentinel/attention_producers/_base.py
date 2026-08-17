"""AttentionProducer ABC — one section per subclass."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar

# Shared weekday tag used by producers rendering date / day-of-week
# bullets (active_threads, currently_focused, predicted_3d, ...).
# Centralized here so adding a new producer doesn't replicate the tuple.
WEEKDAY: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class AttentionProducer(ABC):
    """Contract that every attention.md section writer implements.

    Lifecycle per :class:`AttentionUpdater` tick:

    1. ``should_run(now)`` — cheap gate. Tick-cadence producers return
       True every call; cooldown-bearing producers compare ``now``
       against their last-run cookie. Skipping here saves the
       ``compute_body`` work, which can be expensive (LLM call,
       full-file scan).
    2. ``compute_body(now)`` — produces the section's body markdown
       (NO leading H2 header — the orchestrator wraps the body via
       :func:`memory_engine.consolidate.attention.upsert_section`). Async
       so LLM-backed producers can ``await`` without blocking the
       attention.md lock. Returns empty string when the producer has
       nothing to say this tick — caller then skips the section.

    Subclasses MUST set ``SECTION_HEADER`` to the exact H2 string
    (with leading ``## ``); ``upsert_section`` keys off it.

    Errors raised inside ``compute_body`` bubble up to the orchestrator,
    which logs and skips the affected section. Producers SHOULD return
    empty string for "no data" rather than raising.
    """

    SECTION_HEADER: ClassVar[str]

    def should_run(self, now: datetime) -> bool:
        """Default: run on every tick. Override for cooldown logic."""
        return True

    @abstractmethod
    async def compute_body(self, now: datetime) -> str:
        """Return the section body markdown, or empty string for no-op."""


__all__ = ["AttentionProducer", "WEEKDAY"]
