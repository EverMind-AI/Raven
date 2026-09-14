"""A real A2A client against a real raven A2A server, over a real socket.

The SDK's own client is the closest thing to a second implementation available,
so conformance is asserted against it rather than against our own encoder. The
handler under test is the real ``DefaultRequestHandler`` assembled by
``raven.a2a.runtime.build_request_handler`` -- not a fake -- so a pass here is
proof about the SDK integration, not just about this repo's own encoding.
"""

from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from raven.a2a.card import CARD_PATH
from raven.a2a.routes_aiohttp import add_a2a_routes
from raven.a2a.runtime import build_request_handler
from raven.config.schema import A2aConfig

CONFIG = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken", "path": "/a2a"}})


@pytest.fixture
async def server() -> AsyncIterator[TestServer]:
    """`pytest-aiohttp` is absent; aiohttp's own TestServer is what this repo uses."""

    async def run_turn(prompt):
        return f"echo: {prompt}"

    app = web.Application()
    add_a2a_routes(app, CONFIG, build_request_handler(CONFIG, run_turn))
    srv = TestServer(app)
    await srv.start_server()
    try:
        yield srv
    finally:
        await srv.close()


async def test_the_card_round_trips_through_a_plain_fetch(server):
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.get(str(server.make_url(CARD_PATH)))
    assert resp.status_code == 200
    card = resp.json()
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["capabilities"]["streaming"] is True


async def test_send_message_returns_the_turn_answer(server):
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            str(server.make_url("/a2a")),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {
                    "message": {
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "messageId": "m1",
                    }
                },
            },
            headers={"Authorization": "Bearer t0ken", "A2A-Version": "1.0"},
        )
    body = resp.json()
    assert "error" not in body
    assert "echo: hello" in str(body["result"])


async def test_a_header_less_request_is_refused_over_the_wire(server):
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            str(server.make_url("/a2a")),
            json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}},
            headers={"Authorization": "Bearer t0ken"},
        )
    assert resp.json()["error"]["message"] == "VersionNotSupportedError"


async def test_streaming_delivers_more_than_one_event(server):
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as http:
        async with http.stream(
            "POST",
            str(server.make_url("/a2a")),
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "SendStreamingMessage",
                "params": {
                    "message": {
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "messageId": "m2",
                    }
                },
            },
            headers={"Authorization": "Bearer t0ken", "A2A-Version": "1.0"},
        ) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            lines = [line async for line in resp.aiter_lines() if line.startswith("data:")]
    assert len(lines) >= 2
