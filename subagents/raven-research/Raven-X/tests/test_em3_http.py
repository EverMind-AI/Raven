"""EM-3 — EverosBackend HTTP mode (remote EverOS)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("raven.plugin.memory.everos")

from raven.plugin import PluginContext, ServiceLocator
from raven.plugin.memory.everos.backend import (
    _STORE_BREAKER_THRESHOLD,
    EverosBackend,
    _HttpEverosAdapter,
    _jsonify,
    _NoOpAdapter,
)

# ---------------------------------------------------------------------------
# Mock-transport helpers
# ---------------------------------------------------------------------------


class _MockEverOS:
    """Records requests + emits canned responses for tests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        # Prefixes that 404 — set {"v2"} to emulate a 1.1.x server that
        # only mounts /api/v1 (negotiation fallback path).
        self.not_found_prefixes: set[str] = set()
        self.search_response: dict = {
            "request_id": "test-req",
            "data": {
                "episodes": [],
                "profiles": [],
                "agent_cases": [],
                "agent_skills": [],
            },
        }
        self.add_response: dict = {
            "request_id": "test-req",
            "data": {"message_count": 0, "status": "accumulated"},
        }
        self.status_for_path: dict[str, int] = {}
        # ``/health`` sits outside the versioned mount. ``None`` emulates a
        # server that is not there at all (the transport raises).
        self.health_response: dict | None = {
            "status": "ok",
            "version": "1.2.3",
            "capabilities": {"llm": True, "embed": True, "rerank": True},
            "cascade": {
                "healthy": True,
                "reasons": [],
                "pending": 0,
                "failed_retryable": 0,
                "failed_permanent": 0,
                "drain_consecutive_failures": 0,
            },
        }
        self.health_status: int = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/health":
            if self.health_response is None:
                raise httpx.ConnectError("all connection attempts failed")
            return httpx.Response(self.health_status, json=self.health_response)
        if any(path.startswith(f"/api/{p}/") for p in self.not_found_prefixes):
            return httpx.Response(404, text="not found")
        status = self.status_for_path.get(path, 200)
        if path.endswith("/memory/search"):
            return httpx.Response(status, json=self.search_response)
        if path.endswith("/memory/add"):
            return httpx.Response(status, json=self.add_response)
        if path.endswith("/memory/flush"):
            return httpx.Response(
                status,
                json={"request_id": "test-req", "data": {"status": "extracted"}},
            )
        return httpx.Response(404, text="not found")


@pytest.fixture
def mock():
    return _MockEverOS()


@pytest.fixture
async def http_client(mock):
    client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
    yield client
    await client.aclose()


# ---------------------------------------------------------------------------
# _jsonify recursive converter
# ---------------------------------------------------------------------------


class TestJsonify:
    def test_dict_to_namespace(self) -> None:
        out = _jsonify({"a": 1, "b": "x"})
        assert out.a == 1
        assert out.b == "x"

    def test_nested_dict(self) -> None:
        out = _jsonify({"outer": {"inner": "v"}})
        assert out.outer.inner == "v"

    def test_list_of_dicts(self) -> None:
        out = _jsonify([{"k": 1}, {"k": 2}])
        assert isinstance(out, list)
        assert out[0].k == 1
        assert out[1].k == 2

    def test_dict_with_list_of_dicts(self) -> None:
        out = _jsonify({"episodes": [{"id": "x", "score": 0.5}]})
        assert out.episodes[0].id == "x"
        assert out.episodes[0].score == 0.5

    def test_scalar_passthrough(self) -> None:
        assert _jsonify(3) == 3
        assert _jsonify("s") == "s"
        assert _jsonify(None) is None


# ---------------------------------------------------------------------------
# _HttpEverosAdapter direct tests
# ---------------------------------------------------------------------------


class TestHttpAdapterSearch:
    async def test_posts_to_search_endpoint(
        self,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            client=http_client,
        )
        await adapter.search(
            user_id="alice",
            agent_id=None,
            query="coffee",
            top_k=5,
        )
        assert len(mock.requests) == 1
        req = mock.requests[0]
        assert req.method == "POST"
        assert str(req.url) == "http://mem.test/api/v2/memory/search"
        body = json.loads(req.content.decode())
        # everos's SearchRequest wire contract is user_id XOR agent_id.
        assert body == {
            "user_id": "alice",
            "query": "coffee",
            "top_k": 5,
        }

    async def test_returns_jsonified_data(self, mock, http_client) -> None:
        mock.search_response = {
            "request_id": "x",
            "data": {
                "episodes": [
                    {"id": "ep1", "summary": "hi", "score": 0.7, "session_id": "s1"},
                ],
                "profiles": [],
                "agent_cases": [],
                "agent_skills": [],
            },
        }
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            client=http_client,
        )
        data = await adapter.search(
            user_id="x",
            agent_id=None,
            query="q",
            top_k=5,
        )
        # The host's converter accesses via attributes — verify shape.
        assert data.episodes[0].id == "ep1"
        assert data.episodes[0].summary == "hi"
        assert data.episodes[0].score == pytest.approx(0.7)

    async def test_5xx_raises(self, mock, http_client) -> None:
        mock.status_for_path["/api/v2/memory/search"] = 503
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            client=http_client,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.search(
                user_id="x",
                agent_id=None,
                query="q",
                top_k=5,
            )

    async def test_no_auth_header_when_no_key(
        self,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            client=http_client,
        )
        await adapter.search(
            user_id="x",
            agent_id=None,
            query="q",
            top_k=1,
        )
        # No Authorization header set.
        assert "authorization" not in {h.lower() for h in mock.requests[0].headers}


class TestHttpAdapterAuth:
    async def test_bearer_token_sent(self) -> None:
        mock = _MockEverOS()
        client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            api_key="secret-token",
            client=client,
        )
        await adapter.search(
            user_id="a",
            agent_id=None,
            query="q",
            top_k=1,
        )
        assert mock.requests[0].headers["Authorization"] == "Bearer secret-token"
        await client.aclose()


class TestHttpAdapterMemorize:
    async def test_posts_to_add_endpoint(self, mock, http_client) -> None:
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            client=http_client,
        )
        msgs = [
            {"sender_id": "alice", "role": "user", "timestamp": 1, "content": "hi"},
        ]
        await adapter.memorize("session-1", msgs)
        assert mock.requests[0].method == "POST"
        assert str(mock.requests[0].url).endswith("/api/v2/memory/add")
        body = json.loads(mock.requests[0].content.decode())
        assert body == {"session_id": "session-1", "messages": msgs}

    async def test_5xx_raises(self, mock, http_client) -> None:
        mock.status_for_path["/api/v2/memory/add"] = 500
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            client=http_client,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.memorize(
                "s",
                [
                    {"sender_id": "a", "role": "user", "timestamp": 1, "content": "x"},
                ],
            )


class TestHttpAdapterLifecycle:
    async def test_aclose_idempotent(self) -> None:
        adapter = _HttpEverosAdapter("http://x")
        await adapter.aclose()
        await adapter.aclose()  # second call must not raise

    async def test_injected_client_not_closed(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "request_id": "x",
                        "data": {"episodes": [], "profiles": [], "agent_cases": [], "agent_skills": []},
                    },
                ),
            )
        )
        adapter = _HttpEverosAdapter("http://x", client=client)
        await adapter.aclose()
        # Caller-owned client still usable
        resp = await client.post("http://x/api/v1/memory/search", json={})
        assert resp.status_code == 200
        await client.aclose()


class TestEndpointNormalization:
    async def test_trailing_slash_stripped(self, mock, http_client) -> None:
        adapter = _HttpEverosAdapter(
            "http://mem.test/",
            client=http_client,
        )
        await adapter.search(
            user_id="x",
            agent_id=None,
            query="q",
            top_k=1,
        )
        # No double-slash in path.
        assert str(mock.requests[0].url) == ("http://mem.test/api/v2/memory/search")


# ---------------------------------------------------------------------------
# EverosBackend in mode="http" wires the HTTP adapter end-to-end
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, **config: Any) -> PluginContext:
    return PluginContext(
        config=config,
        services=ServiceLocator(workspace=tmp_path),
    )


class TestBackendHttpMode:
    def test_http_mode_constructs_http_adapter(self, tmp_path: Path) -> None:
        b = EverosBackend(
            _ctx(
                tmp_path,
                mode="http",
                base_url="http://x:9000",
            )
        )
        assert isinstance(b._adapter, _HttpEverosAdapter)
        assert b._adapter._base_url == "http://x:9000"

    def test_base_url_default(self, tmp_path: Path) -> None:
        b = EverosBackend(_ctx(tmp_path, mode="http"))
        assert isinstance(b._adapter, _HttpEverosAdapter)
        assert b._adapter._base_url == "http://127.0.0.1:8000"

    def test_api_key_threaded_through(self, tmp_path: Path) -> None:
        b = EverosBackend(
            _ctx(
                tmp_path,
                mode="http",
                api_key="my-key",
            )
        )
        assert b._adapter._api_key == "my-key"

    async def test_end_to_end_recall_through_http(
        self,
        tmp_path: Path,
    ) -> None:
        """Inject a MockTransport-backed client into a real
        EverosBackend.http adapter and verify the search → recall →
        Memory mapping works end-to-end."""
        mock = _MockEverOS()
        mock.search_response = {
            "request_id": "x",
            "data": {
                "episodes": [],
                "profiles": [],
                "agent_cases": [],
                "agent_skills": [
                    {
                        "id": "sk1",
                        "agent_id": "agent:default",
                        "name": "git-resolver",
                        "description": "resolves git refs",
                        "content": "use git rerere",
                        "confidence": 0.9,
                        "maturity_score": 0.8,
                        "source_case_ids": [],
                        "score": 0.75,
                    },
                ],
            },
        }
        client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
        adapter = _HttpEverosAdapter("http://m", client=client)

        # Build the backend with the explicit adapter
        b = EverosBackend(
            _ctx(tmp_path, mode="http"),
            adapter=adapter,
        )
        hits = await b.recall("git", agent_id="agent:default", top_k=5)

        assert len(hits) == 1
        assert hits[0].text == "use git rerere"
        assert hits[0].metadata["name"] == "git-resolver"
        assert hits[0].metadata["type"] == "skill"
        assert hits[0].score == pytest.approx(0.75)

        await client.aclose()

    async def test_backend_stop_closes_http_adapter(
        self,
        tmp_path: Path,
    ) -> None:
        b = EverosBackend(_ctx(tmp_path, mode="http"))
        # Get a handle to the adapter to verify close happens
        adapter = b._adapter
        assert isinstance(adapter, _HttpEverosAdapter)
        await b.stop()
        # Second stop should not raise even though client is closed
        await b.stop()


# ---------------------------------------------------------------------------
# API version negotiation (v2 canonical, v1 fallback, cached)
# ---------------------------------------------------------------------------


class TestApiVersionNegotiation:
    async def test_auto_negotiates_v2_once(self, mock, http_client) -> None:
        adapter = _HttpEverosAdapter("http://mem.test", client=http_client)
        await adapter.search(user_id="x", agent_id=None, query="q", top_k=1)
        await adapter.search(user_id="x", agent_id=None, query="q", top_k=1)
        paths = [r.url.path for r in mock.requests]
        # No extra probe traffic: both calls go straight to v2.
        assert paths == ["/api/v2/memory/search", "/api/v2/memory/search"]

    async def test_auto_falls_back_to_v1_on_404_and_caches(
        self,
        mock,
        http_client,
    ) -> None:
        mock.not_found_prefixes = {"v2"}  # a 1.1.x server: v1 only
        adapter = _HttpEverosAdapter("http://mem.test", client=http_client)
        await adapter.search(user_id="x", agent_id=None, query="q", top_k=1)
        await adapter.search(user_id="x", agent_id=None, query="q", top_k=1)
        paths = [r.url.path for r in mock.requests]
        # First call probes v2, gets 404, retries v1; second call goes
        # straight to the cached v1.
        assert paths == [
            "/api/v2/memory/search",
            "/api/v1/memory/search",
            "/api/v1/memory/search",
        ]

    async def test_pinned_v1_never_probes(self, mock, http_client) -> None:
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            api_version="v1",
            client=http_client,
        )
        await adapter.memorize(
            "s",
            [{"sender_id": "a", "role": "user", "timestamp": 1, "content": "x"}],
        )
        assert [r.url.path for r in mock.requests] == ["/api/v1/memory/add"]

    async def test_5xx_on_v2_does_not_fall_back(self, mock, http_client) -> None:
        # 404 is the only "prefix absent" signal; a 503 on v2 must raise,
        # not silently reroute the write to v1.
        mock.status_for_path["/api/v2/memory/add"] = 503
        adapter = _HttpEverosAdapter("http://mem.test", client=http_client)
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.memorize(
                "s",
                [{"sender_id": "a", "role": "user", "timestamp": 1, "content": "x"}],
            )
        assert [r.url.path for r in mock.requests] == ["/api/v2/memory/add"]


# ---------------------------------------------------------------------------
# Scope keys + defer_extraction on the wire
# ---------------------------------------------------------------------------


class TestScopeKeys:
    async def test_add_and_flush_carry_scope(self, mock, http_client) -> None:
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            app_id="raven_dr",
            project_id="w302s100_flowon",
            client=http_client,
        )
        await adapter.memorize(
            "s1",
            [{"sender_id": "a", "role": "user", "timestamp": 1, "content": "x"}],
        )
        await adapter.flush("s1")
        add_body = json.loads(mock.requests[0].content.decode())
        flush_body = json.loads(mock.requests[1].content.decode())
        assert add_body["app_id"] == "raven_dr"
        assert add_body["project_id"] == "w302s100_flowon"
        # Flush resolves the buffer by (session, app, project) — the
        # scope must match the add exactly.
        assert flush_body == {
            "session_id": "s1",
            "app_id": "raven_dr",
            "project_id": "w302s100_flowon",
        }

    async def test_search_carries_scope(self, mock, http_client) -> None:
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            app_id="raven_dr",
            project_id="p1",
            client=http_client,
        )
        await adapter.search(user_id="u", agent_id=None, query="q", top_k=1)
        body = json.loads(mock.requests[0].content.decode())
        assert body["app_id"] == "raven_dr"
        assert body["project_id"] == "p1"

    async def test_unset_scope_omitted(self, mock, http_client) -> None:
        adapter = _HttpEverosAdapter("http://mem.test", client=http_client)
        await adapter.memorize(
            "s",
            [{"sender_id": "a", "role": "user", "timestamp": 1, "content": "x"}],
        )
        body = json.loads(mock.requests[0].content.decode())
        assert "app_id" not in body
        assert "project_id" not in body
        assert "defer_extraction" not in body


class TestDeferExtraction:
    async def test_add_carries_defer_flag(self, mock, http_client) -> None:
        adapter = _HttpEverosAdapter(
            "http://mem.test",
            defer_extraction=True,
            client=http_client,
        )
        await adapter.memorize(
            "s",
            [{"sender_id": "a", "role": "user", "timestamp": 1, "content": "x"}],
        )
        body = json.loads(mock.requests[0].content.decode())
        assert body["defer_extraction"] is True

    async def test_backend_store_never_flushes_when_deferred(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter(
            "http://m",
            defer_extraction=True,
            client=http_client,
        )
        b = EverosBackend(
            _ctx(
                tmp_path,
                mode="http",
                defer_extraction=True,
                flush_every_turns=1,
            ),
            adapter=adapter,
        )
        await b.store("s", [{"role": "user", "content": "hello"}])
        paths = [r.url.path for r in mock.requests]
        # flush_every_turns=1 would flush every store; defer_extraction
        # overrides — capture is one /add and nothing else.
        assert paths == ["/api/v2/memory/add"]

    async def test_eager_store_flushes_explicitly(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=1),
            adapter=adapter,
        )
        await b.store("s", [{"role": "user", "content": "hello"}])
        paths = [r.url.path for r in mock.requests]
        # add/flush decoupled in the adapter; the backend still flushes
        # per its eager cadence — same wire sequence as before.
        assert paths == ["/api/v2/memory/add", "/api/v2/memory/flush"]

    async def test_explicit_backend_flush(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter(
            "http://m",
            defer_extraction=True,
            client=http_client,
        )
        b = EverosBackend(
            _ctx(tmp_path, mode="http", defer_extraction=True),
            adapter=adapter,
        )
        await b.store("s", [{"role": "user", "content": "hello"}])
        await b.flush("s")
        paths = [r.url.path for r in mock.requests]
        assert paths == ["/api/v2/memory/add", "/api/v2/memory/flush"]

    async def test_backend_advertises_the_flush_capability(self, tmp_path: Path) -> None:
        """The host promotes at a task boundary only if its capability
        check recognises the backend; a mismatch here would make
        ``AgentLoop._dispatch_backend_flush`` skip silently."""
        from raven.memory_engine import FlushableBackend, MemoryBackend

        b = EverosBackend(
            _ctx(tmp_path, mode="http", defer_extraction=True),
            adapter=_NoOpAdapter(),
        )
        assert isinstance(b, MemoryBackend)
        assert isinstance(b, FlushableBackend)

    async def test_flush_by_host_key_resolves_the_generated_session(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        """The host knows only its own session key, so flush has to map it
        through the same table ``store`` used — otherwise the promotion
        names a session id that never received a write."""
        adapter = _HttpEverosAdapter(
            "http://m",
            defer_extraction=True,
            client=http_client,
        )
        b = EverosBackend(
            _ctx(
                tmp_path,
                mode="http",
                defer_extraction=True,
                session_id_prefix="raven_dr",
            ),
            adapter=adapter,
        )
        await b.store("mock:c1", [{"role": "user", "content": "hello"}])
        await b.flush("mock:c1")

        added, flushed = (json.loads(r.content.decode()) for r in mock.requests)
        assert flushed["session_id"] == added["session_id"]
        assert flushed["session_id"].startswith("raven_dr_")

    async def test_deferred_sessions_recorded_for_post_hoc_flush(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter(
            "http://m",
            app_id="raven_dr",
            project_id="p1",
            defer_extraction=True,
            client=http_client,
        )
        b = EverosBackend(
            _ctx(
                tmp_path,
                mode="http",
                defer_extraction=True,
                app_id="raven_dr",
                project_id="p1",
                session_id_prefix="raven_dr",
            ),
            adapter=adapter,
        )
        await b.store("host-key", [{"role": "user", "content": "x"}])
        await b.store("host-key", [{"role": "user", "content": "y"}])
        sidecar = tmp_path / ".everos_sessions.jsonl"
        rows = [json.loads(line) for line in sidecar.read_text().splitlines()]
        assert len(rows) == 1  # one host session -> one record
        assert rows[0]["app_id"] == "raven_dr"
        assert rows[0]["project_id"] == "p1"
        assert rows[0]["session_id"].startswith("raven_dr_")
        # The generated id is what actually went on the wire.
        sent = json.loads(mock.requests[0].content.decode())["session_id"]
        assert sent == rows[0]["session_id"]


# ---------------------------------------------------------------------------
# Client-side chunking + capture caps (the 422 face of the server DTO)
# ---------------------------------------------------------------------------


class TestClientSideChunking:
    async def test_501_messages_split_into_two_adds(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=0),
            adapter=adapter,
        )
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(501)]
        await b.store("s", msgs)
        adds = [r for r in mock.requests if r.url.path.endswith("/memory/add")]
        assert len(adds) == 2
        first = json.loads(adds[0].content.decode())["messages"]
        second = json.loads(adds[1].content.decode())["messages"]
        assert len(first) == 500
        assert len(second) == 1

    async def test_synthesized_timestamps_unique_across_chunks(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        # message_id is (session, timestamp_ms, index-within-request);
        # a shared fill-in timestamp makes chunk 2 collide with chunk 1
        # and be dropped server-side as duplicates. Timestamps must be
        # strictly increasing across the whole trajectory.
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=0),
            adapter=adapter,
        )
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(501)]
        await b.store("s", msgs)
        adds = [r for r in mock.requests if r.url.path.endswith("/memory/add")]
        ts = [
            m["timestamp"]
            for r in adds
            for m in json.loads(r.content.decode())["messages"]
        ]
        assert len(ts) == 501
        assert len(set(ts)) == 501
        assert ts == sorted(ts)


class TestToolCallPairing:
    """The server maps a tool row to a ToolCallResult and raises when it
    has nothing to pair on, failing the session's whole /flush with a
    500 — so an unpairable row must not reach the buffer."""

    async def test_tool_message_without_id_is_dropped(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=0),
            adapter=adapter,
        )
        await b.store(
            "s",
            [
                {"role": "user", "content": "q"},
                {"role": "tool", "content": "orphan result", "name": "web_fetch"},
                {"role": "assistant", "content": "a"},
            ],
        )
        roles = [m["role"] for m in json.loads(mock.requests[0].content.decode())["messages"]]
        assert roles == ["user", "assistant"]

    async def test_paired_tool_message_is_kept_with_its_id(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=0),
            adapter=adapter,
        )
        await b.store(
            "s",
            [{"role": "tool", "content": "result", "tool_call_id": "call_1"}],
        )
        sent = json.loads(mock.requests[0].content.decode())["messages"]
        assert [(m["role"], m["tool_call_id"]) for m in sent] == [("tool", "call_1")]


class TestTimestampCoercion:
    async def test_iso_string_timestamps_coerced_to_ms_ints(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        # AgentLoop stamps messages with datetime.isoformat() strings —
        # and stamps adjacent records with the SAME string. The server
        # 422s any non-int timestamp (rejecting the whole /add, which
        # the fail-open store swallows), and dedups on
        # (session, timestamp_ms, index-within-request).
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=0),
            adapter=adapter,
        )
        stamp = "2026-08-19T08:48:33.545594"
        await b.store(
            "s",
            [
                {"role": "user", "content": "q", "timestamp": stamp},
                {"role": "assistant", "content": "a", "timestamp": stamp},
                {
                    "role": "tool",
                    "content": "t",
                    "timestamp": "not-a-date",
                    "tool_call_id": "c1",
                },
            ],
        )
        body = json.loads(mock.requests[0].content.decode())
        ts = [m["timestamp"] for m in body["messages"]]
        assert all(isinstance(t, int) for t in ts)
        assert len(set(ts)) == len(ts)
        assert ts == sorted(ts)
        expected = int(datetime.fromisoformat(stamp).timestamp() * 1000)
        assert ts[0] == expected
        assert ts[1] == expected + 1

    async def test_int_timestamps_pass_through(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=0),
            adapter=adapter,
        )
        await b.store(
            "s",
            [
                {"role": "user", "content": "q", "timestamp": 1_000},
                {"role": "assistant", "content": "a", "timestamp": 2_000},
            ],
        )
        body = json.loads(mock.requests[0].content.decode())
        assert [m["timestamp"] for m in body["messages"]] == [1_000, 2_000]

    async def test_monotonic_across_store_calls(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        # Two same-ms fallback fills in consecutive store() calls would
        # reuse a (session, ts_ms, index) dedup key and the second
        # message would be silently dropped server-side.
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=0),
            adapter=adapter,
        )
        await b.store("s", [{"role": "user", "content": "a"}])
        await b.store(
            "s",
            [
                {"role": "user", "content": "b"},
                {"role": "assistant", "content": "c"},
            ],
        )
        ts = [
            m["timestamp"]
            for r in mock.requests
            if r.url.path.endswith("/memory/add")
            for m in json.loads(r.content.decode())["messages"]
        ]
        assert len(ts) == 3
        assert len(set(ts)) == 3
        assert ts == sorted(ts)


class _ScriptedAdapter:
    """memorize() outcomes scripted as booleans (False = raise)."""

    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def memorize(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        is_final: bool = False,
    ) -> None:
        self.calls += 1
        if self.outcomes and not self.outcomes.pop(0):
            raise RuntimeError("scripted failure")


class TestStoreBreaker:
    async def test_breaker_opens_after_consecutive_failures(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _ScriptedAdapter([False] * 10)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=0),
            adapter=adapter,
        )
        for _ in range(8):
            await b.store("s", [{"role": "user", "content": "x"}])
        assert adapter.calls == _STORE_BREAKER_THRESHOLD

    async def test_success_resets_the_streak(self, tmp_path: Path) -> None:
        outcomes = [False] * 4 + [True] + [False] * 4
        adapter = _ScriptedAdapter(outcomes)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=0),
            adapter=adapter,
        )
        for _ in range(len(outcomes)):
            await b.store("s", [{"role": "user", "content": "x"}])
        # Neither streak reached the threshold — every call dispatched.
        assert adapter.calls == len(outcomes)


class TestCaptureMaxChars:
    async def test_content_clipped(self, tmp_path: Path, mock, http_client) -> None:
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(
                tmp_path,
                mode="http",
                flush_every_turns=0,
                capture_max_chars=10,
            ),
            adapter=adapter,
        )
        await b.store("s", [{"role": "user", "content": "x" * 100}])
        body = json.loads(mock.requests[0].content.decode())
        assert body["messages"][0]["content"] == "x" * 10


class TestEmitUserMessages:
    async def test_user_messages_omitted_when_disabled(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(
                tmp_path,
                mode="http",
                flush_every_turns=0,
                emit_user_messages=False,
            ),
            adapter=adapter,
        )
        await b.store(
            "s",
            [
                {"role": "user", "content": "forwarded question"},
                {"role": "assistant", "content": "finding"},
            ],
        )
        body = json.loads(mock.requests[0].content.decode())
        assert [m["role"] for m in body["messages"]] == ["assistant"]


# ---------------------------------------------------------------------------
# D8 store-only gate + client-side PathSafeId pre-validation
# ---------------------------------------------------------------------------


class TestRecallGateConfig:
    def test_recall_enabled_defaults_true(self, tmp_path: Path) -> None:
        b = EverosBackend(_ctx(tmp_path, mode="http"))
        assert b.recall_enabled is True

    def test_recall_enabled_false_from_config(self, tmp_path: Path) -> None:
        b = EverosBackend(_ctx(tmp_path, mode="http", recall_enabled=False))
        assert b.recall_enabled is False


class TestPathSafeValidation:
    def test_invalid_project_id_fails_at_construction(
        self,
        tmp_path: Path,
    ) -> None:
        # Server-side this is a 422 swallowed by the fail-open store —
        # the whole batch would silently capture nothing.
        with pytest.raises(ValueError, match="project_id"):
            EverosBackend(_ctx(tmp_path, mode="http", project_id="w302:flowon"))

    def test_colon_agent_id_fails_at_construction(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="agent_id"):
            EverosBackend(_ctx(tmp_path, mode="http", agent_id="agent:default"))

    def test_invalid_api_version_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="api_version"):
            EverosBackend(_ctx(tmp_path, mode="http", api_version="v3"))

    def test_embedded_defer_extraction_fails_at_construction(
        self,
        tmp_path: Path,
    ) -> None:
        # The embedded adapter exposes no flush and the post-batch flush
        # script only speaks HTTP — a deferred buffer in embedded mode
        # could never be promoted.
        with pytest.raises(ValueError, match="defer_extraction"):
            EverosBackend(_ctx(tmp_path, mode="embedded", defer_extraction=True))

    async def test_user_sender_id_sanitized_not_dropped(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        # Per-message sender ids can't fail fast mid-turn (fail-open
        # would eat the raise) — they are coerced into the charset.
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", flush_every_turns=0),
            adapter=adapter,
        )
        await b.store(
            "s",
            [{"role": "user", "content": "x", "sender_id": "channel:chat_id"}],
        )
        body = json.loads(mock.requests[0].content.decode())
        assert body["messages"][0]["sender_id"] == "channel_chat_id"


# ---------------------------------------------------------------------------
# Start-time service probe (ported from upstream, minus the spawner)
# ---------------------------------------------------------------------------


class TestServiceProbe:
    async def test_healthy_service_probes_and_records(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(_ctx(tmp_path, mode="http"), adapter=adapter)
        await b.start()
        assert b._service_checked is True
        assert [r.url.path for r in mock.requests] == ["/health"]

    async def test_absent_service_warns_and_continues(
        self,
        tmp_path: Path,
        mock,
        http_client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fail-open by default: the turn is already durable in the session
        log, so an absent memory service must not cost the user an answer."""
        mock.health_response = None
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(_ctx(tmp_path, mode="http"), adapter=adapter)
        await b.start()  # must not raise
        assert b._service_checked is False
        assert "not usable" in caplog.text
        assert "refused" in caplog.text

    async def test_require_service_raises_when_absent(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        from raven.memory_engine.backend import MemoryServiceUnavailableError

        mock.health_response = None
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", require_service=True),
            adapter=adapter,
        )
        with pytest.raises(MemoryServiceUnavailableError, match="refused"):
            await b.start()

    async def test_degraded_service_is_named_not_just_reachable(
        self,
        tmp_path: Path,
        mock,
        http_client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A 200 stopped implying a working install at everos 1.2.1. A server
        that answers but cannot embed silently degrades recall to keyword
        matching, which reads as an agent that merely learned nothing."""
        mock.health_response = {
            "version": "1.2.3",
            "capabilities": {"llm": True, "embed": False},
            "cascade": {"healthy": True, "failed_retryable": 0},
        }
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(_ctx(tmp_path, mode="http"), adapter=adapter)
        await b.start()
        assert "degraded" in caplog.text
        assert "embedding unavailable" in caplog.text

    async def test_cascade_retries_are_reported(
        self,
        tmp_path: Path,
        mock,
        http_client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The signal a revoked embedding key actually produces: capabilities
        still report embed=true (it was configured), and the failure only
        shows up as cascade items retrying."""
        mock.health_response = {
            "version": "1.2.3",
            "capabilities": {"llm": True, "embed": True},
            "cascade": {"healthy": True, "failed_retryable": 3, "failed_permanent": 4},
        }
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(_ctx(tmp_path, mode="http"), adapter=adapter)
        await b.start()
        assert "3 cascade item(s) failing" in caplog.text
        # Historical permanents are not actionable and must not warn -- they
        # would fire on every start forever.
        assert "failed_permanent" not in caplog.text

    async def test_health_error_status_is_not_ok(
        self,
        tmp_path: Path,
        mock,
        http_client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock.health_status = 503
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(_ctx(tmp_path, mode="http"), adapter=adapter)
        await b.start()
        assert b._service_checked is False
        assert "error" in caplog.text

    async def test_noop_adapter_is_not_probed(self, tmp_path: Path) -> None:
        """Embedded / no-op adapters expose no /health to ask."""
        b = EverosBackend(_ctx(tmp_path, mode="http"), adapter=_NoOpAdapter())
        await b.start()
        assert b._service_checked is False


# ---------------------------------------------------------------------------
# Recall tuning on the wire (SearchRequest sets extra="forbid")
# ---------------------------------------------------------------------------


class TestRecallTuning:
    async def test_method_and_rerank_omitted_by_default(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        """Default request stays byte-identical to before these knobs existed."""
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(_ctx(tmp_path, mode="http"), adapter=adapter)
        await b.recall("q", agent_id="a", top_k=3)
        body = json.loads(mock.requests[0].content.decode())
        assert "method" not in body
        assert "enable_llm_rerank" not in body

    async def test_method_and_rerank_sent_when_configured(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter(
            "http://m",
            recall_method="vector",
            enable_llm_rerank=True,
            client=http_client,
        )
        b = EverosBackend(_ctx(tmp_path, mode="http"), adapter=adapter)
        await b.recall("q", agent_id="a", top_k=3)
        body = json.loads(mock.requests[0].content.decode())
        assert body["method"] == "vector"
        assert body["enable_llm_rerank"] is True

    def test_invalid_recall_method_rejected_at_construction(self, tmp_path: Path) -> None:
        """A bad value is a 422 the fail-open recall path would swallow,
        leaving recall silently empty for the whole run."""
        with pytest.raises(ValueError, match="invalid recall_method"):
            EverosBackend(_ctx(tmp_path, mode="http", recall_method="fuzzy"))

    def test_config_threads_tuning_into_the_adapter(self, tmp_path: Path) -> None:
        b = EverosBackend(
            _ctx(tmp_path, mode="http", recall_method="hybrid", enable_llm_rerank=True),
        )
        assert b._adapter._recall_method == "hybrid"
        assert b._adapter._enable_llm_rerank is True


# ---------------------------------------------------------------------------
# Agent-lane warm-up
# ---------------------------------------------------------------------------


class TestRecallWarmup:
    async def test_warmup_fires_when_skill_recall_is_on(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(_ctx(tmp_path, mode="http", agent_id="agt"), adapter=adapter)
        await b.start()
        assert b._warm_task is not None
        await b._warm_task
        searches = [r for r in mock.requests if r.url.path.endswith("/memory/search")]
        assert len(searches) == 1
        body = json.loads(searches[0].content.decode())
        assert body["agent_id"] == "agt"

    async def test_no_warmup_when_recall_is_off(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        """A store-only capture profile must issue no search at all -- that is
        what keeps it byte- and latency-identical to memory.backend=null."""
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", recall_enabled=False),
            adapter=adapter,
        )
        await b.start()
        assert b._warm_task is None
        assert not [r for r in mock.requests if r.url.path.endswith("/memory/search")]

    async def test_no_warmup_when_only_skills_lane_is_off(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(
            _ctx(tmp_path, mode="http", recall_skills_enabled=False),
            adapter=adapter,
        )
        await b.start()
        assert b._warm_task is None

    async def test_warmup_failure_is_swallowed(
        self,
        tmp_path: Path,
        mock,
        http_client,
    ) -> None:
        mock.status_for_path["/api/v2/memory/search"] = 500
        adapter = _HttpEverosAdapter("http://m", client=http_client)
        b = EverosBackend(_ctx(tmp_path, mode="http"), adapter=adapter)
        await b.start()
        assert b._warm_task is not None
        await b._warm_task  # must not raise


class TestRetryPolicy:
    """Retry covers only what the server provably did not act on.

    The rule is not "did it fail" -- a 500 from /flush is the server-side
    cancel that already committed memcells and drained the buffer, and
    re-sending it answers no_extraction while hiding the loss.
    """

    def _adapter(self, handler, **kw):
        from raven.plugin.memory.everos.backend import _HttpEverosAdapter

        return _HttpEverosAdapter(
            "http://everos.test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            **kw,
        )

    async def test_429_is_retried_and_then_succeeds(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(429, json={"detail": "quota"})
            return httpx.Response(200, json={"request_id": "r", "data": {}})

        a = self._adapter(handler, retries=3)
        r = await a._post_memory("add", {})
        assert r.status_code == 200
        assert len(calls) == 3

    async def test_429_gives_up_after_the_budget(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, json={"detail": "quota"})

        a = self._adapter(handler, retries=2)
        with pytest.raises(httpx.HTTPStatusError):
            await a._post_memory("add", {})
        assert len(calls) == 3, "one initial attempt plus two retries"

    async def test_a_500_on_flush_is_never_retried(self) -> None:
        """The one failure a retry must not paper over: the buffer is already
        drained, so a second flush answers no_extraction."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(500, text="cancelled")

        a = self._adapter(handler, retries=3)
        with pytest.raises(httpx.HTTPStatusError):
            await a._post_memory("flush", {})
        assert len(calls) == 1

    async def test_a_read_timeout_is_never_retried(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ReadTimeout("too slow")

        a = self._adapter(handler, retries=3)
        with pytest.raises(httpx.ReadTimeout):
            await a._post_memory("flush", {})
        assert len(calls) == 1

    async def test_a_connect_error_is_retried(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 2:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json={"request_id": "r", "data": {}})

        a = self._adapter(handler, retries=2)
        assert (await a._post_memory("add", {})).status_code == 200
        assert len(calls) == 2

    async def test_retries_zero_is_a_pass_through(self) -> None:
        """A local service must keep its exact single-attempt behaviour."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, json={"detail": "quota"})

        a = self._adapter(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await a._post_memory("add", {})
        assert len(calls) == 1


class TestRetryAfter:
    def _resp(self, value: str | None) -> httpx.Response:
        headers = {"Retry-After": value} if value is not None else {}
        return httpx.Response(429, headers=headers)

    def _parse(self, value: str | None, timeout_s: float = 600.0) -> float | None:
        # A large timeout by default so these cases exercise the absolute cap
        # rather than the per-request one; the budget interaction has its own
        # tests below.
        a = _HttpEverosAdapter("http://x", timeout_s=timeout_s)
        return a._parse_retry_after(self._resp(value))

    def test_absent_header_means_fall_back_to_backoff(self) -> None:
        assert self._parse(None) is None

    def test_delta_seconds(self) -> None:
        assert self._parse("7") == 7.0

    def test_it_is_capped(self) -> None:
        """Awaited inside the turn on the recall path, so an hour published by
        a rate limiter must not park the turn."""
        from raven.plugin.memory.everos.backend import _RETRY_AFTER_CAP_S

        assert self._parse("3600") == _RETRY_AFTER_CAP_S

    def test_the_cap_is_also_bounded_by_the_request_budget(self) -> None:
        """This sleep happens outside httpx's timeout, so a 30s ceiling against
        a 10s per-request budget would let two retries park a recall for 60s in
        a turn whose own request budget was 10s."""
        assert self._parse("3600", timeout_s=10.0) == 10.0
        assert self._parse("2", timeout_s=10.0) == 2.0

    def test_a_tiny_budget_still_leaves_a_usable_floor(self) -> None:
        assert self._parse("3600", timeout_s=0.1) == 1.0

    def test_nan_is_rejected_not_slept_on(self) -> None:
        """float() accepts "nan", and min/max of nan is still nan -- which
        would reach asyncio.sleep(nan)."""
        assert self._parse("nan") is None

    def test_inf_is_capped_not_rejected(self) -> None:
        from raven.plugin.memory.everos.backend import _RETRY_AFTER_CAP_S

        assert self._parse("inf") == _RETRY_AFTER_CAP_S
        assert self._parse("-inf") == 0.0

    def test_an_http_date_is_understood(self) -> None:
        """The date form would otherwise read as "no header" and be silently
        replaced by a guess."""
        import email.utils
        from datetime import datetime, timedelta, timezone

        when = datetime.now(timezone.utc) + timedelta(seconds=10)
        got = self._parse(email.utils.format_datetime(when))
        assert got is not None and 5.0 <= got <= 12.0

    def test_a_past_date_clamps_to_zero(self) -> None:
        import email.utils
        from datetime import datetime, timedelta, timezone

        when = datetime.now(timezone.utc) - timedelta(hours=1)
        assert self._parse(email.utils.format_datetime(when)) == 0.0

    def test_garbage_is_ignored(self) -> None:
        assert self._parse("soon please") is None

    async def test_the_published_delay_is_used_instead_of_a_guess(self) -> None:
        """Guessing when the server has published a value is how a client turns
        a rate limit into a longer one."""
        from raven.plugin.memory.everos.backend import _HttpEverosAdapter

        slept: list[float] = []
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 2:
                return httpx.Response(429, headers={"Retry-After": "3"})
            return httpx.Response(200, json={"request_id": "r", "data": {}})

        a = _HttpEverosAdapter(
            "http://everos.test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            retries=2,
            timeout_s=600.0,
        )
        import raven.plugin.memory.everos.backend as mod

        real_sleep = mod.asyncio.sleep

        async def fake_sleep(d: float) -> None:
            slept.append(d)
            await real_sleep(0)

        mod.asyncio.sleep = fake_sleep
        try:
            await a._post_memory("add", {})
        finally:
            mod.asyncio.sleep = real_sleep
        assert slept == [3.0], f"expected the published 3s, not a guess: {slept}"


from raven.memory_engine import MemoryApiVersionError

# The Cloud's actual refusal, copied from api.evermind.ai on 2026-08-21.
_CLOUD_VERSION_REFUSAL = {
    "error": {
        "code": "VERSION_NOT_ALLOWED",
        "message": "This account (memory API v1) is not allowed to call the v2 memory API.",
        "type": "api_error",
    },
}


class TestVersionNegotiationAgainstTheCloud:
    """The Cloud mounts v2 and refuses it per-account with 403, not 404.

    Negotiating on 404 alone pinned v2 and gave up, so an account provisioned
    for v1 could never reach memory at all -- while every call still returned
    "successfully" through the fail-open paths.
    """

    def _adapter(self, handler, **kw):
        from raven.plugin.memory.everos.backend import _HttpEverosAdapter

        return _HttpEverosAdapter(
            "https://api.evermind.test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            **kw,
        )

    async def test_a_version_refusal_does_not_retry_v1(self) -> None:
        """Measured: the Cloud's legacy v1 lives at /api/v1/memories/* (401 =
        present) while /api/v1/memory/* -- the path this adapter builds -- is
        404. Falling back replaced a precise diagnosis with a bare 404."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(403, json=_CLOUD_VERSION_REFUSAL)

        a = self._adapter(handler)
        with pytest.raises(MemoryApiVersionError):
            await a._post_memory("search", {})
        assert seen == ["/api/v2/memory/search"], "no fallback to a path that cannot exist"
        assert a._api_prefix is None, "nothing to cache"

    async def test_the_refusal_carries_the_servers_own_wording(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json=_CLOUD_VERSION_REFUSAL)

        a = self._adapter(handler)
        with pytest.raises(MemoryApiVersionError) as ei:
            await a._post_memory("search", {})
        msg = str(ei.value)
        assert "not allowed to call the v2 memory API" in msg, "server wording must survive"
        assert "/api/v1/memories/*" in msg, "must name where the legacy API actually is"
        assert "enabled for v2" in msg, "must name the one action that resolves it"

    async def test_a_plain_403_is_not_a_version_error(self) -> None:
        """Otherwise every authentication failure would be reported as a
        version misconfiguration and send the operator to the wrong fix."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(403, json={"detail": "forbidden"})

        a = self._adapter(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await a._post_memory("search", {})
        assert seen == ["/api/v2/memory/search"]

    async def test_a_403_with_an_unparseable_body_is_not_a_version_refusal(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(403, text="<html>nope</html>")

        a = self._adapter(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await a._post_memory("search", {})
        assert seen == ["/api/v2/memory/search"]

    async def test_404_still_falls_back(self) -> None:
        """The self-hosted 1.1.x path must keep working."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if "/api/v2/" in request.url.path:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json={"request_id": "r", "data": {}})

        a = self._adapter(handler)
        assert (await a._post_memory("search", {})).status_code == 200
        assert a._api_prefix == "v1"

    async def test_a_pinned_version_refusal_is_explained_not_negotiated(self, caplog) -> None:
        """api_version="v2" is an explicit choice, so there is nothing to
        negotiate -- but the bare 403 would not say the fix is one config line."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(403, json=_CLOUD_VERSION_REFUSAL)

        a = self._adapter(handler, api_version="v2")
        with pytest.raises(MemoryApiVersionError):
            await a._post_memory("search", {})
        assert seen == ["/api/v2/memory/search"], "a pinned prefix must not fall back"


class TestAsyncModeAndQueuedWrites:
    """The Cloud queues /add by default; the self-hosted server applies it.

    Measured on everos 1.2.3: async_mode=false is accepted (no 422) but turns a
    ~0s /add into a ~33s one, so it cannot be the default -- and on the Cloud,
    omitting it means a 202 that nothing downstream distinguishes from an
    applied write.
    """

    def _adapter(self, handler, **kw):
        return _HttpEverosAdapter(
            "https://api.evermind.test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            **kw,
        )

    def _body_of(self, request: httpx.Request) -> dict:
        return json.loads(request.content.decode())

    async def test_the_field_is_omitted_when_unset(self) -> None:
        """Omission is what keeps each deployment's own default."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"request_id": "r", "data": {}})

        a = self._adapter(handler)
        await a.memorize("s", [])
        assert "async_mode" not in self._body_of(seen[0])

    @pytest.mark.parametrize("value", [True, False])
    async def test_an_explicit_value_is_sent(self, value: bool) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"request_id": "r", "data": {}})

        a = self._adapter(handler, async_mode=value)
        await a.memorize("s", [])
        assert self._body_of(seen[0])["async_mode"] is value

    async def test_a_202_is_not_an_error_but_is_reported(self, caplog) -> None:
        """raise_for_status accepts 202, so without this the ordering hazard
        would be completely silent."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(202, json={"request_id": "r", "data": {"status": "queued"}})

        a = self._adapter(handler)
        with caplog.at_level("WARNING"):
            await a.memorize("s", [])
        assert "queued" in caplog.text
        assert "async_mode=false" in caplog.text, "must name the knob that fixes it"

    async def test_the_202_warning_is_once_per_adapter(self, caplog) -> None:
        """One line per turn for a condition that cannot change mid-process
        reads as noise and gets skimmed past."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(202, json={"request_id": "r", "data": {}})

        a = self._adapter(handler)
        with caplog.at_level("WARNING"):
            for _ in range(3):
                await a.memorize("s", [])
        assert caplog.text.count("queued, not applied") == 1

    async def test_a_200_says_nothing(self, caplog) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"request_id": "r", "data": {}})

        a = self._adapter(handler)
        with caplog.at_level("WARNING"):
            await a.memorize("s", [])
        assert "queued" not in caplog.text


class TestAsyncModeConfig:
    def test_a_non_boolean_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="async_mode"):
            EverosBackend(PluginContext(
                config={"mode": "http", "async_mode": "false"},
                services=ServiceLocator(workspace=tmp_path),
            ))

    def test_unset_stays_none(self, tmp_path: Path) -> None:
        b = EverosBackend(PluginContext(
            config={"mode": "http"},
            services=ServiceLocator(workspace=tmp_path),
        ))
        assert b._adapter._async_mode is None


class TestVersionProbeAtStart:
    """``require_service`` must catch a version-refused account at boot.

    /health is unauthenticated on the Cloud (a wrong key still gets
    ``{"message": "ok"}``), so the health probe alone cannot see this.
    """

    def _ctx(self, tmp_path: Path, **cfg: Any) -> PluginContext:
        return PluginContext(
            config={"mode": "http", "base_url": "https://api.evermind.test", **cfg},
            services=ServiceLocator(workspace=tmp_path),
        )

    def _backend(self, tmp_path: Path, handler, **cfg: Any) -> EverosBackend:
        b = EverosBackend(self._ctx(tmp_path, **cfg))
        b._adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return b

    def _refusing(self, seen: list[httpx.Request]):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/health":
                return httpx.Response(200, json={"message": "ok", "service": "gateway"})
            return httpx.Response(403, json=_CLOUD_VERSION_REFUSAL)

        return handler

    async def test_require_service_fails_the_run(self, tmp_path: Path) -> None:
        """The bug this replaced: a bare RuntimeError was swallowed by the
        CLI's broad except and the job ran for an hour writing nothing."""
        from raven.memory_engine import MemoryServiceUnavailableError

        seen: list[httpx.Request] = []
        b = self._backend(tmp_path, self._refusing(seen), api_key="k", require_service=True)
        with pytest.raises(MemoryServiceUnavailableError):
            await b.start()

    async def test_without_require_service_it_degrades(self, tmp_path: Path, caplog) -> None:
        seen: list[httpx.Request] = []
        b = self._backend(tmp_path, self._refusing(seen), api_key="k")
        with caplog.at_level("ERROR"):
            await b.start()
        assert "enabled for v2" in caplog.text

    async def test_the_probe_is_skipped_without_a_credential(self, tmp_path: Path) -> None:
        """Self-hosted with no key has no account, so no per-account version
        provisioning to hit -- and start() stays one /health request."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"status": "ok", "version": "1.2.3"})

        b = self._backend(tmp_path, handler)
        await b.start()
        assert [r.url.path for r in seen] == ["/health"]

    async def test_the_probe_does_not_carry_method_or_rerank(self, tmp_path: Path) -> None:
        """Routing it through search() would make method="agentic" + rerank
        fire a synchronous LLM call inside start()."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "1.2.3"})
            return httpx.Response(200, json={"request_id": "r", "data": {}})

        b = self._backend(
            tmp_path, handler, api_key="k",
            recall_method="agentic", enable_llm_rerank=True,
        )
        await b.start()
        probes = [r for r in seen if r.url.path.endswith("/memory/search")]
        assert probes, "the probe must have run"
        body = json.loads(probes[0].content.decode())
        assert "method" not in body, f"probe carried a retrieval method: {body}"
        assert "enable_llm_rerank" not in body, f"probe carried rerank: {body}"

    async def test_an_unrelated_failure_does_not_fail_start(self, tmp_path: Path) -> None:
        """A probe must not be stricter than the operation it probes."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "1.2.3"})
            return httpx.Response(422, json={"detail": "empty query"})

        b = self._backend(tmp_path, handler, api_key="k", require_service=True)
        await b.start()


class TestRecallFailureAccounting:
    """Recall fails open by returning [], which every call site reads as
    "nothing matched". The counter is the only thing that distinguishes them."""

    def _backend(self, tmp_path: Path, handler, **cfg: Any) -> EverosBackend:
        b = EverosBackend(PluginContext(
            config={"mode": "http", "base_url": "https://api.evermind.test", **cfg},
            services=ServiceLocator(workspace=tmp_path),
        ))
        b._adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return b

    async def test_a_failed_recall_is_counted(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        b = self._backend(tmp_path, handler)
        assert await b.recall("q", user_id="u", top_k=1) == []
        assert b._recall_failures == 1

    async def test_a_successful_recall_is_not_counted(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"request_id": "r", "data": {
                "episodes": [], "profiles": [], "agent_cases": [], "agent_skills": []}})

        b = self._backend(tmp_path, handler)
        await b.recall("q", user_id="u", top_k=1)
        assert b._recall_failures == 0

    async def test_the_xor_violation_is_counted_too(self, tmp_path: Path) -> None:
        """Also a fail-open empty, and equally indistinguishable at the call
        site from "nothing matched"."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"request_id": "r", "data": {}})

        b = self._backend(tmp_path, handler)
        assert await b.recall("q", user_id="u", agent_id="a", top_k=1) == []
        assert b._recall_failures == 1

    async def test_a_version_refusal_reports_once_per_process(
        self, tmp_path: Path, caplog,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json=_CLOUD_VERSION_REFUSAL)

        b = self._backend(tmp_path, handler, api_key="k")
        with caplog.at_level("ERROR"):
            for _ in range(3):
                await b.recall("q", user_id="u", top_k=1)
        assert caplog.text.count("enabled for v2") == 1, "one ERROR per process"
        assert b._recall_failures == 3, "but every attempt is still counted"

    async def test_teardown_reports_the_total(self, tmp_path: Path, caplog) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        b = self._backend(tmp_path, handler)
        await b.recall("q", user_id="u", top_k=1)
        await b.recall("q", user_id="u", top_k=1)
        with caplog.at_level("WARNING"):
            await b.stop()
        assert "2 recall(s) failed" in caplog.text

    async def test_the_store_only_profile_also_gets_the_diagnosis(
        self, tmp_path: Path, caplog,
    ) -> None:
        """recall_enabled=false never runs a recall, so the write-only shape
        would otherwise be the one deployment that never sees it."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json=_CLOUD_VERSION_REFUSAL)

        b = self._backend(tmp_path, handler, api_key="k", recall_enabled=False)
        with caplog.at_level("ERROR"):
            await b.store("s", [{"role": "user", "content": "hi"}])
        assert "enabled for v2" in caplog.text


class TestHealthRetry:
    async def test_health_retries_a_refused_connection(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json={"status": "ok", "version": "1.2.3"})

        a = _HttpEverosAdapter(
            "https://x", retries=3,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        result, payload = await a.health(timeout=1.0)
        assert result.value == "ok"
        assert len(calls) == 3

    async def test_health_retries_a_read_timeout(self) -> None:
        """A GET carries no side effect, so asking twice only costs time."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 2:
                raise httpx.ReadTimeout("slow")
            return httpx.Response(200, json={"status": "ok"})

        a = _HttpEverosAdapter(
            "https://x", retries=2,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        result, _ = await a.health(timeout=1.0)
        assert result.value == "ok"
        assert len(calls) == 2

    async def test_health_gives_up_and_reports_the_reason(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        a = _HttpEverosAdapter(
            "https://x", retries=1,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        result, _ = await a.health(timeout=1.0)
        assert result.value == "refused"
