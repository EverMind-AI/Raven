"""Stateful stand-ins for the three hosted memory services.

Each fake answers over ``httpx.MockTransport`` with the request paths, auth
scheme and response bodies of its service, keeps what was stored per user, and
finds it again by substring. Every input the backend sends is honoured or
refused; nothing is silently ignored:

- a wrong or missing key is 401 in the service's own body shape;
- memories are bucketed by user, so a wrong owner id finds nothing;
- ingestion is asynchronous the way the real services are (Mem0 ``event_id``,
  Zep ``task_id``, MemOS ``async_mode``) and the status endpoints report
  "still running" forever -- a backend that polls them hangs, which is the
  point: ``status_calls`` counts who asked;
- ``extra_hits`` pads a search with more rows than asked, for ``top_k`` tests;
- one-shot faults (``fail_next`` / ``malformed_next`` / ``rate_limit_next`` /
  ``timeout_next``) fire on the next request and reset.

``inert=True`` accepts writes without keeping them: the switch that proves the
round-trip tests can fail.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class FakeCloud:
    KEY = "good-key-1234567890"
    AUTH_SCHEME = "Token"

    def __init__(self, *, inert: bool = False) -> None:
        self.inert = inert
        self.requests: list[httpx.Request] = []
        self.status_calls = 0
        self.extra_hits = 0
        self.memories: dict[str, list[dict[str, Any]]] = {}
        self._seq = 0
        self._fault: tuple[str, Any] | None = None

    # ── faults ───────────────────────────────────────────────────────

    def fail_next(self, status: int) -> None:
        self._fault = ("status", status)

    def malformed_next(self) -> None:
        self._fault = ("malformed", None)

    def rate_limit_next(self) -> None:
        self._fault = ("rate", None)

    def timeout_next(self) -> None:
        self._fault = ("timeout", None)

    # ── helpers for tests ────────────────────────────────────────────

    def seed(self, user_id: str, text: str, *, session: str = "seed", **extra: Any) -> str:
        return self._keep(user_id, text, session, **extra)

    def stored_texts(self, user_id: str) -> list[str]:
        return [m["text"] for m in self.memories.get(user_id, [])]

    def calls(self, method: str | None = None, path_suffix: str | None = None) -> list[httpx.Request]:
        out = []
        for r in self.requests:
            if method and r.method != method:
                continue
            if path_suffix and not r.url.path.endswith(path_suffix):
                continue
            out.append(r)
        return out

    @staticmethod
    def body(request: httpx.Request) -> Any:
        return json.loads(request.content) if request.content else None

    # ── transport ────────────────────────────────────────────────────

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fault, self._fault = self._fault, None
        if fault:
            kind, value = fault
            if kind == "timeout":
                raise httpx.ReadTimeout("fake: no answer", request=request)
            if kind == "status":
                return httpx.Response(value, json=self._error_body(value))
            if kind == "malformed":
                return httpx.Response(200, content=b"<html>not json</html>")
            if kind == "rate":
                return self._rate_limited()
        if request.headers.get("Authorization") != f"{self.AUTH_SCHEME} {self.KEY}":
            return httpx.Response(401, json=self._error_body(401))
        return self.route(request)

    def route(self, request: httpx.Request) -> httpx.Response:
        raise NotImplementedError

    def _rate_limited(self) -> httpx.Response:
        return httpx.Response(429, json=self._error_body(429))

    def _error_body(self, status: int) -> Any:
        return {"detail": f"fake error {status}"}

    def _keep(self, user_id: str, text: str, session: str, **extra: Any) -> str:
        self._seq += 1
        mid = f"{self.__class__.__name__.lower()}-{self._seq}"
        if not self.inert:
            self.memories.setdefault(user_id, []).append({"id": mid, "text": text, "session": session, **extra})
        return mid

    def _search(self, user_id: str, query: str) -> list[dict[str, Any]]:
        hits = [m for m in self.memories.get(user_id, []) if query.lower() in m["text"].lower()]
        for i in range(self.extra_hits):
            hits.append({"id": f"pad-{i}", "text": f"padding {i} {query}", "session": "pad"})
        return hits

    def _delete(self, memory_id: str) -> bool:
        for rows in self.memories.values():
            for i, m in enumerate(rows):
                if m["id"] == memory_id:
                    del rows[i]
                    return True
        return False


class FakeMem0(FakeCloud):
    """Mem0 Platform, v3 add/search plus the v1 list/delete routes."""

    def route(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/v3/memories/search/":
            b = self.body(request)
            user = (b.get("filters") or {}).get("user_id")
            rows = [{"id": m["id"], "memory": m["text"], "score": 0.9} for m in self._search(user, b["query"])]
            return httpx.Response(200, json={"results": rows})
        if method == "POST" and path == "/v3/memories/add/":
            b = self.body(request)
            for m in b["messages"]:
                self._keep(b["user_id"], m["content"], b.get("run_id") or "", role=m["role"])
            return httpx.Response(200, json={"event_id": "evt-fake"})
        if method == "GET" and path.startswith("/v1/event/"):
            self.status_calls += 1
            return httpx.Response(200, json={"status": "PENDING"})
        if method == "GET" and path == "/v1/memories/":
            user = request.url.params.get("user_id")
            run = request.url.params.get("run_id")
            rows = [
                {"id": m["id"], "memory": m["text"], "score": 0.5}
                for m in self.memories.get(user, [])
                if run is None or m["session"] == run
            ]
            return httpx.Response(200, json=rows)
        if method == "DELETE" and path.startswith("/v1/memories/"):
            mid = path.removeprefix("/v1/memories/").strip("/")
            return httpx.Response(200 if self._delete(mid) else 404, json={"message": "ok"})
        return httpx.Response(404, json={"detail": f"no route {method} {path}"})


class FakeZep(FakeCloud):
    """Zep Cloud v2: users hold threads; recall is a graph search over edges."""

    AUTH_SCHEME = "Api-Key"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.users: set[str] = set()
        self.threads: dict[str, str] = {}
        self.user_creates = 0
        self.thread_creates = 0

    def _error_body(self, status: int) -> Any:
        return {"message": f"fake {status}", "request_id": "req-fake"}

    def seed(self, user_id: str, text: str, *, session: str = "seed", **extra: Any) -> str:
        # A seeded fact lives in a thread of that user, as a stored one would.
        self.users.add(user_id)
        self.threads.setdefault(session, user_id)
        return super().seed(user_id, text, session=session, **extra)

    def route(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/api/v2/users":
            uid = self.body(request)["user_id"]
            self.user_creates += 1
            if uid in self.users:
                return httpx.Response(409, json={"message": "user already exists"})
            self.users.add(uid)
            return httpx.Response(201, json={"user_id": uid})
        if method == "GET" and path.startswith("/api/v2/users/"):
            uid = path.removeprefix("/api/v2/users/")
            if uid in self.users:
                return httpx.Response(200, json={"user_id": uid})
            return httpx.Response(404, json={"message": "not found"})
        if method == "POST" and path == "/api/v2/threads":
            b = self.body(request)
            self.thread_creates += 1
            if b["thread_id"] in self.threads:
                return httpx.Response(409, json={"message": "thread already exists"})
            self.threads[b["thread_id"]] = b["user_id"]
            return httpx.Response(201, json={"thread_id": b["thread_id"]})
        if method == "POST" and path.startswith("/api/v2/threads/") and path.endswith("/messages"):
            sid = path.removeprefix("/api/v2/threads/").removesuffix("/messages")
            if sid not in self.threads:
                return httpx.Response(404, json={"message": "thread not found"})
            for m in self.body(request)["messages"]:
                self._keep(self.threads[sid], m["content"], sid, role=m["role"])
            return httpx.Response(202, json={"task_id": "task-fake"})
        if method == "GET" and path.startswith("/api/v2/threads/") and path.endswith("/messages"):
            sid = path.removeprefix("/api/v2/threads/").removesuffix("/messages")
            user = self.threads.get(sid)
            rows = [
                {"uuid": m["id"], "role": m.get("role", "user"), "content": m["text"]}
                for m in self.memories.get(user, [])
                if m["session"] == sid
            ]
            return httpx.Response(200, json={"messages": rows})
        if method == "GET" and path.startswith("/api/v2/tasks/"):
            self.status_calls += 1
            return httpx.Response(200, json={"status": "pending"})
        if method == "POST" and path == "/api/v2/graph/search":
            b = self.body(request)
            edges = [{"uuid": m["id"], "fact": m["text"], "score": 0.8} for m in self._search(b["user_id"], b["query"])]
            return httpx.Response(200, json={"edges": edges, "nodes": [], "episodes": []})
        if method == "DELETE" and path.startswith("/api/v2/graph/edge/"):
            mid = path.removeprefix("/api/v2/graph/edge/")
            if self._delete(mid):
                return httpx.Response(200, json={"message": "ok"})
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(404, json={"message": f"no route {method} {path}"})


class FakeMemos(FakeCloud):
    """MemOS Cloud: every answer is a 200 whose ``code`` carries the verdict."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.preferences: dict[str, list[str]] = {}

    def _error_body(self, status: int) -> Any:
        return {"code": status, "message": f"fake {status}", "data": None}

    def _rate_limited(self) -> httpx.Response:
        return httpx.Response(200, json={"code": 40309, "message": "rate limit exceeded", "data": None})

    def route(self, request: httpx.Request) -> httpx.Response:
        # The real base URL carries a path prefix; routes are relative to it.
        path = request.url.path.removeprefix("/api/openmem/v1")
        method = request.method
        b = self.body(request) or {}
        if method == "POST" and path == "/add/message":
            for m in b["messages"]:
                self._keep(b["user_id"], m["content"], b.get("conversation_id") or "", role=m["role"])
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"task_id": "task-fake"}})
        if method == "POST" and path == "/search/memory":
            hits = self._search(b["user_id"], b["query"])
            data = {
                "memory_detail_list": [
                    {
                        "id": m["id"],
                        "memory_key": "fact",
                        "memory_value": m["text"],
                        "memory_type": "UserMemory",
                        "relativity": 0.7,
                        "conversation_id": m["session"],
                    }
                    for m in hits
                ],
                "preference_detail_list": [
                    {"id": f"pref-{i}", "memory_value": p, "preference_type": "explicit_preference"}
                    for i, p in enumerate(self.preferences.get(b["user_id"], []))
                ],
                "tool_memory_detail_list": [{"memory_id": "tool-1", "memory_value": "tool noise"}],
                "skill_detail_list": [{"memory_id": "skill-1", "memory_value": "skill noise"}],
                "profile_detail_list": [{"memory_id": "profile-1", "memory_value": "profile noise"}],
                "event_detail_list": [{"memory_id": "event-1", "memory_value": "event noise"}],
                "preference_note": "",
            }
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": data})
        if method == "POST" and path == "/get/message":
            rows = [
                {"message_id": m["id"], "role": m.get("role", "user"), "content": m["text"]}
                for m in self.memories.get(b["user_id"], [])
                if m["session"] == b.get("conversation_id")
            ]
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"message_detail_list": rows}})
        if method == "POST" and path == "/delete/memory":
            selectors = [k for k in ("user_id", "memory_ids", "filter") if b.get(k)]
            if len(selectors) != 1:
                message = "Only one of user_id, memory_ids, or filter can be provided"
                return httpx.Response(400, json={"code": 40071, "data": None, "message": message})
            if selectors == ["user_id"]:
                self.memories.pop(b["user_id"], None)
                return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"success": True}})
            deleted = [mid for mid in b.get("memory_ids") or [] if self._delete(mid)]
            if not deleted:
                return httpx.Response(404, json=self._error_body(404))
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"deleted": deleted}})
        if method == "POST" and path == "/get/status":
            self.status_calls += 1
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"status": "running"}})
        return httpx.Response(404, json=self._error_body(404))


def client_for(fake: FakeCloud) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))


__all__ = ["FakeCloud", "FakeMem0", "FakeMemos", "FakeZep", "client_for"]
