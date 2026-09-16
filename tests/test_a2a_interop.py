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

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from a2a.types import SendMessageResponse
from aiohttp import web
from aiohttp.test_utils import TestServer
from google.protobuf.json_format import ParseDict

from raven.a2a.card import SUBAGENT_EXTENSION_URI
from raven.a2a.routes_aiohttp import add_a2a_routes
from raven.a2a.runtime import build_request_handler
from raven.a2a_client.client import send_message
from raven.a2a_client.peers import same_origin
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


class _Agent:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description


ROSTER = [_Agent("Raven-Code", "Writes and edits code."), _Agent("Raven-Design", "Makes visual decks.")]


@pytest.fixture
async def peer_with_roster() -> AsyncIterator[TestServer]:
    """The same real face, given a roster it can derive an extended card from."""

    async def run_turn(prompt: str, *, conversation_id: str, broker: object) -> str:
        return f"echo: {prompt}"

    app = web.Application()
    add_a2a_routes(app, SERVING, build_request_handler(SERVING, run_turn, roster=lambda: ROSTER))
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


async def _rpc(origin: str, method: str, *, token: str | None) -> dict:
    headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as http:
        reply = await http.post(
            f"{origin}/a2a",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
        )
    return {"status": reply.status_code, "body": reply.json()}


async def _rpc_send(origin: str, text: str) -> dict:
    """One non-streaming `SendMessage`, returning the JSON-RPC body."""
    async with httpx.AsyncClient() as http:
        reply = await http.post(
            f"{origin}/a2a",
            headers={
                "Content-Type": "application/json",
                "A2A-Version": "1.0",
                "Authorization": f"Bearer {TOKEN}",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": uuid4().hex,
                        "role": "ROLE_USER",
                        "parts": [{"text": text}],
                    }
                },
            },
        )
    return reply.json()


async def test_the_unauthenticated_card_hides_the_sub_agents_the_extended_one_names(
    peer_with_roster: TestServer,
) -> None:
    """The two-card split, proved over the wire rather than in the builders.

    The public card answers a plain GET with no credential, so it must not carry
    the host's roster. The extended card carries it and is reachable only
    through the authenticated RPC channel. Asserting both against one running
    server is what makes this a property of the deployment rather than of two
    functions that happen to differ.
    """
    origin = _origin(peer_with_roster)

    async with httpx.AsyncClient() as http:
        public = (await http.get(f"{origin}/.well-known/agent-card.json")).json()

    assert "Raven-Code" not in json.dumps(public)
    assert public["capabilities"].get("extensions", []) == []
    assert public["capabilities"]["extendedAgentCard"] is True

    extended = await _rpc(origin, "GetExtendedAgentCard", token=TOKEN)
    assert extended["status"] == 200
    capabilities = extended["body"]["result"]["capabilities"]
    (extension,) = [e for e in capabilities["extensions"] if e["uri"] == SUBAGENT_EXTENSION_URI]
    assert [row["name"] for row in extension["params"]["agents"]] == ["Raven-Code", "Raven-Design"]


async def test_the_extended_card_is_refused_without_the_credential(peer_with_roster: TestServer) -> None:
    """It rides the same bearer check as every other RPC method -- that is the
    entire reason the roster may live on it."""
    refused = await _rpc(_origin(peer_with_roster), "GetExtendedAgentCard", token=None)

    assert refused["status"] == 401
    assert "Raven-Code" not in json.dumps(refused["body"])


async def test_a_host_with_no_roster_says_so_in_the_protocol_s_own_words(peer: TestServer) -> None:
    """`peer` is built without a roster, so it must answer
    ExtendedAgentCardNotConfiguredError rather than a generic failure -- and its
    public card must not have advertised the method in the first place."""
    async with httpx.AsyncClient() as http:
        public = (await http.get(f"{_origin(peer)}/.well-known/agent-card.json")).json()
    assert public["capabilities"]["extendedAgentCard"] is False

    answer = await _rpc(_origin(peer), "GetExtendedAgentCard", token=TOKEN)

    assert answer["body"]["error"]["message"] == "ExtendedAgentCardNotConfiguredError"


async def test_both_cards_send_a_caller_back_to_the_origin_it_arrived_on(
    peer_with_roster: TestServer,
) -> None:
    """The two cards must not disagree about where this agent is.

    `supportedInterfaces[].url` is what a caller dials and what raven's own
    same-origin guard measures a peer against, and the answer depends on the
    request: one face is reachable under every name that routes to it -- a
    tunnel, a published container port, a proxy -- so a URL fixed at mount time
    is right for at most one of them.

    Over the wire on a server bound to an ephemeral port, because that is the
    only way the two computations are forced to agree on a value neither could
    have been written to expect. Asserting the two builders match in-process
    would pass on any pair of constants.
    """
    origin = _origin(peer_with_roster)

    async with httpx.AsyncClient() as http:
        public = (await http.get(f"{origin}/.well-known/agent-card.json")).json()
    extended = await _rpc(origin, "GetExtendedAgentCard", token=TOKEN)

    served = extended["body"]["result"]["supportedInterfaces"][0]["url"]
    assert served == public["supportedInterfaces"][0]["url"]
    assert served == f"{origin}/a2a"


async def test_the_extended_cards_interface_passes_ravens_own_same_origin_guard(
    peer_with_roster: TestServer,
) -> None:
    """The consumer's predicate, imported rather than restated.

    `send_message` refuses a peer whose card declares an interface off the card's
    own origin. A relative URL reads as off-origin to `same_origin`, so a card
    built without the request would make this host's own guard reject it -- which
    a test asserting "the URL is absolute" would not have shown.
    """
    origin = _origin(peer_with_roster)
    card_url = f"{origin}/.well-known/agent-card.json"

    extended = await _rpc(origin, "GetExtendedAgentCard", token=TOKEN)

    for interface in extended["body"]["result"]["supportedInterfaces"]:
        assert same_origin(card_url, interface["url"])


async def test_a_non_streaming_send_message_parses_as_the_protocols_own_response(peer: TestServer) -> None:
    """The JSON-RPC binding carries `SendMessageResponse`, not the bare result.

    `RequestHandler.on_message_send` answers with a `Task` or a `Message`, and
    the binding wraps that in the envelope whose oneof names which arrived --
    exactly as the streaming sibling wraps its event in `StreamResponse`. A bare
    object makes a successful turn unparseable to any conformant client: the
    SDK's own transport parses `result` as this type and rejects an `id` field
    it has no room for.

    Parsed with the consumer's own type rather than asserted field by field, so
    the test fails for the same reason a real client would.
    """
    reply = await _rpc_send(_origin(peer), "one turn please")

    assert "result" in reply, reply
    envelope = ParseDict(reply["result"], SendMessageResponse())
    assert envelope.WhichOneof("payload") in {"task", "message"}
