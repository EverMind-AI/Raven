"""MemoryBackend Protocol — the single contract every memory plugin implements.

MB-1 introduction. This is the **new** seam between AgentLoop and the
memory subsystem, deliberately distinct from the older
:class:`MemoryEngine` ABC in :mod:`raven.memory_engine.base` so the
two can coexist while the codebase transitions.

Three design points to flag for plugin authors:

- ``recall`` names the track explicitly: it takes ``user_id`` XOR
  ``agent_id`` (exactly one set). Dual-track backends (EverOS) route
  the set field to the matching store (user episodes/profiles vs agent
  cases/skills); flat backends (mem0, MemOS) use ``user_id`` and return
  ``[]`` for the ``agent_id`` call. Ids are bare, backend-native
  strings — no ``"user:"`` / ``"agent:"`` prefix convention.

- ``Memory.metadata`` is the **escape hatch**. ``text`` and ``score``
  are normalized; everything backend-specific (categories, episode
  type, native id, source labels) goes in ``metadata``. The host's
  context assembler does **not** read ``metadata`` — only the
  pre-rendered ``text`` lands in the prompt. Skill-source adapters
  that re-emit Memory hits as ScoredSkill do read ``metadata`` for
  qualified-id construction.

- ``feedback`` is **allowed to be a no-op**. Most backends have no
  native concept of "skill confidence" or "execution signal"; the
  Protocol exposes the slot so EverOS-style backends can consume it
  without forcing every adapter to fake support.

Capabilities beyond those five live in their own single-method
Protocols (:class:`FlushableBackend`), never as extra members of
:class:`MemoryBackend` — see that class's note on why.

The Protocol is :func:`typing.runtime_checkable` so ``isinstance(x,
MemoryBackend)`` works in tests — at the cost of accepting any class
whose surface matches, including duck-typed mocks. That's the trade we
want: contract tests don't have to inherit from a base class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Data carrier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Memory:
    """One hit returned by :meth:`MemoryBackend.recall`.

    ``frozen=True`` so the host can hand a list of these around without
    worrying about adapter code mutating someone else's view.
    """

    text: str
    """Pre-rendered content the LLM sees verbatim in the prompt.

    Adapters are responsible for formatting (e.g. EverMem returns
    natural-sentence facts; mem0 returns category-tagged blobs). The
    host **never** post-processes ``text`` except to join multiple
    hits into a block."""

    score: float = 0.0
    """Relevance normalized to ``[0, 1]`` by the adapter. Used by
    :class:`SkillForgeRouter` for cross-source RRF when a Memory hit is
    re-emitted as a ScoredSkill. For plain ``# Recalled memory``
    injection, ``score`` is informational only."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Adapter-specific escape hatch. Examples by backend:

    - EverMem: ``{"id": ..., "episode_type": ..., "name": ...,
      "owner_type": "user" | "agent"}``
    - mem0: ``{"id": ..., "categories": [...], "memory_type": ...}``
    - MemOS: ``{"id": ..., "mem_cube_id": ..., "metadata": {...}}``
    - Letta: ``{"archival_memory_id": ...}``
    """


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryBackend(Protocol):
    """The single contract every memory plugin implements.

    Five methods, ordered by hot-path:

    1. :meth:`recall` — called by ``ContextEngine.assemble`` every turn
       (potentially twice: once for user-track memory with ``user_id``,
       once for agent-track skills with ``agent_id`` via
       :class:`EverosSkillSource`).
    2. :meth:`store` — called by AgentLoop after each turn to persist
       the conversation slice.
    3. :meth:`feedback` — called by AgentLoop's after-turn dispatcher
       when ``injected_skill_ids`` contains source-qualified entries
       belonging to this backend.
    4. :meth:`start` / :meth:`stop` — lifecycle, awaited by the host.
    """

    async def recall(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        top_k: int,
    ) -> list[Memory]:
        """Retrieve memories matching ``query`` for one track.

        Exactly one of ``user_id`` / ``agent_id`` is set (XOR) — the
        caller knows which track it wants at construction time, so the
        track is named explicitly rather than smuggled through a
        prefixed opaque string. Dual-track backends (EverOS) route the
        set field to the matching store; flat backends (mem0, MemOS)
        use ``user_id`` and return ``[]`` for the ``agent_id`` call.
        Passing neither or both is a caller bug — return ``[]``.

        Empty result is a valid response (no hits); raise on transport
        errors, auth failures, etc. The host will fall back to other
        sources via ``SkillForgeRouter._safe_search``.
        """
        ...

    async def store(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Persist a session slice.

        ``messages`` follows the AgentLoop ``{"role", "content", ...}``
        shape — the existing list-of-dicts form the codebase already
        produces, so adapters don't need a conversion step. Backends
        that want to chunk / deduplicate / extract are free to; the
        Protocol is fire-and-forget per call.

        Raises on transport / auth errors so AgentLoop can surface
        them; the host does **not** silently swallow store failures.
        """
        ...

    async def feedback(self, signals: dict[str, Any]) -> None:
        """Consume a free-form signal dict (e.g. injected/used skill ids).

        A no-op implementation is fully valid and idiomatic — only
        EverOS-style backends with confidence-based skills do useful
        work here. Adapters that don't care should still accept the
        call without raising.
        """
        ...

    async def start(self) -> None:
        """One-time / idempotent initialization (open connections,
        warm caches, run migrations). The host awaits this exactly
        once during agent boot; failures abort startup."""
        ...

    async def stop(self) -> None:
        """One-time / idempotent teardown. Adapters should make this
        safe to call after a failed ``start`` (so partial-init state
        cleans up)."""
        ...


@runtime_checkable
class FlushableBackend(Protocol):
    """Optional capability: promote what :meth:`MemoryBackend.store` buffered.

    A separate Protocol rather than a sixth :class:`MemoryBackend`
    method. Both are ``runtime_checkable``, which checks *every* member
    is present, so folding ``flush`` into the main contract would strip
    protocol identity from every adapter that doesn't implement it
    (pinned by ``tests/test_memory_backend_protocol.py``). An
    ``isinstance`` against this one-method Protocol gives the host a
    typed capability check without breaking those adapters.
    """

    async def flush(self, session_id: str) -> None:
        """Promote ``session_id``'s buffered writes to derived memory.

        Only meaningful for backends that decouple capture from
        extraction (EverOS's ``defer_extraction``), where ``store``
        appends to a buffer and nothing derives episodes / cases /
        skills from it until asked. Backends that extract eagerly
        satisfy this by not implementing it at all.

        Callers invoke it at a task boundary, never per turn — deferring
        exists to keep extraction LLMs out of the turn. Implementations
        own their own timeout and are expected to absorb transport
        failures rather than raise: the captured turns stay durable, so
        a failed promotion costs derived memory, not data.
        """
        ...


class MemoryServiceUnavailableError(RuntimeError):
    """A backend's service was required at start and is not usable.

    Host-side rather than plugin-side so a caller can decide what an absent
    memory service means without importing a plugin: an interactive session
    degrades and continues, while a scripted run that exists to *produce*
    memory should not spend an hour writing into nothing. Raised from
    ``start()`` only when the operator opted in — the default everywhere
    stays fail-open.
    """


class MemoryApiVersionError(MemoryServiceUnavailableError):
    """The service refused the API version this backend speaks.

    A *subclass*, narrowing rather than replacing: for this account the service
    genuinely is unusable, so every caller that already handles an unusable
    service handles this correctly by default. Being a bare ``RuntimeError``
    instead was a real bug -- ``raven agent``'s start path catches
    ``MemoryServiceUnavailableError`` to exit cleanly and then has a broad
    ``except Exception`` that logs and continues, so ``require_service=true``
    printed a traceback and ran the whole job anyway. That is the exact outcome
    the switch exists to prevent.

    The distinct type is still worth having: what is wrong is the *account's*
    provisioning, not the service's health, and no amount of retrying, waiting
    or failing open improves it. A caller that wants to tell the two apart can;
    one that does not, does not have to.

    Host-side for the same reason as its parent: a caller decides what it means
    without importing a plugin.
    """


__all__ = [
    "FlushableBackend",
    "Memory",
    "MemoryApiVersionError",
    "MemoryBackend",
    "MemoryServiceUnavailableError",
]
