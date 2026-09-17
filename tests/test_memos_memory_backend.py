"""MemOS Cloud backend: the shared hosted-backend cases plus MemOS's in-body verdicts."""

from __future__ import annotations

import httpx

from raven.memory_engine.http_backend import Reply
from raven_memos.backend import MemosBackend
from tests._hosted_memory_cases import AGENT, USER, HostedBackendCases
from tests._hosted_memory_fakes import FakeMemos

_SKILL = {
    "name": "Widen a strict type check",
    "description": "Accept numpy scalars in validation.",
    "procedure": ["1. Find _validate_params", "2. Use numbers.Integral"],
}


class TestMemos(HostedBackendCases):
    fake_cls = FakeMemos
    backend_cls = MemosBackend
    RECALL_PATH = "/search/memory"
    HEALTH_PATH = "/search/memory"
    serves_agent_track = True

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

    async def test_hits_keep_the_views_asked_for_and_drop_the_rest(self, backend, fake):
        fake.seed(USER, "alice drinks tea")
        fake.preferences[USER] = ["prefers oolong"]
        fake.skills[USER] = [_SKILL]
        hits = await backend.recall("tea", user_id=USER, top_k=10)
        # Ordered by relativity across the views, not by which list they sat in:
        # the caller keeps a prefix, so a weak factual row must not displace a
        # strong skill just for arriving in an earlier list.
        assert [h.metadata["kind"] for h in hits] == ["skill", "memory", "preference"]
        assert hits[1].text == "alice drinks tea"
        assert hits[2].text == "prefers oolong"
        # Tool, profile and event rows are memory about the session, not about
        # the work; they stay out.
        assert not any("noise" in h.text for h in hits)

    async def test_a_skill_arrives_as_its_procedure_not_as_an_empty_string(self, backend, fake):
        """``skill_value`` is a triple, and the empty-text guard hides the miss.

        Read a skill row for ``memory_value`` and it is blank, and a blank hit
        is skipped -- so the whole view reads as "this pool has no skills"
        rather than as a field name that does not match.
        """
        fake.skills[USER] = [_SKILL]
        hits = await backend.recall("types", user_id=USER, top_k=10)
        (skill,) = [h for h in hits if h.metadata["kind"] == "skill"]
        assert skill.text == (
            "Widen a strict type check\n"
            "Accept numpy scalars in validation.\n"
            "1. Find _validate_params\n"
            "2. Use numbers.Integral"
        )
        assert skill.score == 0.9

    async def test_each_track_names_the_views_that_belong_to_it(self, backend, fake):
        """One identity, two families of view.

        MemOS has no second id to route by, so the tracks are told apart by
        what they ask for: the user track wants the narrative of what
        happened, the agent track wants the agent's own side of it. The
        service validates these names -- an unknown one is a 400 -- and its
        default is the factual view alone, so the agent's half has to be
        asked for by name or it is simply absent.
        """
        await backend.recall("tea", user_id=USER, top_k=5)
        await backend.recall("tea", agent_id=AGENT, top_k=5)
        user_req, agent_req = fake.requests
        assert FakeMemos.body(user_req)["include_memory_view"] == ["detail_factual"]
        assert FakeMemos.body(agent_req)["include_memory_view"] == ["tool_memory", "skill"]
        # One identity: the agent call still owns its rows by ``user_id``.
        assert FakeMemos.body(agent_req)["user_id"] == AGENT

    async def test_a_trajectory_carries_both_what_was_done_and_what_it_taught(self, backend, fake):
        """``tool_value`` is the run, ``experience`` is the rule from it.

        Neither half stands alone: the rule without the run is advice from
        nowhere, and the run without the rule leaves the point to be
        re-derived. Nothing here read either field before.
        """
        fake.tool_memories[AGENT] = [
            {
                "memory_id": "tool-1",
                "relativity": 0.69,
                "tool_type": "ToolTrajectoryMemory",
                "tool_value": "User task: widen the check. -> Execution action: edited _validate_params.",
                "experience": "when a check rejects a numpy scalar, accept numbers.Integral instead.",
            }
        ]
        hits = await backend.recall("types", agent_id=AGENT, top_k=5)
        (tool,) = [h for h in hits if h.metadata["kind"] == "tool"]
        assert tool.text == (
            "User task: widen the check. -> Execution action: edited _validate_params.\n"
            "when a check rejects a numpy scalar, accept numbers.Integral instead."
        )
        assert tool.score == 0.69

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
