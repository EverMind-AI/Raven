"""SkillForgeRouter data types — :class:`RouterHit` + :class:`SkillSource` Protocol.

Two design points worth highlighting:

- :class:`RouterHit` is **self-contained**. Unlike the legacy
  ``ScoredSkill`` in :mod:`raven.memory_engine.skill_local.types` (which
  only carried name + score and forced consumers to re-fetch the body
  from SkillRegistry), :class:`RouterHit` ships the rendered ``content``
  so :class:`ContextBuilder` can write it straight into the prompt
  without a second round-trip to the source.

- :class:`SkillSource` is **internal**. Per the design decision
  recorded in the change plan, sources are hardcoded (Local + Mass +
  Everos) rather than exposed as a plugin contribution point.
  ``@runtime_checkable`` lets tests assert duck-typed conformance
  without inheritance; the cost is accepting any object whose surface
  matches, which is fine because the registration set is closed.

The legacy ``ScoredSkill`` in ``skill/types.py`` stays untouched for
now — :class:`LocalPool` and :class:`SkillService` continue to use it.
The cleanup PR collapses the two once SkillService is removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class RouterHit:
    """One ranked skill returned by a :class:`SkillSource`.

    Carries everything :class:`ContextBuilder` needs to render the
    skill into the system prompt — no further registry lookup happens
    on the consumer side.

    The ``qualified_id`` format is ``<source>/<native_id>``; the prefix
    is how the after-turn feedback dispatcher routes
    ``injected_skill_ids`` back to the right backend.
    """

    qualified_id: str
    """Globally-unique id with source prefix. Examples:
    ``"local/git-resolver"`` / ``"mass/curated-xyz"`` /
    ``"everos/abc123"``. The slash split is unambiguous because
    source names are simple identifiers (no embedded slashes)."""

    name: str
    """Skill display name. Used as the cross-source dedup key inside
    :func:`rrf_merge_weighted` (lands in SR-2): two hits with the same
    ``name`` are collapsed to one with summed RRF score, regardless of
    which source they came from."""

    content: str
    """Pre-rendered SKILL.md body (frontmatter already stripped) that
    will land in the prompt's ``# Skills`` block. Empty string means
    the source has metadata but no body — consumers skip such hits
    from the body-join, while still letting the name appear in
    summaries."""

    score: float
    """Source-internal relevance. Each source normalizes to its own
    scale (BM25 raw / cosine sim / EverMem score); RRF doesn't compare
    them directly so absolute values across sources are not meaningful.
    Used only for "pick the best representative when two hits collide
    on ``name``" — :func:`rrf_merge_weighted` keeps the one with the
    higher ``score``."""

    meta: dict[str, Any] = field(default_factory=dict)
    """Source-specific escape hatch.

    SR-2 stuffs ``rrf_score`` and ``contributing_sources`` here for
    telemetry; sources stuff their physical-origin label, native id,
    confidence, ``always`` flag, etc.
    """


@runtime_checkable
class SkillSource(Protocol):
    """One pool of skills the router can ask. Internal Protocol — the
    set of sources is fixed at compile time (Local + Mass + Everos);
    third parties extend retrieval by contributing a
    :class:`MemoryBackend` whose ``agent``-track ``recall`` hits get
    re-emitted by :class:`EverosSkillSource`.

    Why ``weight`` is a class attribute, not a method param: weights
    are router-wide policy, not per-call, so they belong with the
    source's identity. Tests and config tweaks set them once at
    construction.
    """

    name: str
    """Stable source identifier (``"local"`` / ``"mass"`` / ``"everos"``).
    Used as the prefix in :attr:`RouterHit.qualified_id` and as the
    feedback-dispatch routing key."""

    weight: float
    """RRF source weight. Higher = source contributes more rank mass
    when the same skill surfaces from multiple sources."""

    async def search(
        self,
        query: str,
        history: list[dict[str, Any]],
        k: int,
    ) -> list[RouterHit]:
        """Return at most ``k`` :class:`RouterHit` records ranked best-first.

        ``history`` is the session-level message list — sources free to
        ignore it (Local does) or use it as context for a smarter
        ranker (Everos can feed it into ``backend.recall`` if the
        backend supports re-ranking by conversation context).

        Empty list is a valid response — the router's
        ``_safe_search`` wrapper additionally turns exceptions into
        empty lists so a single source's failure doesn't poison the
        whole assembly.
        """
        ...


__all__ = ["RouterHit", "SkillSource"]
