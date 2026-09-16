"""Zep Cloud as a memory backend.

REST surface used (v2 API, https://help.getzep.com/api-reference): a user
(``POST /api/v2/users``) holds threads (``POST /api/v2/threads``); a session's
messages go to ``POST /api/v2/threads/{id}/messages``; cross-session recall is
``POST /api/v2/graph/search`` over the user's graph, whose hits are edges
carrying a ``fact``. ``GET /api/v2/threads/{id}/messages`` reads one session
back; ``DELETE /api/v2/graph/edge/{uuid}`` removes one fact.

Zep needs the containers to exist before a write lands, so ``start`` creates
the user and the first ``store`` of a session creates its thread. Both treat
409 as "already there", and both are remembered so a session costs one extra
request, not one per turn. Ingestion returns a ``task_id`` that nobody polls.
"""

from __future__ import annotations

from typing import Any

from raven.contracts.memory import BackendHealth, HealthCheck, Memory
from raven.plugins import PluginContext
from raven_cloud_memory._base import STORE_TIMEOUT_S, Call, CloudBackend, Reply, clamp_score

_EXISTS = (409,)


class ZepBackend(CloudBackend):
    NAME = "zep"
    DEFAULT_BASE_URL = "https://api.getzep.com"
    ENV_KEY = "ZEP_API_KEY"
    SIGNUP_URL = "https://app.getzep.com"

    def __init__(self, ctx: PluginContext, **kwargs: Any) -> None:
        super().__init__(ctx, **kwargs)
        self._user_ready = False
        self._threads: set[str] = set()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Api-Key {self._api_key}"}

    async def start(self) -> None:
        if self._user_ready or not self._api_key:
            return
        reply = await self._send(
            Call("POST", "/api/v2/users", json={"user_id": self._user_id}),
            timeout=STORE_TIMEOUT_S,
        )
        if reply.ok or reply.status in _EXISTS:
            self._user_ready = True
        else:
            self._warn("start", reply)

    async def _before_store(self, session_id: str) -> bool:
        if session_id in self._threads:
            return True
        if not self._user_ready:
            await self.start()
        reply = await self._send(
            Call("POST", "/api/v2/threads", json={"thread_id": session_id, "user_id": self._user_id}),
            timeout=STORE_TIMEOUT_S,
        )
        if reply.ok or reply.status in _EXISTS:
            self._threads.add(session_id)
            return True
        self._warn("thread", reply)
        return False

    def _store_call(self, session_id: str, messages: list[dict[str, Any]]) -> Call:
        rows = []
        for m in messages:
            row: dict[str, Any] = {"role": m["role"], "content": m["content"]}
            if m.get("timestamp"):
                row["created_at"] = m["timestamp"]
            rows.append(row)
        return Call("POST", f"/api/v2/threads/{session_id}/messages", json={"messages": rows})

    def _recall_call(self, query: str, top_k: int) -> Call:
        return Call(
            "POST",
            "/api/v2/graph/search",
            json={"user_id": self._user_id, "query": query, "limit": top_k, "scope": "edges"},
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

    def _session_call(self, session_id: str) -> Call:
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
