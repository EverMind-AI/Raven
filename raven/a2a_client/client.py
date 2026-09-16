"""One A2A call: fetch the peer's card, send a message, return its text.

The SDK owns the wire format; this module owns which credential goes out and
how a reply becomes a string the model can read.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import AgentCard, Message, Part, Role, SendMessageRequest
from a2a.utils import TransportProtocol

from raven.a2a_client.peers import auth_headers, resolve_peer, same_origin
from raven.config.schema import A2aConfig

A2A_VERSION_HEADER = {"A2A-Version": "1.0"}


def _text_of(event: object) -> str:
    """Any text carried by one StreamResponse event.

    The oneof has four arms -- task, message, status_update, artifact_update --
    and which one carries the answer is the peer's choice, so all four are read.
    `status_update` is not an optional extra: raven's own executor reports
    through it, putting the reply in the terminal COMPLETED frame's
    `status.message`, so a reader of only task and message watches every frame
    arrive and extracts nothing from any of them.

    An unset arm reads back as a default-empty message rather than raising, so
    the arms that did not arrive contribute nothing and need no guard.
    """
    chunks: list[str] = []

    def take(container: object) -> None:
        for part in getattr(container, "parts", None) or []:
            if getattr(part, "text", ""):
                chunks.append(part.text)

    take(getattr(event, "message", None))

    task = getattr(event, "task", None)
    take(getattr(getattr(task, "status", None), "message", None))
    for artifact in getattr(task, "artifacts", None) or []:
        take(artifact)

    take(getattr(getattr(getattr(event, "status_update", None), "status", None), "message", None))
    take(getattr(getattr(event, "artifact_update", None), "artifact", None))
    return "\n".join(chunks)


def _off_origin_interface(card_url: str, card: AgentCard) -> str | None:
    """The URL of a JSON-RPC interface `card` declares off `card_url`'s origin, if any.

    A card can declare `supported_interfaces[].url` on a different origin
    than the URL it was fetched from (or, via the SDK's legacy-card
    compatibility shim, promote a bare top-level `url` field into the same
    list). Only JSON-RPC-bound interfaces are checked: this client's
    `ClientConfig` never enables any other binding, so no other binding is
    ever dialed regardless of what else the card declares.
    """
    for interface in card.supported_interfaces:
        if interface.protocol_binding != TransportProtocol.JSONRPC:
            continue
        if not same_origin(card_url, interface.url):
            return interface.url
    return None


async def send_message(config: A2aConfig, card_url: str, message: str, *, timeout_s: float = 300.0) -> str:
    """Send `message` to the A2A agent whose card is at `card_url`.

    `card_url` reaches `resolve_peer` and the SDK unchanged and identical: the
    same string that resolves the credential is the one that later opens the
    connection, so the two can never disagree about which origin is being
    called. The httpx client keeps `follow_redirects` at its default (False):
    a redirect followed with these headers already attached could otherwise
    carry the resolved Authorization header to a different origin.

    A card fetched from a trusted origin can still declare a JSON-RPC
    interface on a different one (`AgentCard.supported_interfaces[].url`);
    the resolved credential is a blanket header on this client, so it would
    reach that declared origin too if the SDK were allowed to dial it. The
    card is fetched here rather than left to `ClientFactory.create_from_url`
    so that check can run before any client is built from it.
    """
    headers = {**A2A_VERSION_HEADER, **auth_headers(resolve_peer(config, card_url))}
    async with httpx.AsyncClient(headers=headers, timeout=timeout_s) as http:
        # relative_card_path="/" tells the resolver that card_url is already the
        # complete agent-card URL. Left at its None default, the resolver joins
        # its own well-known suffix onto card_url instead of using it as-is,
        # which 404s whenever card_url already ends in that same suffix.
        card = await A2ACardResolver(http, card_url).get_agent_card(relative_card_path="/")
        off_origin_url = _off_origin_interface(card_url, card)
        if off_origin_url is not None:
            return (
                f"Error: the agent card at {card_url} declares an interface at "
                f"{off_origin_url}, which is on a different origin than the card "
                "itself. Refusing to send this message: the credential resolved for "
                "the card's origin must not reach a different origin. If this "
                "cross-origin interface is intended, configure its origin as its own "
                "peer."
            )
        client = ClientFactory(ClientConfig(httpx_client=http)).create(card)
        # `message_id` is required, and the SDK client does not fill it in: a
        # message sent without one is refused by a conformant peer -- including
        # raven's own inbound face -- as InvalidParamsError, before the peer's
        # agent ever runs. The value only has to identify this message to the
        # peer, so a fresh uuid is the whole requirement.
        request = SendMessageRequest(
            message=Message(role=Role.ROLE_USER, parts=[Part(text=message)], message_id=uuid4().hex)
        )
        chunks = [text async for event in client.send_message(request) if (text := _text_of(event))]
    return "\n".join(chunks) or "(the peer returned no text)"
