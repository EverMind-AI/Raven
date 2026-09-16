"""Mem0 Platform as a memory backend.

REST surface used (v3 pipeline, https://docs.mem0.ai/api-reference):
``POST /v3/memories/search/`` and ``POST /v3/memories/add/`` for the turn
path, ``GET /v1/memories/`` to read a conversation back (``run_id`` is the
session) and to prove the key, ``DELETE /v1/memories/{id}/`` for the browser.

v3's ``add`` extracts asynchronously and returns an ``event_id``; the batch is
accepted the moment that comes back, and nothing here polls ``/v1/event/``.
v3 has no time field on a message, so a message that carries a timestamp
gets it spliced into the content the extractor reads.
"""

from __future__ import annotations

from typing import Any

from raven.contracts.memory import Memory
from raven.plugins import PluginContext
from raven_cloud_memory._base import Call, CloudBackend, clamp_score

SEARCH_THRESHOLD = 0.1


class Mem0Backend(CloudBackend):
    NAME = "mem0"
    DEFAULT_BASE_URL = "https://api.mem0.ai"
    ENV_KEY = "MEM0_API_KEY"
    SIGNUP_URL = "https://app.mem0.ai"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._api_key}"}

    def _recall_call(self, query: str, top_k: int) -> Call:
        return Call(
            "POST",
            "/v3/memories/search/",
            json={
                "query": query,
                "top_k": top_k,
                "threshold": SEARCH_THRESHOLD,
                "rerank": False,
                "filters": {"user_id": self._user_id},
            },
        )

    def _parse_hits(self, body: Any) -> list[Memory]:
        rows = body.get("results") if isinstance(body, dict) else body
        out: list[Memory] = []
        for row in rows or []:
            text = str(row.get("memory") or "")
            if not text:
                continue
            out.append(
                Memory(
                    text=text,
                    score=clamp_score(row.get("score")),
                    metadata={"id": row.get("id"), "kind": "memory", "backend": self.NAME},
                )
            )
        return out

    def _store_call(self, session_id: str, messages: list[dict[str, Any]]) -> Call:
        rows = []
        for m in messages:
            content = m["content"]
            if m.get("timestamp"):
                content = f"[{m['timestamp']}] {content}"
            rows.append({"role": m["role"], "content": content})
        return Call(
            "POST",
            "/v3/memories/add/",
            json={"messages": rows, "user_id": self._user_id, "run_id": session_id},
        )

    def _delete_call(self, memory_id: str, kind: str | None) -> Call | None:
        return Call("DELETE", f"/v1/memories/{memory_id}/")

    def _session_call(self, session_id: str) -> Call:
        return Call("GET", "/v1/memories/", params={"user_id": self._user_id, "run_id": session_id})

    def _health_call(self) -> Call:
        return Call("GET", "/v1/memories/", params={"user_id": self._user_id, "page_size": 1})


def make_backend(ctx: PluginContext) -> Mem0Backend:
    """Sync and read-only: ``raven doctor`` constructs without starting."""
    return Mem0Backend(ctx)


__all__ = ["Mem0Backend", "make_backend"]
