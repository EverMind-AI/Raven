"""EverosSkillSource — re-emit ``MemoryBackend`` agent-track hits as RouterHits.

This is the source through which **the self-evolving skill track**
plugs into the router. The backend's ``recall(agent_id=...)``
returns :class:`Memory` records; we re-wrap them as :class:`RouterHit`
with the ``everos/`` prefix so :class:`SkillForgeRouter` can RRF-fuse them
against Local + Mass.

The source is **host code, not part of any plugin**. The actual
backend behind it can be the bundled EverOS plugin or any other
:class:`MemoryBackend` adapter — for a backend that doesn't carry an
agent track (mem0 / MemOS / Letta), ``recall`` returns an empty list
and the source gracefully degrades.

Why we accept the ``agent_id`` in the constructor rather than at
search-time: it's a host-policy decision (driven by the agent's
config), not a per-query value. Putting it in ``__init__`` means each
``search`` stays cheap and there's exactly one place to audit who the
agent owner is.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from raven.memory_engine.skill_forge.types import RouterHit

if TYPE_CHECKING:
    from raven.memory_engine.backend import MemoryBackend

logger = logging.getLogger(__name__)


def _stable_id_for(text: str) -> str:
    """Stable 12-hex-char fingerprint when the backend omits an id.

    EverMem returns proper ids; this is a safety net for adapters
    (mem0 / Letta) whose hit objects might not carry one. Same text
    always hashes to the same id, so feedback dispatch still has a
    consistent key even without an upstream id."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _short_name_for(text: str) -> str:
    """Display-name fallback: first non-blank line truncated to 40 chars."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:40]
    return text[:40]


class EverosSkillSource:
    """Adapter that turns ``backend.recall(agent_id=...)`` into the
    third member of :class:`SkillForgeRouter`'s source set.

    ``weight = 0.9`` sits between Local (1.0 — most trusted, hand-
    curated) and Mass (0.8 — imported, may not match project
    conventions). Self-evolved skills are task-specific (so we trust
    them more than Mass) but unvalidated by humans (so we trust them
    less than Local).
    """

    name: str = "everos"
    weight: float = 0.9

    def __init__(
        self,
        backend: "MemoryBackend",
        agent_id: str,
    ) -> None:
        self._backend = backend
        self._agent_id = agent_id

    async def search(
        self,
        query: str,
        history: list[dict[str, Any]],
        k: int,
    ) -> list[RouterHit]:
        # ``history`` isn't forwarded today — the Protocol surface for
        # ``MemoryBackend.recall`` is intentionally small (query +
        # track id + top_k). When EverMem grows a "rerank by conversation
        # context" mode, we'll add an optional field here that
        # backends can ignore.
        del history

        hits = await self._backend.recall(
            query,
            agent_id=self._agent_id,
            top_k=k,
        )

        out: list[RouterHit] = []
        for m in hits:
            native_id = (m.metadata.get("id") if m.metadata else None) or _stable_id_for(m.text)
            name = (m.metadata.get("name") if m.metadata else None) or _short_name_for(m.text)
            out.append(
                RouterHit(
                    qualified_id=f"everos/{native_id}",
                    name=name,
                    content=m.text,
                    score=m.score,
                    meta={
                        "source": "everos",
                        # The original Memory.metadata flows through —
                        # the after-turn feedback dispatcher reads
                        # things like ``owner_type`` / ``episode_type``
                        # / confidence when forming feedback signals.
                        **(m.metadata or {}),
                    },
                ),
            )
        return out


__all__ = ["EverosSkillSource"]
