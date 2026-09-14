"""The JSON-RPC binding: version gating, auth ordering, and error shape."""

import json
from collections.abc import AsyncIterator

import pytest
from a2a.utils.errors import JSON_RPC_ERROR_CODE_MAP
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from raven.a2a.card import CARD_PATH
from raven.a2a.routes_aiohttp import ERROR_CODES, add_a2a_routes
from raven.config.schema import A2aConfig

CONFIG = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken", "path": "/a2a"}})
AUTH = {"Authorization": "Bearer t0ken", "A2A-Version": "1.0"}


class RecordingHandler:
    def __init__(self):
        self.calls = []

    async def on_message_send(self, params, context):
        self.calls.append(params)
        return {"ok": True}

    async def on_message_send_stream(self, params, context):
        self.calls.append(params)
        yield {"seq": 1}
        yield {"seq": 2}


class NonSerializableResultHandler:
    """A handler whose result cannot reach `json.dumps` -- reproduces F3."""

    def __init__(self):
        self.calls = []

    async def on_message_send(self, params, context):
        self.calls.append(params)
        return {"tags": {1, 2, 3}}


@pytest.fixture
async def client_and_handler() -> AsyncIterator[tuple[TestClient, RecordingHandler]]:
    """`pytest-aiohttp` is not installed here; aiohttp ships these test utils itself.
    Same shape as the fixture in tests/test_rpc_files.py."""
    handler = RecordingHandler()
    app = web.Application()
    add_a2a_routes(app, CONFIG, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, handler
    finally:
        await client.close()


async def test_the_card_is_served_unauthenticated(client_and_handler):
    client, _ = client_and_handler
    resp = await client.get(CARD_PATH)
    assert resp.status == 200
    assert "supportedInterfaces" in await resp.text()


async def test_a_missing_version_header_is_refused(client_and_handler):
    client, handler = client_and_handler
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}},
        headers={"Authorization": "Bearer t0ken"},
    )
    body = await resp.json()
    assert body["error"]["message"] == "VersionNotSupportedError"
    assert handler.calls == []


async def test_an_unauthenticated_call_never_reaches_the_handler(client_and_handler):
    client, handler = client_and_handler
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}},
        headers={"A2A-Version": "1.0"},
    )
    assert resp.status == 401
    assert handler.calls == []


async def test_an_authenticated_1_0_call_reaches_the_handler(client_and_handler):
    client, handler = client_and_handler
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": "SendMessage", "params": {"x": 1}},
        headers=AUTH,
    )
    assert resp.status == 200
    assert (await resp.json())["id"] == 7
    assert handler.calls == [{"x": 1}]


async def test_an_unknown_method_is_a_json_rpc_error(client_and_handler):
    client, _ = client_and_handler
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 2, "method": "Nope", "params": {}},
        headers=AUTH,
    )
    assert (await resp.json())["error"]["message"] == "MethodNotFoundError"


async def test_streaming_delivers_two_events_over_sse(client_and_handler):
    client, handler = client_and_handler
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 3, "method": "SendStreamingMessage", "params": {"x": 1}},
        headers=AUTH,
    )
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    body = await resp.text()
    events = [line for line in body.splitlines() if line.startswith("data:")]
    assert len(events) == 2
    assert handler.calls == [{"x": 1}]


def test_error_codes_are_read_from_the_sdks_canonical_map():
    """Pin against the SDK's own map, not against numbers copied into this test --
    a hardcoded expectation here would drift the same way the production table did."""
    sdk_codes = {cls.__name__: code for cls, code in JSON_RPC_ERROR_CODE_MAP.items()}
    assert ERROR_CODES["VersionNotSupportedError"] == sdk_codes["VersionNotSupportedError"]
    assert ERROR_CODES["TaskNotFoundError"] == sdk_codes["TaskNotFoundError"]


@pytest.mark.parametrize("value", [None, 42, [], "str", True])
async def test_a_non_dict_body_is_a_json_rpc_error_not_a_crash(client_and_handler, value):
    client, handler = client_and_handler
    resp = await client.post(
        "/a2a",
        data=json.dumps(value).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == ERROR_CODES["InvalidRequestError"]
    assert handler.calls == []


async def test_a_non_serializable_result_is_a_json_rpc_error_not_a_crash():
    handler = NonSerializableResultHandler()
    app = web.Application()
    add_a2a_routes(app, CONFIG, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": 9, "method": "SendMessage", "params": {}},
            headers=AUTH,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["error"]["code"] == ERROR_CODES["InternalError"]
        assert handler.calls == [{}]
    finally:
        await client.close()
