"""raven's own A2A client against raven's own A2A server, over a real socket.

Both halves ship in this repo and neither had ever been pointed at the other.
Every other test on either side supplies the counterpart itself: the client
tests answer from a hand-written `httpx` transport that accepts whatever it is
given, and the server tests build their requests by hand and read the frames
back with the same assumptions that wrote them. Each side therefore agreed with
its own idea of the protocol, and three separate disagreements survived a
hundred passing tests -- a missing required field, a missing wire envelope, and
a reply read from an arm the server never uses.

Only the agent turn is stubbed. The credential resolution, the card fetch, the
SDK client, the JSON-RPC framing, the SSE stream and the routes are all real.
"""

from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from raven.a2a.routes_aiohttp import add_a2a_routes
from raven.a2a.runtime import build_request_handler
from raven.a2a_client.client import send_message
from raven.config.schema import A2aConfig, A2aPeerConfig

TOKEN = "t0ken"
SERVING = A2aConfig.model_validate({"server": {"enabled": True, "token": TOKEN, "path": "/a2a"}})


@pytest.fixture
async def peer() -> AsyncIterator[TestServer]:
    """A real raven A2A face on a real port. `pytest-aiohttp` is absent, so
    aiohttp's own TestServer is what this repo uses."""

    async def run_turn(prompt: str, *, conversation_id: str, broker: object) -> str:
        return f"echo: {prompt}"

    app = web.Application()
    add_a2a_routes(app, SERVING, build_request_handler(SERVING, run_turn))
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


def _origin(peer: TestServer) -> str:
    return f"http://127.0.0.1:{peer.port}"


async def test_ravens_client_gets_the_answer_back_from_ravens_server(peer: TestServer) -> None:
    """The whole outbound path against the whole inbound path.

    Three defects lived in the gap this closes, and any one of them alone is
    enough to fail this: the client omitting `message_id` (refused as
    InvalidParamsError before the peer's agent runs), the server streaming bare
    events instead of the `StreamResponse` envelope (unparseable by any
    conformant client), and the client reading only the task and message arms of
    that envelope while raven's own executor answers through `status_update`
    (every frame arrives, no text comes out).
    """
    origin = _origin(peer)
    config = A2aConfig(peers=[A2aPeerConfig(origin=origin, credential=TOKEN)])

    reply = await send_message(config, f"{origin}/.well-known/agent-card.json", "hello")

    assert reply == "echo: hello"


async def test_an_unlisted_peer_is_refused_rather_than_answered(peer: TestServer) -> None:
    """The negative control for the test above: the reply there is not something
    this server hands to anyone who asks. An origin absent from the peer list
    gets no credential attached, and the face refuses it."""
    origin = _origin(peer)

    with pytest.raises(Exception) as excinfo:
        await send_message(A2aConfig(peers=[]), f"{origin}/.well-known/agent-card.json", "hello")

    assert "echo" not in str(excinfo.value)
