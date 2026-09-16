"""Zep Cloud as a memory backend.

REST surface used (v2 API, https://help.getzep.com/api-reference): a user
(``POST /api/v2/users``) holds threads (``POST /api/v2/threads``); a session's
messages go to ``POST /api/v2/threads/{id}/messages``; cross-session recall is
``POST /api/v2/graph/search`` over the user's graph, whose hits are edges
carrying a ``fact``. ``GET /api/v2/threads/{id}/messages`` reads one session
back; ``DELETE /api/v2/graph/edge/{uuid}`` removes one fact.

Zep needs the containers to exist before a write lands, so ``start`` creates
the host's user, a write under another owner creates that user first, and the
first ``store`` of a session creates its thread. All treat 409 as "already
there", and all are remembered so a session costs one extra request, not one
per turn. Ingestion returns a ``task_id`` that nobody polls.

Zep caps a write at 30 messages per request and 4,096 characters per message
(https://help.getzep.com/v3/adding-messages#message-limits); a batch beyond
either is split, a longer message into consecutive messages of the same role,
so the importer's 100-message batches land instead of failing as a whole.
"""

from __future__ import annotations

from typing import Any

from raven.contracts.memory import BackendHealth, HealthCheck, Memory
from raven.memory_engine import STORE_TIMEOUT_S, Call, HttpMemoryBackend, Reply, clamp_score
from raven.plugins import PluginContext

_EXISTS = (409,)
MAX_MESSAGES_PER_REQUEST = 30
MAX_MESSAGE_CHARS = 4096


class ZepBackend(HttpMemoryBackend):
    NAME = "zep"
    DEFAULT_BASE_URL = "https://api.getzep.com"
    ENV_KEY = "ZEP_API_KEY"
    SIGNUP_URL = "https://app.getzep.com"

    def __init__(self, ctx: PluginContext, **kwargs: Any) -> None:
        super().__init__(ctx, **kwargs)
        self._users_ready: set[str] = set()
        self._threads: dict[str, str] = {}

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Api-Key {self._api_key}"}

    async def start(self) -> None:
        if self._api_key:
            await self._ensure_user(self._user_id)

    async def _ensure_user(self, owner: str) -> bool:
        if owner in self._users_ready:
            return True
        reply = await self._send(Call("POST", "/api/v2/users", json={"user_id": owner}), timeout=STORE_TIMEOUT_S)
        if reply.ok or reply.status in _EXISTS:
            self._users_ready.add(owner)
            return True
        self._warn("user", reply)
        return False

    async def _before_store(self, session_id: str, owner: str) -> bool:
        if self._threads.get(session_id) == owner:
            return True
        if not await self._ensure_user(owner):
            return False
        reply = await self._send(
            Call("POST", "/api/v2/threads", json={"thread_id": session_id, "user_id": owner}),
            timeout=STORE_TIMEOUT_S,
        )
        if reply.ok or reply.status in _EXISTS:
            self._threads[session_id] = owner
            return True
        self._warn("thread", reply)
        return False

    def _store_call(self, session_id: str, messages: list[dict[str, Any]], owner: str) -> Call:
        rows = []
        for m in messages:
            row: dict[str, Any] = {"role": m["role"], "content": m["content"]}
            if m.get("timestamp"):
                row["created_at"] = m["timestamp"]
            rows.append(row)
        return Call("POST", f"/api/v2/threads/{session_id}/messages", json={"messages": rows})

    def _store_calls(self, session_id: str, messages: list[dict[str, Any]], owner: str) -> list[Call]:
        pieces: list[dict[str, Any]] = []
        for m in messages:
            content = m["content"]
            for at in range(0, len(content), MAX_MESSAGE_CHARS):
                pieces.append({**m, "content": content[at : at + MAX_MESSAGE_CHARS]})
        return [
            self._store_call(session_id, pieces[i : i + MAX_MESSAGES_PER_REQUEST], owner)
            for i in range(0, len(pieces), MAX_MESSAGES_PER_REQUEST)
        ]

    def _recall_call(self, query: str, top_k: int, owner: str) -> Call:
        return Call(
            "POST",
            "/api/v2/graph/search",
            json={"user_id": owner, "query": query, "limit": top_k, "scope": "edges"},
        )

    def _parse_hits(self, body: Any) -> list[Memory]:
        out: list[Memory] = []
        for edge in (body or {}).get("edges") or []:
            fact = str(edge.get("fact") or "")
            if not fact:
                continue
            out.append(
                Memory(
                    text=fact,
                    score=clamp_score(edge.get("score")),
                    metadata={"id": edge.get("uuid"), "kind": "edge", "backend": self.NAME},
                )
            )
        return out

    def _delete_call(self, memory_id: str, kind: str | None) -> Call | None:
        # Only the kind our own recall hands out; a node or episode id would
        # need a different route and this backend never issued one.
        if kind != "edge":
            return None
        return Call("DELETE", f"/api/v2/graph/edge/{memory_id}")

    def _session_call(self, session_id: str, owner: str) -> Call:
        return Call("GET", f"/api/v2/threads/{session_id}/messages")

    def _parse_session(self, body: Any) -> list[Memory]:
        out: list[Memory] = []
        for m in (body or {}).get("messages") or []:
            content = str(m.get("content") or "")
            if not content:
                continue
            out.append(
                Memory(
                    text=content,
                    score=0.5,
                    metadata={"id": m.get("uuid"), "kind": "message", "backend": self.NAME, "role": m.get("role")},
                )
            )
        return out

    def _health_call(self) -> Call:
        return Call("GET", f"/api/v2/users/{self._user_id}")

    def _classify_health(self, status: int, reply: Reply) -> BackendHealth:
        if status == 404:
            # The key answered; the user is created by ``start`` on first use.
            return BackendHealth(
                ready=True,
                checks=[HealthCheck(self.NAME, "ok", f"{self._base_url} (user is created at first start)")],
            )
        return super()._classify_health(status, reply)


def make_backend(ctx: PluginContext) -> ZepBackend:
    """Sync and read-only: ``raven doctor`` constructs without starting."""
    return ZepBackend(ctx)


__all__ = ["ZepBackend", "make_backend"]
