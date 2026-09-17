"""MemOS Cloud as a memory backend.

REST surface used (``https://memos.memtensor.cn/api/openmem/v1``, the paths the
official ``MemOSClient`` calls): ``POST /add/message`` and ``POST
/search/memory`` for the turn path, ``POST /get/message`` to read one
conversation back, ``POST /delete/memory`` with ``memory_ids`` alone for the
browser. A search row is ``{"id", "memory_key", "memory_value", "memory_type",
"relativity", "conversation_id", ...}`` (observed 2026-09-15).

A search takes its row budget as memory_limit_number; top_k is not a
field of this route and is ignored rather than refused, which caps a caller that
sends it at the service default of 6 rows.

MemOS reports failure inside a 200: ``code`` is ``0`` on success, ``40309`` on
rate limiting, and ``message`` is ``"ok"``. ``_effective_status`` folds those
onto the HTTP statuses the base class already handles. A search answers with
several lists; this backend keeps the two that are memory about the user
(``memory_detail_list`` and ``preference_detail_list``) and ignores the tool,
skill, profile and event lists.
"""

from __future__ import annotations

from typing import Any

from raven.contracts.memory import Memory
from raven.memory_engine import Call, HttpMemoryBackend, Reply, clamp_score
from raven.plugins import PluginContext

RATE_LIMITED_CODE = 40309


def _body_code(body: Any) -> int | None:
    if isinstance(body, dict) and isinstance(body.get("code"), int):
        return body["code"]
    return None


class MemosBackend(HttpMemoryBackend):
    NAME = "memos"
    DEFAULT_BASE_URL = "https://memos.memtensor.cn/api/openmem/v1"
    ENV_KEY = "MEMOS_API_KEY"
    SIGNUP_URL = "https://memos-dashboard.openmem.net"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._api_key}"}

    def _effective_status(self, reply: Reply) -> int:
        code = _body_code(reply.body)
        if reply.status == 200 and code is not None and code != 0:
            return 429 if code == RATE_LIMITED_CODE else 502
        return reply.status

    def _store_accepted(self, reply: Reply) -> bool:
        return self._effective_status(reply) == 200 and (reply.body or {}).get("message") == "ok"

    def _recall_call(self, query: str, top_k: int, owner: str) -> Call:
        # The row budget is memory_limit_number. top_k is not a field of
        # this route: it is accepted and ignored, so the service answers with its
        # own default (6 rows, measured 2026-09-17) whatever the caller asked for.
        return Call(
            "POST",
            "/search/memory",
            json={"query": query, "user_id": owner, "memory_limit_number": top_k},
        )

    def _parse_hits(self, body: Any) -> list[Memory]:
        data = (body or {}).get("data") or {}
        out: list[Memory] = []
        for kind, key in (("memory", "memory_detail_list"), ("preference", "preference_detail_list")):
            for row in data.get(key) or []:
                text = str(row.get("memory_value") or row.get("preference") or "")
                if not text:
                    continue
                out.append(
                    Memory(
                        text=text,
                        score=clamp_score(row.get("relativity", row.get("score"))),
                        metadata={
                            "id": row.get("memory_id") or row.get("id"),
                            "kind": kind,
                            "backend": self.NAME,
                        },
                    )
                )
        return out

    def _store_call(self, session_id: str, messages: list[dict[str, Any]], owner: str) -> Call:
        rows = [{"role": m["role"], "content": m["content"]} for m in messages]
        return Call(
            "POST",
            "/add/message",
            json={
                "messages": rows,
                "user_id": owner,
                "conversation_id": session_id,
                "async_mode": True,
            },
        )

    def _delete_call(self, memory_id: str, kind: str | None) -> Call | None:
        # The route takes exactly one selector; ``user_id`` beside ``memory_ids``
        # is refused (code 40071), and ``user_id`` alone deletes the whole user.
        return Call("POST", "/delete/memory", json={"memory_ids": [memory_id]})

    def _session_call(self, session_id: str, owner: str) -> Call:
        return Call("POST", "/get/message", json={"user_id": owner, "conversation_id": session_id})

    def _parse_session(self, body: Any) -> list[Memory]:
        data = (body or {}).get("data") or {}
        rows = data.get("message_detail_list") or data.get("messages") or []
        out: list[Memory] = []
        for m in rows:
            content = str(m.get("content") or "")
            if not content:
                continue
            out.append(
                Memory(
                    text=content,
                    score=0.5,
                    metadata={"id": m.get("message_id") or m.get("id"), "kind": "message", "backend": self.NAME},
                )
            )
        return out

    def _health_call(self) -> Call:
        return Call(
            "POST",
            "/search/memory",
            json={"query": "health", "user_id": self._user_id, "memory_limit_number": 1},
        )


def make_backend(ctx: PluginContext) -> MemosBackend:
    """Sync and read-only: ``raven doctor`` constructs without starting."""
    return MemosBackend(ctx)


__all__ = ["MemosBackend", "make_backend"]
