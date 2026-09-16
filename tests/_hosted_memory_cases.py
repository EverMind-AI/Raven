"""The unit cases every hosted backend answers the same way.

``tests/test_<service>_memory_backend.py`` mixes :class:`HostedBackendCases` in
with the service's fake and a few extractor hooks, then adds what only that
service does. Assertions land where the fact lives: what the fake received for
request shape, the returned ``Memory`` for mapping, the wall clock for the
turn-path budget, ``caplog`` for what must never be logged.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from raven.contracts.memory import BackendHealth, Memory
from raven.memory_engine.http_backend import RECALL_TIMEOUT_S, HttpMemoryBackend
from raven.plugins import PluginContext, ServiceLocator
from tests._hosted_memory_fakes import FakeCloud, client_for

USER = "alice"
AGENT = "raven-agent"
ALL_ENV_KEYS = ("MEM0_API_KEY", "ZEP_API_KEY", "MEMOS_API_KEY")


def make_ctx(tmp_path: Path, config: dict[str, Any] | None, *, user_id: str = USER) -> PluginContext:
    return PluginContext(
        config=dict(config or {}),
        services=ServiceLocator(workspace=tmp_path, user_id=user_id, agent_id=AGENT),
        logger=logging.getLogger("raven.plugins.hosted-memory"),
    )


class HostedBackendCases:
    fake_cls: type[FakeCloud]
    backend_cls: type[HttpMemoryBackend]

    # ── hooks a service file fills in ─────────────────────────────────

    RECALL_PATH: str
    HEALTH_PATH: str

    def store_path(self, session_id: str) -> str:
        raise NotImplementedError

    def session_path(self, session_id: str) -> str:
        raise NotImplementedError

    def recall_owner(self, request: httpx.Request) -> str:
        raise NotImplementedError

    def store_owner(self, request: httpx.Request) -> str:
        raise NotImplementedError

    def store_session(self, request: httpx.Request) -> str:
        raise NotImplementedError

    def store_messages(self, request: httpx.Request) -> list[dict[str, Any]]:
        return self.fake_cls.body(request)["messages"]

    def delete_kind(self) -> str | None:
        return None

    def prepare_health_ok(self, fake: FakeCloud) -> None:
        return None

    # ── fixtures ─────────────────────────────────────────────────────

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        for var in ALL_ENV_KEYS:
            monkeypatch.delenv(var, raising=False)
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    @pytest.fixture
    def fake(self) -> FakeCloud:
        return self.fake_cls()

    @pytest.fixture
    async def backend(self, fake: FakeCloud):
        b = self.build(fake)
        yield b
        await b.stop()

    def build(self, fake: FakeCloud, config: dict[str, Any] | None = None, **kw: Any) -> HttpMemoryBackend:
        cfg = {"api_key": fake.KEY} if config is None else config
        return self.backend_cls(make_ctx(self.tmp_path, cfg, **kw), client=client_for(fake))

    @property
    def scheme(self) -> str:
        return self.fake_cls.AUTH_SCHEME

    # ── recall ───────────────────────────────────────────────────────

    async def test_recall_request_shape(self, backend, fake):
        fake.seed(USER, "alice likes tea")
        await backend.recall("tea", user_id=USER, top_k=3)
        (req,) = fake.calls("POST", self.RECALL_PATH) or fake.calls("GET", self.RECALL_PATH)
        assert req.headers["Authorization"] == f"{self.scheme} {fake.KEY}"
        assert self.recall_owner(req) == USER

    async def test_recall_owner_is_the_host_identity_not_the_slice(self, fake):
        backend = self.build(fake, {"api_key": fake.KEY, "user_id": "from-slice"})
        await backend.recall("tea", user_id=USER, top_k=3)
        (req,) = fake.requests
        assert self.recall_owner(req) == USER

    async def test_recall_uses_the_owner_of_the_call(self, backend, fake):
        fake.seed("bob", "bob likes coffee")
        fake.seed(USER, "alice likes tea")
        hits = await backend.recall("likes", user_id="bob", top_k=5)
        assert [h.text for h in hits] == ["bob likes coffee"]
        (req,) = fake.requests
        assert self.recall_owner(req) == "bob"

    async def test_store_files_under_the_owner_named_in_metadata(self, backend, fake):
        assert await backend.store("s-1", [{"role": "user", "content": "one"}], metadata={"user_id": "bob"}) is True
        assert await backend.store("s-2", [{"role": "user", "content": "two"}], metadata={"userId": "carol"}) is True
        assert await backend.store("s-3", [{"role": "user", "content": "three"}], metadata={"flush": True}) is True
        assert fake.stored_texts("bob") == ["one"]
        assert fake.stored_texts("carol") == ["two"]
        assert fake.stored_texts(USER) == ["three"]
        owners = [
            self.store_owner(r)
            for r in fake.calls("POST")
            if r.url.path.endswith("/messages") or r.url.path.endswith("/add/") or r.url.path.endswith("/add/message")
        ]
        assert owners == ["bob", "carol", USER]

    async def test_recall_session_uses_the_owner_of_the_call(self, backend, fake):
        fake.seed("bob", "bob's session fact", session="s-1")
        fake.seed(USER, "alice's session fact", session="s-1")
        hits = await backend.recall_session("s-1", user_id="bob")
        assert [h.text for h in hits] == ["bob's session fact"]

    async def test_hit_mapping(self, backend, fake):
        mid = fake.seed(USER, "alice likes tea")
        hits = await backend.recall("tea", user_id=USER, top_k=5)
        assert [h.text for h in hits] == ["alice likes tea"]
        hit = hits[0]
        assert isinstance(hit, Memory)
        assert 0.0 <= hit.score <= 1.0
        assert hit.metadata["id"] == mid
        assert hit.metadata["backend"] == self.backend_cls.NAME

    async def test_agent_track_is_empty_without_a_request(self, backend, fake):
        fake.seed(USER, "alice likes tea")
        assert await backend.recall("tea", agent_id=AGENT, top_k=5) == []
        assert fake.requests == []

    async def test_both_owners_is_a_caller_bug_answered_empty(self, backend, fake):
        assert await backend.recall("tea", user_id=USER, agent_id=AGENT, top_k=5) == []
        assert await backend.recall("tea", top_k=5) == []
        assert fake.requests == []

    async def test_top_k_zero_sends_nothing_and_one_truncates(self, backend, fake):
        fake.seed(USER, "alice likes tea")
        fake.extra_hits = 5
        assert await backend.recall("tea", user_id=USER, top_k=0) == []
        assert fake.requests == []
        assert len(await backend.recall("tea", user_id=USER, top_k=1)) == 1

    async def test_401_is_empty_and_logged_without_the_key(self, backend, fake, caplog):
        fake.fail_next(401)
        with caplog.at_level(logging.WARNING):
            assert await backend.recall("tea", user_id=USER, top_k=5) == []
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert fake.KEY not in caplog.text

    async def test_429_is_empty_without_a_retry_storm(self, backend, fake):
        fake.rate_limit_next()
        assert await backend.recall("tea", user_id=USER, top_k=5) == []
        assert len(fake.requests) <= 2

    @pytest.mark.parametrize("fault", ["status:500", "status:503", "timeout", "malformed"])
    async def test_service_faults_cost_one_recall_not_the_turn(self, backend, fake, fault):
        if fault.startswith("status:"):
            fake.fail_next(int(fault.split(":")[1]))
        elif fault == "timeout":
            fake.timeout_next()
        else:
            fake.malformed_next()
        assert await backend.recall("tea", user_id=USER, top_k=5) == []

    async def test_recall_timeout_is_under_the_host_budget(self, backend, fake):
        await backend.recall("tea", user_id=USER, top_k=5)
        (req,) = fake.requests
        timeout = req.extensions["timeout"]
        assert timeout["read"] <= RECALL_TIMEOUT_S < 5.0
        assert timeout["connect"] <= RECALL_TIMEOUT_S

    # ── store ────────────────────────────────────────────────────────

    async def test_store_request_shape(self, backend, fake):
        ok = await backend.store("sess-1", [{"role": "user", "content": "I like tea"}])
        assert ok is True
        (req,) = fake.calls("POST", self.store_path("sess-1"))
        assert req.headers["Authorization"] == f"{self.scheme} {fake.KEY}"
        assert self.store_owner(req) == USER
        assert self.store_session(req) == "sess-1"

    async def test_store_returns_on_acceptance_without_polling(self, backend, fake):
        started = time.monotonic()
        assert await backend.store("sess-1", [{"role": "user", "content": "I like tea"}]) is True
        assert time.monotonic() - started < 1.0
        assert fake.status_calls == 0

    @pytest.mark.parametrize("fault", ["status:400", "status:422", "status:500", "timeout"])
    async def test_store_failures_are_false_not_raised(self, backend, fake, fault):
        # One good write first, so a service that creates containers on the
        # first store (Zep) has them, and the fault lands on the write itself.
        assert await backend.store("sess-1", [{"role": "user", "content": "warm-up"}]) is True
        if fault == "timeout":
            fake.timeout_next()
        else:
            fake.fail_next(int(fault.split(":")[1]))
        assert await backend.store("sess-1", [{"role": "user", "content": "I like tea"}]) is False

    async def test_empty_batch_is_true_without_a_request(self, backend, fake):
        assert await backend.store("sess-1", []) is True
        assert await backend.store("sess-1", [{"role": "system", "content": "you are raven"}]) is True
        assert await backend.store("sess-1", [{"role": "user", "content": "   "}]) is True
        assert fake.calls("POST", self.store_path("sess-1")) == []

    async def test_unsupported_roles_are_dropped_not_fatal(self, backend, fake):
        ok = await backend.store(
            "sess-1",
            [
                {"role": "tool", "content": "tool output"},
                {"role": "user", "content": "I like tea"},
                {"role": "assistant", "content": [{"type": "text", "text": "noted"}, {"type": "image", "url": "x"}]},
            ],
        )
        assert ok is True
        (req,) = fake.calls("POST", self.store_path("sess-1"))
        sent = self.store_messages(req)
        assert [m["role"] for m in sent] == ["user", "assistant"]
        assert sent[1]["content"] == "noted"

    async def test_store_then_recall_round_trips(self, backend, fake):
        token = "zx91-quartz"
        assert await backend.store("sess-1", [{"role": "user", "content": f"my project is {token}"}]) is True
        hits = await backend.recall(token, user_id=USER, top_k=5)
        assert hits and token in hits[0].text

    async def test_inert_fake_breaks_the_round_trip(self):
        """The switch the round-trip test depends on: a fake that accepts and
        forgets must make the previous test fail, or that test proves nothing."""
        fake = self.fake_cls(inert=True)
        backend = self.build(fake)
        try:
            assert await backend.store("sess-1", [{"role": "user", "content": "my project is zx91"}]) is True
            assert await backend.recall("zx91", user_id=USER, top_k=5) == []
        finally:
            await backend.stop()

    # ── delete / recall_session / feedback ───────────────────────────

    async def test_delete_known_unknown_and_unauthorized(self, backend, fake):
        mid = fake.seed(USER, "alice likes tea")
        kind = self.delete_kind()
        assert await backend.delete(mid, kind=kind) is True
        assert fake.stored_texts(USER) == []
        assert await backend.delete("no-such-id", kind=kind) is False
        fake.fail_next(401)
        assert await backend.delete(mid, kind=kind) is False

    async def test_recall_session_reads_one_conversation(self, backend, fake):
        fake.seed(USER, "first session fact", session="s-1")
        fake.seed(USER, "second session fact", session="s-2")
        hits = await backend.recall_session("s-1", user_id=USER)
        assert [h.text for h in hits] == ["first session fact"]
        assert fake.calls(None, self.session_path("s-1")) or fake.calls("GET", self.session_path("s-1"))
        assert await backend.recall_session("s-none", user_id=USER) == []

    async def test_feedback_is_a_no_op(self, backend, fake):
        await backend.feedback({"used": ["x"]})
        await backend.feedback({})
        assert fake.requests == []

    # ── configuration ────────────────────────────────────────────────

    async def test_base_url_override_and_trailing_slash(self, fake):
        backend = self.build(fake, {"api_key": fake.KEY, "base_url": "https://mirror.test/"})
        await backend.recall("tea", user_id=USER, top_k=1)
        (req,) = fake.requests
        assert str(req.url).startswith("https://mirror.test/")
        assert "//" not in str(req.url).removeprefix("https://")
        await backend.stop()

    async def test_environment_key_beats_the_slice(self, fake):
        self.monkeypatch.setenv(self.backend_cls.ENV_KEY, fake.KEY)
        backend = self.build(fake, {"api_key": "stale-key-from-file"})
        await backend.recall("tea", user_id=USER, top_k=1)
        (req,) = fake.requests
        assert req.headers["Authorization"] == f"{self.scheme} {fake.KEY}"
        await backend.stop()

    async def test_no_key_anywhere_sends_nothing(self, fake):
        backend = self.build(fake, {})
        assert await backend.recall("tea", user_id=USER, top_k=5) == []
        assert await backend.store("s", [{"role": "user", "content": "x"}]) is False
        assert await backend.delete("id") is False
        assert await backend.recall_session("s", user_id=USER) == []
        assert fake.requests == []
        await backend.stop()

    # ── health (table H) ─────────────────────────────────────────────

    @pytest.mark.parametrize("started", [False, True])
    async def test_health_without_a_key_names_the_variable_and_sends_nothing(self, fake, started):
        backend = self.build(fake, {})
        if started:
            await backend.start()
        health = await backend.health()
        assert isinstance(health, BackendHealth)
        assert health.ready is False
        (check,) = health.checks
        assert check.status == "missing"
        assert self.backend_cls.ENV_KEY in (check.hint or "")
        assert self.backend_cls.SIGNUP_URL in (check.hint or "")
        assert fake.requests == []
        await backend.stop()

    @pytest.mark.parametrize("started", [False, True])
    async def test_health_ok(self, backend, fake, started):
        self.prepare_health_ok(fake)
        if started:
            await backend.start()
        health = await backend.health()
        assert health.ready is True
        assert [c.status for c in health.checks] == ["ok"]
        assert fake.calls(None, self.HEALTH_PATH)

    @pytest.mark.parametrize("status", [401, 403])
    async def test_health_rejected_key_is_missing(self, backend, fake, status):
        fake.fail_next(status)
        health = await backend.health()
        assert health.ready is False
        assert health.checks[0].status == "missing"
        assert fake.KEY not in (health.checks[0].hint or "")

    async def test_health_server_error_is_missing(self, backend, fake):
        fake.fail_next(503)
        health = await backend.health()
        assert health.ready is False
        assert health.checks[0].status == "missing"
        assert "503" in (health.checks[0].hint or "")

    async def test_health_unreachable_is_missing_with_the_address(self, backend, fake):
        fake.timeout_next()
        health = await backend.health()
        assert health.ready is False
        assert health.checks[0].status == "missing"
        assert self.backend_cls.DEFAULT_BASE_URL in (health.checks[0].hint or "")

    async def test_health_rate_limited_is_degraded_but_ready(self, backend, fake):
        fake.rate_limit_next()
        health = await backend.health()
        assert health.ready is True
        assert health.checks[0].status == "degraded"

    async def test_health_probe_never_writes(self, backend, fake):
        self.prepare_health_ok(fake)
        before = dict((u, list(rows)) for u, rows in fake.memories.items())
        await backend.health()
        assert fake.memories == before

    async def test_nothing_logs_the_key(self, backend, fake, caplog):
        fake.fail_next(500)
        with caplog.at_level(logging.DEBUG):
            await backend.recall("tea", user_id=USER, top_k=1)
            fake.fail_next(500)
            await backend.store("s", [{"role": "user", "content": "x"}])
            fake.timeout_next()
            await backend.health()
        assert fake.KEY not in caplog.text
        assert os.environ.get(self.backend_cls.ENV_KEY) is None
