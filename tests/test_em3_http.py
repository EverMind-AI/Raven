"""EM-3 — EverosBackend HTTP mode (remote EverOS)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("raven.plugin.memory.everos")

from raven.plugin import PluginContext, ServiceLocator
from raven.plugin.memory.everos.backend import (
    EverosBackend,
    _HttpEverosAdapter,
    _jsonify,
)

# ---------------------------------------------------------------------------
# Mock-transport helpers
# ---------------------------------------------------------------------------


class _MockEverOS:
    """Records requests + emits canned responses for tests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
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

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
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
        assert str(req.url) == "http://mem.test/api/v1/memory/search"
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
        mock.status_for_path["/api/v1/memory/search"] = 503
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
        assert str(mock.requests[0].url).endswith("/api/v1/memory/add")
        body = json.loads(mock.requests[0].content.decode())
        assert body == {"session_id": "session-1", "messages": msgs}

    async def test_5xx_raises(self, mock, http_client) -> None:
        mock.status_for_path["/api/v1/memory/add"] = 500
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
        assert str(mock.requests[0].url) == ("http://mem.test/api/v1/memory/search")


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
        assert b._adapter._base_url == "http://localhost:1995"

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
