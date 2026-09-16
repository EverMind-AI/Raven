"""Mem0 Platform backend: the shared hosted-backend cases plus what only Mem0 does."""

from __future__ import annotations

from typing import Any

import httpx

from raven_mem0.backend import SEARCH_THRESHOLD, Mem0Backend
from tests._hosted_memory_cases import USER, HostedBackendCases
from tests._hosted_memory_fakes import FakeMem0


class TestMem0(HostedBackendCases):
    fake_cls = FakeMem0
    backend_cls = Mem0Backend
    RECALL_PATH = "/v3/memories/search/"
    HEALTH_PATH = "/v1/memories/"

    def store_path(self, session_id: str) -> str:
        return "/v3/memories/add/"

    def session_path(self, session_id: str) -> str:
        return "/v1/memories/"

    def recall_owner(self, request: httpx.Request) -> str:
        return FakeMem0.body(request)["filters"]["user_id"]

    def store_owner(self, request: httpx.Request) -> str:
        return FakeMem0.body(request)["user_id"]

    def store_session(self, request: httpx.Request) -> str:
        return FakeMem0.body(request)["run_id"]

    # ── M-1 … M-7 ────────────────────────────────────────────────────

    async def test_search_body_uses_the_v3_defaults(self, backend, fake):
        await backend.recall("tea", user_id=USER, top_k=4)
        (req,) = fake.requests
        body = FakeMem0.body(req)
        assert body == {
            "query": "tea",
            "top_k": 4,
            "threshold": SEARCH_THRESHOLD,
            "rerank": False,
            "filters": {"user_id": USER},
        }
        assert req.headers["Authorization"].startswith("Token ")

    async def test_add_is_the_v3_route_and_the_event_id_is_not_polled(self, backend, fake):
        assert await backend.store("run-7", [{"role": "user", "content": "hi"}]) is True
        (req,) = fake.requests
        assert req.url.path == "/v3/memories/add/"
        assert FakeMem0.body(req)["run_id"] == "run-7"
        assert fake.calls("GET", "/v1/event/") == [] and fake.status_calls == 0

    async def test_timestamp_is_spliced_into_the_content(self, backend, fake):
        await backend.store(
            "s",
            [
                {"role": "user", "content": "I moved to Paris", "timestamp": "2026-03-01T10:00:00+00:00"},
                {"role": "assistant", "content": "Noted"},
            ],
        )
        sent = FakeMem0.body(fake.requests[0])["messages"]
        assert sent[0]["content"] == "[2026-03-01T10:00:00+00:00] I moved to Paris"
        assert sent[1]["content"] == "Noted"
        assert set(sent[0]) == {"role", "content"}

    def test_both_search_response_shapes_parse(self, tmp_path):
        backend = self.build(self.fake_cls())
        row: dict[str, Any] = {"id": "m1", "memory": "likes tea", "score": 0.42}
        assert [h.text for h in backend._parse_hits({"results": [row]})] == ["likes tea"]
        assert [h.metadata["id"] for h in backend._parse_hits([row])] == ["m1"]
        assert backend._parse_hits({"results": []}) == []

    async def test_delete_is_the_v1_route(self, backend, fake):
        mid = fake.seed(USER, "x")
        assert await backend.delete(mid) is True
        (req,) = fake.requests
        assert (req.method, req.url.path) == ("DELETE", f"/v1/memories/{mid}/")

    async def test_recall_session_lists_by_run_id(self, backend, fake):
        fake.seed(USER, "a", session="run-1")
        await backend.recall_session("run-1", user_id=USER)
        (req,) = fake.requests
        assert (req.method, req.url.path) == ("GET", "/v1/memories/")
        assert dict(req.url.params) == {"user_id": USER, "run_id": "run-1"}

    async def test_health_is_a_one_row_list(self, backend, fake):
        await backend.health()
        (req,) = fake.requests
        assert (req.method, req.url.path) == ("GET", "/v1/memories/")
        assert dict(req.url.params) == {"user_id": USER, "page_size": "1"}
