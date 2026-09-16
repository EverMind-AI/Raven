"""Zep Cloud backend: the shared hosted-backend cases plus the containers Zep needs."""

from __future__ import annotations

import httpx

from raven_cloud_memory.zep import ZepBackend
from tests._cloud_memory_cases import USER, CloudBackendCases
from tests._cloud_memory_fakes import FakeCloud, FakeZep


class TestZep(CloudBackendCases):
    fake_cls = FakeZep
    backend_cls = ZepBackend
    RECALL_PATH = "/api/v2/graph/search"
    HEALTH_PATH = f"/api/v2/users/{USER}"

    def store_path(self, session_id: str) -> str:
        return f"/api/v2/threads/{session_id}/messages"

    def session_path(self, session_id: str) -> str:
        return f"/api/v2/threads/{session_id}/messages"

    def recall_owner(self, request: httpx.Request) -> str:
        return FakeZep.body(request)["user_id"]

    def store_owner(self, request: httpx.Request) -> str:
        # The write names a thread; the thread was created against the owner.
        sid = request.url.path.removeprefix("/api/v2/threads/").removesuffix("/messages")
        return self._fake.threads[sid]

    def store_session(self, request: httpx.Request) -> str:
        return request.url.path.removeprefix("/api/v2/threads/").removesuffix("/messages")

    def delete_kind(self) -> str | None:
        return "edge"

    def prepare_health_ok(self, fake: FakeCloud) -> None:
        fake.users.add(USER)

    def build(self, fake, config=None, **kw):
        self._fake = fake
        return super().build(fake, config, **kw)

    # ── Z-1 … Z-8 ────────────────────────────────────────────────────

    async def test_auth_scheme_is_api_key(self, backend, fake):
        await backend.recall("tea", user_id=USER, top_k=1)
        assert fake.requests[0].headers["Authorization"] == f"Api-Key {fake.KEY}"
        assert ZepBackend.DEFAULT_BASE_URL == "https://api.getzep.com"

    async def test_start_creates_the_user_once_and_tolerates_409(self, backend, fake):
        await backend.start()
        await backend.start()
        assert fake.user_creates == 1
        assert USER in fake.users
        pre_existing = FakeZep()
        pre_existing.users.add(USER)
        other = self.build(pre_existing)
        await other.start()
        assert other._user_ready is True
        await other.stop()

    async def test_first_store_creates_the_thread_once(self, backend, fake):
        await backend.start()
        await backend.store("t-1", [{"role": "user", "content": "one"}])
        await backend.store("t-1", [{"role": "user", "content": "two"}])
        await backend.store("t-2", [{"role": "user", "content": "three"}])
        assert fake.thread_creates == 2
        assert fake.threads == {"t-1": USER, "t-2": USER}
        assert len(fake.calls("POST", "/t-1/messages")) == 2

    async def test_store_without_start_creates_the_user_first(self, backend, fake):
        assert await backend.store("t-1", [{"role": "user", "content": "one"}]) is True
        assert fake.user_creates == 1 and fake.thread_creates == 1

    async def test_existing_thread_409_is_fine(self, backend, fake):
        fake.users.add(USER)
        fake.threads["t-1"] = USER
        assert await backend.store("t-1", [{"role": "user", "content": "one"}]) is True
        assert fake.stored_texts(USER) == ["one"]

    async def test_messages_carry_created_at_and_no_task_poll(self, backend, fake):
        await backend.store(
            "t-1",
            [
                {"role": "user", "content": "one", "timestamp": "2026-03-01T10:00:00+00:00"},
                {"role": "assistant", "content": "two"},
            ],
        )
        (req,) = fake.calls("POST", "/t-1/messages")
        sent = FakeZep.body(req)["messages"]
        assert sent[0] == {"role": "user", "content": "one", "created_at": "2026-03-01T10:00:00+00:00"}
        assert sent[1] == {"role": "assistant", "content": "two"}
        assert fake.status_calls == 0

    async def test_recall_is_a_graph_search_over_edges(self, backend, fake):
        fake.seed(USER, "alice drinks tea")
        hits = await backend.recall("tea", user_id=USER, top_k=3)
        (req,) = fake.requests
        assert FakeZep.body(req) == {"user_id": USER, "query": "tea", "limit": 3, "scope": "edges"}
        assert hits[0].metadata["kind"] == "edge"

    def test_edge_without_a_score_gets_the_midpoint(self):
        backend = self.build(FakeZep())
        (hit,) = backend._parse_hits({"edges": [{"uuid": "e1", "fact": "f"}], "nodes": [{"name": "ignored"}]})
        assert hit.score == 0.5 and hit.metadata["id"] == "e1"

    async def test_delete_only_knows_edges(self, backend, fake):
        mid = fake.seed(USER, "alice drinks tea")
        assert await backend.delete(mid) is False
        assert await backend.delete(mid, kind="node") is False
        assert fake.requests == []
        assert await backend.delete(mid, kind="edge") is True
        assert fake.requests[-1].url.path == f"/api/v2/graph/edge/{mid}"

    async def test_recall_session_reads_the_thread(self, backend, fake):
        fake.users.add(USER)
        fake.threads["t-1"] = USER
        fake.seed(USER, "one", session="t-1", role="user")
        hits = await backend.recall_session("t-1", user_id=USER)
        assert [h.text for h in hits] == ["one"]
        assert fake.requests[-1].method == "GET"

    async def test_health_404_means_the_key_works_and_the_user_is_not_yet_made(self, backend, fake):
        health = await backend.health()
        assert health.ready is True
        assert health.checks[0].status == "ok"
        assert "first start" in (health.checks[0].hint or "")
