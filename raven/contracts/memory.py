"""MemoryBackend Protocol — the single contract every memory plugin implements.

The seam between AgentLoop and the memory subsystem, and the only memory
contract.

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

The Protocol is :func:`typing.runtime_checkable` so ``isinstance(x,
MemoryBackend)`` works in tests — at the cost of accepting any class
whose surface matches, including duck-typed mocks. That's the trade we
want: contract tests don't have to inherit from a base class.

Failure contract. A session host -- the agent loop, the TUI, the gateway,
``raven serve`` -- wraps every call into a backend and treats a raise as the
loss of that one call: ``recall`` counts as no hits, ``store`` as not landed,
``start`` as no long-term memory for this session, ``feedback`` and ``stop``
as logged and ignored. The import CLI (``raven import``) is the deliberate
exception: nothing wraps ``start`` or ``stop`` there, so a raise in either ends
the command, and a ``store`` that raises or returns ``False`` fails that one
source and leaves it unsubmitted for a retry rather than passing as not landed:
``run_import`` catches per source, and a ``False`` becomes the
``MemoryWriteDroppedError`` that ``_feed_session`` raises into that same
``except``. A bulk import that runs against nothing would consume the source
list while writing nothing.

A backend that can classify its own failures (a timeout is not a refused
connection) should catch and act on them, because the host cannot; what it
does not understand it may let propagate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

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


HealthStatus = Literal["ok", "degraded", "missing"]
"""How one thing a backend depends on stands: working, working worse, or a
fault. Only ``"missing"`` counts against the host's exit code."""


@dataclass(frozen=True)
class HealthCheck:
    """One line of :attr:`BackendHealth.checks`, rendered verbatim."""

    label: str
    """What was checked, in the backend's own vocabulary (a role name, a
    server, an address). The host prints it and does not interpret it."""

    status: HealthStatus
    """``"ok"`` when nothing is wrong with this one, ``"degraded"`` when it
    costs quality rather than function, ``"missing"`` when memory cannot work
    until someone fixes it."""

    hint: str | None = None
    """What a person should look at, or the value that answers the check (a
    path, an address). ``None`` when the label and status say it all."""


@dataclass(frozen=True)
class BackendHealth:
    """A backend's own account of itself, for the two hosts that ask.

    ``ready`` answers the importer's question; ``checks`` answers
    ``raven doctor``'s. They are separate because a backend can be
    diagnosable and not usable: a server that starts on demand has nothing
    wrong with it and still cannot take a write this second.
    """

    ready: bool
    """Whether a write issued now would land. The importer refuses to run on
    ``False``: one deliberate batch against nothing writes nothing while
    consuming the source list."""

    checks: list[HealthCheck]
    """What ``raven doctor`` prints, one line each, in this order. Empty is
    valid: a backend with nothing to report and nothing wrong."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryBackend(Protocol):
    """The single contract every memory plugin implements.

    Six methods, ordered by hot-path:

    1. :meth:`recall` — called by ``ContextEngine.assemble`` every turn
       (potentially twice: once for user-track memory with ``user_id``,
       once for agent-track skills with ``agent_id`` via
       :class:`BackendSkillSource`).
    2. :meth:`store` — called by AgentLoop after each turn to persist
       the conversation slice.
    3. :meth:`feedback` — called by AgentLoop's after-turn dispatcher
       when ``injected_skill_ids`` contains source-qualified entries
       belonging to this backend.
    4. :meth:`start` / :meth:`stop` — lifecycle, awaited by the host.
    5. :meth:`health` — asked by ``raven doctor`` and ``raven import``,
       off the turn path, before or after ``start``.
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

        Empty result is a valid response (no hits). A raise is tolerated by
        the host and costs this call its hits; prefer returning ``[]`` for
        failures the backend can recognize. The host also bounds the call
        with a per-turn wall clock (the floor under any backend, since this
        Protocol does not oblige one to have a timeout at all; the constant
        lives in ``raven/context_engine/segments/memory.py``) and abandons a
        slower call, so keep the backend's own timeouts stricter than that.
        """
        ...

    async def store(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Persist a session slice. Returns whether it landed.

        ``messages`` follows the AgentLoop ``{"role", "content", ...}``
        shape — the existing list-of-dicts form the codebase already
        produces, so adapters don't need a conversion step. Backends
        that want to chunk / deduplicate / extract are free to.

        ``metadata`` is an optional dict for caller-supplied context
        that does not fit the message list.  Callers may pass
        backend-specific fields such as ``app_id``, ``project_id``,
        or ``is_final``; normal AgentLoop turns leave it ``None``.
        Backends that do not consume metadata ignore it silently.

        Does not raise on transport / auth errors; it reports them.
        Both known callers act on the return value: the AgentLoop
        retries a turn's write with backoff and gives up after a fixed
        number of attempts, still counting the turn as unindexed if
        every attempt fails; a bulk import marks a source done in its
        resume state, so a write silently treated as landed would erase
        the only record that the source is still pending.

        Only an explicit ``False`` means the write failed. A backend
        that returns ``None`` (or anything else falsy-but-not-``False``)
        has not claimed the write was lost, so callers treat it as
        landed.
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
        warm caches, run migrations).

        The host calls this once at boot, but may not await it before the
        first turn: the TUI and ``raven serve`` spawn it as a task, so
        ``recall`` and ``store`` must tolerate being called while ``start``
        is still running -- no hits and not landed are the expected answers
        then. A raise leaves the session without this backend; it does not
        abort the host.
        """
        ...

    async def stop(self) -> None:
        """One-time / idempotent teardown. Adapters should make this
        safe to call after a failed ``start`` (so partial-init state
        cleans up)."""
        ...

    async def health(self) -> BackendHealth | None:
        """Whether the backend can work now, and what a person should look at
        when it cannot.

        Callable before or after ``start``: ``raven doctor`` asks an instance
        it never started, ``raven import`` asks after starting one. ``ready``
        is the import gate (a write now would land);
        ``checks`` is what doctor prints, verbatim, one line each. Only a real
        fault is ``"missing"``; a server that starts on demand and is not
        running yet is ``"ok"`` with a hint. ``None`` means this backend
        offers no diagnostics and the host proceeds.
        """
        ...


__all__ = ["BackendHealth", "HealthCheck", "HealthStatus", "Memory", "MemoryBackend"]


__tier__ = "contract"
