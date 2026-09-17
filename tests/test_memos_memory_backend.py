"""MemOS Cloud backend: the shared hosted-backend cases plus MemOS's in-body verdicts."""

from __future__ import annotations

import httpx

from raven.memory_engine.http_backend import Reply
from raven_memos.backend import MemosBackend
from tests._hosted_memory_cases import USER, HostedBackendCases
from tests._hosted_memory_fakes import FakeMemos


class TestMemos(HostedBackendCases):
    fake_cls = FakeMemos
    backend_cls = MemosBackend
    RECALL_PATH = "/search/memory"
    HEALTH_PATH = "/search/memory"

    def store_path(self, session_id: str) -> str:
        return "/add/message"

    def session_path(self, session_id: str) -> str:
        return "/get/message"

    def recall_owner(self, request: httpx.Request) -> str:
        return FakeMemos.body(request)["user_id"]

    def store_owner(self, request: httpx.Request) -> str:
        return FakeMemos.body(request)["user_id"]

    def store_session(self, request: httpx.Request) -> str:
        return FakeMemos.body(request)["conversation_id"]

    # ── S-1 … S-7 ────────────────────────────────────────────────────

    async def test_auth_and_base_url(self, backend, fake):
        await backend.recall("tea", user_id=USER, top_k=1)
        assert fake.requests[0].headers["Authorization"] == f"Token {fake.KEY}"
        assert str(fake.requests[0].url).startswith("https://memos.memtensor.cn/api/openmem/v1/")

    async def test_add_message_body_and_in_body_verdict(self, backend, fake):
        assert await backend.store("conv-1", [{"role": "user", "content": "hi"}]) is True
        (req,) = fake.requests
        assert req.url.path.endswith("/add/message")
        assert FakeMemos.body(req) == {
            "messages": [{"role": "user", "content": "hi"}],
            "user_id": USER,
            "conversation_id": "conv-1",
            "async_mode": True,
        }
        assert fake.status_calls == 0
        assert backend._store_accepted(Reply(200, {"code": 0, "message": "queued"})) is False
        assert backend._store_accepted(Reply(200, {"code": 50000, "message": "ok"})) is False

    def test_in_body_codes_map_onto_http_statuses(self):
        backend = self.build(FakeMemos())
        assert backend._effective_status(Reply(200, {"code": 40309, "message": "rate limit"})) == 429
        assert backend._effective_status(Reply(200, {"code": 50001, "message": "boom"})) == 502
        assert backend._effective_status(Reply(200, {"code": 0, "message": "ok"})) == 200
        assert backend._effective_status(Reply(500, None)) == 500

    async def test_hits_keep_memory_and_preference_lists_only(self, backend, fake):
        fake.seed(USER, "alice drinks tea")
        fake.preferences[USER] = ["prefers oolong"]
        hits = await backend.recall("tea", user_id=USER, top_k=10)
        assert [h.text for h in hits] == ["alice drinks tea", "prefers oolong"]
        assert [h.metadata["kind"] for h in hits] == ["memory", "preference"]
        assert not any("noise" in h.text for h in hits)

    async def test_delete_sends_memory_ids_alone(self, backend, fake):
        """One selector only: ``user_id`` beside ``memory_ids`` is refused by
        the service (40071), and ``user_id`` alone would delete the whole user."""
        mid = fake.seed(USER, "x")
        fake.seed(USER, "y")
        assert await backend.delete(mid) is True
        (req,) = fake.requests
        assert req.url.path.endswith("/delete/memory")
        assert FakeMemos.body(req) == {"memory_ids": [mid]}
        assert fake.stored_texts(USER) == ["y"]

    async def test_recall_session_is_get_message(self, backend, fake):
        fake.seed(USER, "one", session="conv-1")
        hits = await backend.recall_session("conv-1", user_id=USER)
        (req,) = fake.requests
        assert req.url.path.endswith("/get/message")
        assert FakeMemos.body(req) == {"user_id": USER, "conversation_id": "conv-1"}
        assert [h.text for h in hits] == ["one"]

    async def test_health_is_a_one_row_search(self, backend, fake):
        await backend.health()
        (req,) = fake.requests
        assert req.url.path.endswith("/search/memory")
        assert FakeMemos.body(req)["memory_limit_number"] == 1

    async def test_recall_asks_for_rows_by_the_field_the_route_reads(self, backend, fake):
        """memory_limit_number, never top_k.

        The route ignores an unknown field instead of refusing it, so sending
        top_k leaves the service on its own default (6 rows) and the caller's
        budget has no effect at all -- a silent cap, not an error.
        """
        await backend.recall("tea", user_id=USER, top_k=40)
        (req,) = fake.requests
        body = FakeMemos.body(req)
        assert body["memory_limit_number"] == 40
        assert "top_k" not in body
