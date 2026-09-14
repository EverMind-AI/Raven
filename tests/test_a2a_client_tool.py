"""The outbound tool: its schema, and that it never surfaces a credential."""

import json

import httpx

from raven.a2a_client.client import send_message
from raven.a2a_client.tool import A2aTool
from raven.config.schema import A2aConfig

CONFIG = A2aConfig.model_validate(
    {"peers": [{"origin": "https://peer.example.com", "authScheme": "bearer", "credential": "sekrit"}]}
)


def test_tool_name_is_stable():
    assert A2aTool(CONFIG).name == "a2a_send"


def test_parameters_ask_for_a_card_url_and_a_message():
    schema = A2aTool(CONFIG).parameters
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"card_url", "message"}
    assert set(schema["properties"]) == {"card_url", "message"}


def test_no_credential_field_is_exposed_to_the_model():
    blob = repr(A2aTool(CONFIG).parameters) + A2aTool(CONFIG).description
    assert "credential" not in blob
    assert "sekrit" not in blob


async def test_execute_returns_the_peer_reply(monkeypatch):
    seen = {}

    async def fake_send(config, card_url, message, *, timeout_s=300.0):
        seen["card_url"] = card_url
        seen["message"] = message
        return "peer answered"

    monkeypatch.setattr("raven.a2a_client.tool.send_message", fake_send)
    out = await A2aTool(CONFIG).execute(
        card_url="https://peer.example.com/.well-known/agent-card.json", message="hello"
    )
    assert out == "peer answered"
    assert seen["message"] == "hello"


async def test_a_transport_failure_becomes_a_model_readable_error(monkeypatch):
    async def boom(config, card_url, message, *, timeout_s=300.0):
        raise ConnectionError("refused")

    monkeypatch.setattr("raven.a2a_client.tool.send_message", boom)
    out = await A2aTool(CONFIG).execute(card_url="https://peer.example.com/c", message="hi")
    assert out.startswith("Error:")
    assert "refused" in out


async def test_send_message_reaches_the_mocked_peer_with_the_resolved_credential(monkeypatch):
    """Regression test for two SDK-usage bugs found while building client.py:
    a missing ``await`` on ``create_from_url``, and a doubled well-known path
    from omitting ``relative_card_path``. Both would make this test fail to
    reach the mocked peer at all, rather than merely return the wrong text.
    """
    seen_auth: list[str | None] = []
    card = {
        "name": "peer-agent",
        "description": "peer",
        "version": "1.0.0",
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "supportedInterfaces": [
            {"url": "https://peer.example.com/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "skills": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        if request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json=card)
        body = json.loads(request.content)
        reply = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {"message": {"role": "ROLE_AGENT", "parts": [{"text": "peer answered"}]}},
        }
        return httpx.Response(200, json=reply)

    real_async_client = httpx.AsyncClient

    def mock_async_client(**kwargs: object) -> httpx.AsyncClient:
        return real_async_client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)

    out = await send_message(CONFIG, "https://peer.example.com/.well-known/agent-card.json", "hello")

    assert out == "peer answered"
    assert seen_auth == ["Bearer sekrit", "Bearer sekrit"]
