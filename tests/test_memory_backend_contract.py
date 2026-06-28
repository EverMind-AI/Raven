"""MB-2 — verify ``MemoryBackendContractTests`` runs against a sample backend.

The base class itself is in the package so it isn't auto-collected as
a test class. We exercise it here by sub-classing with a tiny fake
in-memory backend — this proves both that the base class fixture/test
shape works AND demonstrates the pattern third-party adapter authors
will follow.
"""

from __future__ import annotations

from typing import Any

from raven.memory_engine import (
    LifecycleContractTests,
    Memory,
    MemoryBackend,
    MemoryBackendContractTests,
)


# ---------------------------------------------------------------------------
# Tiny in-memory backend — enough surface area to exercise the contract
# ---------------------------------------------------------------------------


class _DictBackend:
    """Stores messages in a dict keyed by session_id; recall returns
    the most-recent N text contents as Memory hits. Naive but valid."""

    def __init__(self) -> None:
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self._started = False
        self._stopped = False

    async def start(self) -> None:
        # Idempotent start.
        self._started = True

    async def stop(self) -> None:
        # Idempotent stop — must work even if start never ran.
        self._stopped = True

    async def recall(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        top_k: int,
    ) -> list[Memory]:
        # Trivially: pull all messages across all sessions for the owner.
        # No actual filtering — ids are opaque, we just return [].
        # For the user track "contract-test" we surface stored content; for
        # any unknown owner (or neither/both ids) we return [] to verify
        # that path.
        if user_id != "contract-test":
            return []
        all_msgs = []
        for msgs in self._sessions.values():
            for m in msgs:
                all_msgs.append(m)
        # Return the last N as Memory hits.
        out = []
        for m in all_msgs[-top_k:]:
            out.append(
                Memory(
                    text=str(m.get("content", "")),
                    score=0.5,
                    metadata={"role": m.get("role")},
                ),
            )
        return out

    async def store(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        self._sessions.setdefault(session_id, []).extend(messages)

    async def feedback(self, signals: dict[str, Any]) -> None:
        # No-op — valid per Protocol.
        return None


# ---------------------------------------------------------------------------
# Run the contract base against the fake backend
# ---------------------------------------------------------------------------


class TestDictBackendContract(MemoryBackendContractTests):
    async def make_backend(self) -> MemoryBackend:
        return _DictBackend()


class TestDictBackendLifecycle(LifecycleContractTests):
    async def make_backend(self) -> MemoryBackend:
        return _DictBackend()
