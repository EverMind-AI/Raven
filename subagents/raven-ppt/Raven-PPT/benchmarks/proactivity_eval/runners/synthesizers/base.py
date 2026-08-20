"""Protocol + output type for pluggable context synthesizers.

A synthesizer converts raw activity events (ProactiveAgent-style obs) into a
PlannerContext-shaped payload (user_profile + routines + memory_md), simulating
what SentinelRunner's upstream components (RoutineLearner, MemoryManager) would
produce in production.

Design invariants:
- Deterministic: same input → same output. Non-determinism introduces a
  confound between 'planner quality' and 'synthesizer noise' in benchmark
  results. LLM-backed synthesizers (future) must document sampling params
  and report scores over multiple seeds.
- Never emit `Routine(status="active")` or `user_confirmed=True`. Those are
  reserved for routines that have been verified by actual user interaction.
  Synthesizers simulate the 'candidate' pre-confirmation stage only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from raven.proactive_engine.sentinel.types import Routine


@dataclass
class SynthesizedContext:
    """Filled into PlannerContext's memory/routines/user_profile fields."""

    user_profile: str = ""
    routines: list[Routine] = field(default_factory=list)
    memory_md: str = ""


@runtime_checkable
class ContextSynthesizer(Protocol):
    """Any object with a stable `name` and `synthesize(obs) → SynthesizedContext`."""

    name: str

    def synthesize(self, obs: list[dict]) -> SynthesizedContext: ...


__all__ = ["ContextSynthesizer", "SynthesizedContext"]
