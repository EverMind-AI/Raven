"""ContextEngine ABC — contract for the context engine.

This package (``raven.context_engine``) hosts the single
:class:`ContextAssembler` (a concrete subclass of
:class:`ContextEngine`) plus its building blocks. AgentLoop holds
exactly one ``self.context_engine: ContextEngine`` reference, built via
:func:`raven.context_engine.build_context_engine`. The ABC lets an
alternative engine be slotted in; one implementation ships.

Naming note:
    Named ``context_engine`` (not ``context``) to mirror the L4
    ``memory_engine`` package and to avoid colliding with
    :mod:`raven.agent.context`, which hosts the lower-level
    :class:`ContextBuilder` utility that engines use as a building block.

Layering note:
    The data carriers ``AssembledContext`` and ``TokenBudget`` live in
    :mod:`raven.contracts.assembled`; they are shared value objects,
    not part of this contract's surface.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from raven.contracts.assembled import AssembledContext, TokenBudget

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider


# ---------------------------------------------------------------------------
# SegmentBuilder abstraction — the uniform contributor model
# ---------------------------------------------------------------------------
#
# Every part of the turn context is produced by a :class:`SegmentBuilder`.
# seg1–5 (identity / bootstrap / active-skills / memory / skills) and the
# history strategy are all SegmentBuilders — there is no separate "lane"
# category. :class:`ContextAssembler` runs them in two phases and routes their
# outputs into the system / user / history slots.


@dataclass(frozen=True)
class SessionRevision:
    """Identity and high-water mark of one append-only session-log view.

    ``log_id`` changes when a new logical log is started (``/new``), and
    ``generation`` changes before an operation can hand an existing ordinal to
    a different message (an undo). A ref into the log carries ``scope``, so a
    ref minted before either change names a scope that no longer exists -- a
    miss rather than a wrong answer.
    """

    log_id: str
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.log_id, str) or re.fullmatch(r"[0-9a-f]{32}", self.log_id) is None:
            raise ValueError("log_id must be a lowercase UUID hex string")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("generation must be non-negative")

    @property
    def scope(self) -> str:
        """The scope refs into this revision of the log carry."""
        return f"{self.log_id}@{self.generation}"


@dataclass(frozen=True)
class OverflowOutcome:
    """What an engine reclaimed from a prompt the provider refused as too long.

    Returned only when something was reclaimed: an engine with nothing to give
    back returns ``None`` from ``on_context_overflow`` instead, so the caller
    never re-sends a prompt of the same size on the strength of an outcome.
    """

    messages: list[dict[str, Any]]
    reclaimed_tokens: int


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
    can_see_images: bool = True
    describe_tool: str | None = None
    surface: str | None = None
    """Which front end the turn came from, when the connection declared one.

    Distinct from ``channel`` because the terminal and the served page share the
    ``tui`` channel on purpose (see ``Source.surface``), so the channel alone
    cannot say whether the reader is at a prompt or in a browser.
    """
    scent_text: str = ""
    """Pull-mode skill hint for this turn; rendered into the user envelope."""
    # Carried through from ``TurnContext``: what this dispatch's Charter asked
    # of the turn. Empty on every turn that carried none, which is the string
    # every reader already appends to no effect. Appended last, like the pair on
    # ``TurnContext``: this is the object every out-of-tree ``SegmentBuilder``
    # is handed, and the dataclass takes positional arguments, so a field
    # inserted mid-list would rebind somebody's tenth argument rather than
    # failing loudly.
    task_brief: str = ""
    task_done_when: str = ""
    # The log revision ``session_messages`` was read at, for a builder that
    # names messages in it (a ref) or keeps state keyed by it. None where the
    # caller has no session behind the turn. Appended last for the reason the
    # pair above is.
    session_revision: SessionRevision | None = None


@dataclass
class Segment:
    """The uniform product of a :class:`SegmentBuilder`.

    - ``text`` — the segment's contribution to the **system** slot
      (joined by ``order``), or to the turn scratch at the end of the user
      message when the builder is not ``stable`` and the scratch is on;
      ``""`` means "no segment this turn".
    - ``history`` — the **history** slot contribution; only the history
      strategy's builder sets this (``None`` for every other builder).
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

    ``stable`` says this segment's text does not depend on what the user just
    said, so it reads the same on the next turn as on this one.

    Per conversation, not per process. The identity segment interpolates the
    turn's bound workdir and the resolved model id, so two chats in one gateway
    process hold two different heads and two cache entries -- which is correct,
    they are different prompts. Read as "stable process-wide" this flag looks
    wrong; it is not, and nothing a segment could declare would collapse two
    genuinely different prompts into one cached prefix. The assembler
    uses it to tell the provider how much of the system message may carry a
    prompt-cache breakpoint of its own; only the unbroken run of stable builders
    at the *start* counts, because a cache key covers everything in front of it,
    so one volatile segment ends the run for every stable segment behind it.
    The default ``False`` is the safe answer: a segment wrongly called stable
    would have its neighbours cached under a key that changes anyway, which
    costs the write and returns nothing. With ``context.cache.turnScratch`` on,
    a phase-A segment that declares ``False`` leaves the system prompt for the
    end of the turn's user message.
    """

    name: str
    order: int
    needs_prefix: bool
    stable: bool

    async def build(self, ctx: AssemblyContext) -> "Segment | None":
        """Return this turn's :class:`Segment`, or ``None`` to contribute nothing."""
        ...


class ContextEngine(ABC):
    """Decides which messages reach the main agent's LLM each turn.

    One implementation ships:
    :class:`ContextAssembler <raven.context_engine.assembler.ContextAssembler>`
    — assembles a flat list of :class:`SegmentBuilder` into the turn's
    messages. Phase A runs seg1–5 concurrently (identity / bootstrap /
    active-skills / memory+recall / router-skills); phase B runs the history
    strategy ``context.history.strategy`` selects, which fills ``*history``.
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

    @property
    def mints_source_refs(self) -> bool:
        """If True, this engine puts ``[[ref ...]]`` tags where the model can
        read them, and the loop registers ``context_retrieve`` so they can be
        cashed in.

        Defaults False because the tool is only honest beside an engine that
        mints them: registered next to one that does not, it is a capability
        the model will reach for and always be told "found nothing".
        """
        return False

    def set_provider(self, provider: "LLMProvider", model: str) -> None:
        """Adopt the provider a live ``/model`` switch just built.

        Segments that call an LLM hold the provider handed to them at
        construction; without this they keep calling the old one for the
        rest of the process. Concrete rather than abstract so a future
        implementation with no LLM-backed segment is not forced to write an
        empty override; ``ContextAssembler`` is the only one today and does
        override it.
        """

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
        the Memory role based on :attr:`owns_compaction`.
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

    def note_request(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        """Hear about a request the loop actually sent for ``session_key``.

        ``messages`` is the payload as it went out, ``model`` the id it went
        out under and ``tools`` the tool list it carried (``None`` when the
        caller does not say). A history strategy that summarises by replaying
        the previous request -- so the summarising call can reuse its cached
        prefix -- keeps the last one here. Default is no-op.
        """
        return None

    async def on_context_overflow(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
        *,
        attempt: int,
        window: int,
    ) -> OverflowOutcome | None:
        """Reclaim room in a prompt the provider just refused as too long.

        ``messages`` is the request that was refused, ``attempt`` counts the
        refusals this turn from 1. ``None`` means the engine has nothing to say
        about overflow and the caller's own shrink runs, which is the default
        for every engine that never asked to own its history.
        """
        return None

    def set_context_window(self, tokens: int) -> None:
        """Follow a ``/model`` switch: re-budget whichever builders sized
        themselves against the window at construction. Default is no-op so
        an engine with no such builder need not override it.
        """
        return None


@dataclass
class TurnContext:
    """Per-turn inputs needed to build the main agent context."""

    current_message: str
    media: list[str] | None = None
    channel: str | None = None
    chat_id: str | None = None
    surface: str | None = None
    selected_skills: list[Any] | None = None
    # Whether this turn's model can see a picture. Decided by the loop (it owns
    # the provider and the model id) and carried here because the message is
    # built down in render, which knows neither. Defaults True so a caller that
    # does not set it keeps the old inline-everything behavior.
    can_see_images: bool = True
    # Name of a registered tool that can read an attachment the model cannot,
    # or None when none is (it comes from an optional plugin). Naming a tool the
    # model does not have reads as an instruction it cannot follow.
    describe_tool: str | None = None
    # What a dispatch's Charter asked of this turn, if it carried one. Filled
    # by Memory from the bound charter rather than read where it is rendered:
    # the identity segment owns how the text looks, Memory owns whether this
    # turn carries it at all.
    task_brief: str = ""
    task_done_when: str = ""
    # The revision the loop read the session at; carried through to
    # ``AssemblyContext.session_revision``.
    session_revision: SessionRevision | None = None


__all__ = [
    "AssembledPrefix",
    "AssemblyContext",
    "ContextEngine",
    "OverflowOutcome",
    "Segment",
    "SegmentBuilder",
    "SessionRevision",
    "TurnContext",
]


__tier__ = "contract"
