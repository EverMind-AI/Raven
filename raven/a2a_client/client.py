"""One A2A call: fetch the peer's card, send a message, return its text.

The SDK owns the wire format; this module owns which credential goes out and
how a reply becomes a string the model can read.
"""

from __future__ import annotations

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, SendMessageRequest

from raven.a2a_client.peers import auth_headers, resolve_peer
from raven.config.schema import A2aConfig

A2A_VERSION_HEADER = {"A2A-Version": "1.0"}


def _text_of(event: object) -> str:
    """Any text carried by one StreamResponse event.

    The oneof is task-or-message, so both arms are read: a peer may answer with a
    message directly, or with a task whose artifacts hold the answer.
    """
    chunks: list[str] = []
    message = getattr(event, "message", None)
    for part in getattr(message, "parts", None) or []:
        if getattr(part, "text", ""):
            chunks.append(part.text)
    task = getattr(event, "task", None)
    for artifact in getattr(task, "artifacts", None) or []:
        for part in getattr(artifact, "parts", None) or []:
            if getattr(part, "text", ""):
                chunks.append(part.text)
    return "\n".join(chunks)


async def send_message(config: A2aConfig, card_url: str, message: str, *, timeout_s: float = 300.0) -> str:
    """Send `message` to the A2A agent whose card is at `card_url`.

    `card_url` reaches `resolve_peer` and the SDK unchanged and identical: the
    same string that resolves the credential is the one that later opens the
    connection, so the two can never disagree about which origin is being
    called. The httpx client keeps `follow_redirects` at its default (False):
    a redirect followed with these headers already attached could otherwise
    carry the resolved Authorization header to a different origin.

    Not covered: a peer's card can itself declare an interface URL on a
    different origin than `card_url` (`AgentCard.supported_interfaces[].url`),
    and the SDK sends the request to that declared origin without checking it
    against `card_url`'s origin. The credential resolved below is a blanket
    header on this client, so it reaches that declared origin too. Closing
    that gap needs matching the interface URL's origin the same way
    `resolve_peer` matches `card_url`'s.
    """
    headers = {**A2A_VERSION_HEADER, **auth_headers(resolve_peer(config, card_url))}
    async with httpx.AsyncClient(headers=headers, timeout=timeout_s) as http:
        factory = ClientFactory(ClientConfig(httpx_client=http))
        # relative_card_path="/" tells the resolver that card_url is already the
        # complete agent-card URL. Left at its None default, the resolver joins
        # its own well-known suffix onto card_url instead of using it as-is,
        # which 404s whenever card_url already ends in that same suffix.
        client = await factory.create_from_url(card_url, relative_card_path="/")
        request = SendMessageRequest(message=Message(role=Role.ROLE_USER, parts=[Part(text=message)]))
        chunks = [text async for event in client.send_message(request) if (text := _text_of(event))]
    return "\n".join(chunks) or "(the peer returned no text)"
