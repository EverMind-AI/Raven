"""ContextEngine ABC — contract for the context engine.

This package (``raven.context_engine``) hosts the single
:class:`ContextAssembler` (a concrete subclass of
:class:`ContextEngine`) plus its building blocks. AgentLoop holds
exactly one ``self.context_engine: ContextEngine`` reference, built via
:func:`raven.context_engine.build_context_engine`. The ABC is kept so
alternative engines can be slotted in for experiments, but there is one
shipping implementation.

Naming note:
    Named ``context_engine`` (not ``context``) to mirror the L4
    ``memory_engine`` package and to avoid colliding with
    :mod:`raven.agent.context`, which hosts the lower-level
    :class:`ContextBuilder` utility that engines use as a building block.

Layering note:
    The data carriers ``AssembledContext`` and ``TokenBudget`` live in
    :mod:`raven.memory_engine.base` (they were placed there before
    this ABC landed). The two engines (``MemoryEngine``,
    ``ContextEngine``) are peer L4 abstractions; the dataclasses are
    shared value objects, not part of either's contract surface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from raven.memory_engine.base import AssembledContext, TokenBudget

if TYPE_CHECKING:
    # Avoid runtime import — ``curator`` imports back from this module
    # for ``ContextEngine``, so referencing ``TurnContext`` only in type
    # hints keeps the loop unbroken.
    from raven.context_engine.curator import TurnContext


# ---------------------------------------------------------------------------
# SegmentBuilder abstraction — the uniform contributor model
# ---------------------------------------------------------------------------
#
# Every part of the turn context is produced by a :class:`SegmentBuilder`.
# seg1–5 (identity / bootstrap / memory / active-skills / skills) and the
# Curator are all SegmentBuilders — there is no separate "lane" category.
# :class:`ContextAssembler` runs them in two phases and routes their
# outputs into the system / history slots.


@dataclass(frozen=True)
class AssembledPrefix:
    """Phase-A output handed to phase-B builders (the Curator).

    A phase-B builder needs the already-assembled system prefix + the
    user message + tool defs so it can size the *fixed* prompt overhead
    and budget ``*history`` exactly.
    """

    system_prefix: str
    user_message: dict[str, Any]
    tool_defs: list[dict[str, Any]]


@dataclass(frozen=True)
class AssemblyContext:
    """Per-turn, read-only inputs shared by every :class:`SegmentBuilder`.

    ``prefix`` is ``None`` during phase A (independent builders) and is
    populated by :class:`ContextAssembler` before phase-B builders run.
    Phase-A builders ignore it; phase-B builders require it.
    """

    session_key: str
    current_message: str
    media: list[str] | None
    channel: str | None
    chat_id: str | None
    session_messages: list[dict[str, Any]]
    budget: TokenBudget
    prefix: AssembledPrefix | None = None


@dataclass
class Segment:
    """The uniform product of a :class:`SegmentBuilder`.

    - ``text`` — the segment's contribution to the **system** slot
      (joined by ``order``); ``""`` means "no segment this turn".
    - ``history`` — the **history** slot contribution; only the Curator
      sets this (``None`` for every other builder).
    - ``meta`` — merged into ``AssembledContext.metadata`` (e.g.
      ``injected_skill_ids`` / ``memory_hits`` / ``path``).
    """

    text: str = ""
    history: list[dict[str, Any]] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SegmentBuilder(Protocol):
    """One context contributor. seg1–5 and the Curator all implement it.

    ``order`` fixes the segment's position in the system prompt.
    ``needs_prefix`` routes the builder to phase B (it reads
    ``ctx.prefix``); the default ``False`` keeps a builder in the
    phase-A parallel batch.
    """

    name: str
    order: int
    needs_prefix: bool

    async def build(self, ctx: AssemblyContext) -> "Segment | None":
        """Return this turn's :class:`Segment`, or ``None`` to contribute nothing."""
        ...


class ContextEngine(ABC):
    """Decides which messages reach the main agent's LLM each turn.

    One implementation ships:
    :class:`ContextAssembler <raven.context_engine.assembler.ContextAssembler>`
    — assembles a flat list of :class:`SegmentBuilder` into the turn's
    messages. Phase A runs seg1–5 concurrently (identity / bootstrap /
    memory+recall / active-skills / router-skills); phase B runs the
    Curator (``# Curator Working State`` + budget-trimmed ``*history``).
    ``owns_compaction=True``; AgentLoop defers compaction to
    :meth:`after_turn`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier used in logs / metadata (``"unified"``)."""

    @property
    @abstractmethod
    def owns_compaction(self) -> bool:
        """If True, AgentLoop skips ``MemoryEngine.maybe_consolidate`` and
        lets the engine manage history compaction itself (Curator archives
        messages out-of-band)."""

    @abstractmethod
    async def assemble(
        self,
        session_key: str,
        session_messages: list[dict[str, Any]],
        budget: TokenBudget,
        *,
        turn: "TurnContext",
    ) -> AssembledContext:
        """Build the exact message list passed to the main agent's LLM.

        ``session_messages`` is what the engine should consider as candidate
        history. Whether it's ``session.messages`` (full append-only log) or
        ``session.get_history()`` (post-consolidation slice) is decided by
        AgentLoop based on :attr:`owns_compaction`.
        """

    async def after_turn(
        self,
        session_key: str,
        outcome: dict[str, Any],
    ) -> None:
        """Optional post-turn hook. Curator updates its manifest / archives
        here; Legacy ignores it. Default is no-op so future engines can
        opt in incrementally.
        """
        return None


__all__ = [
    "AssembledPrefix",
    "AssemblyContext",
    "ContextEngine",
    "Segment",
    "SegmentBuilder",
]
