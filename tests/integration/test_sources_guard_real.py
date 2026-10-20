"""The real transport, held short of the network.

The guard's whole claim is an order: the rule first, the request second. A
unit test can only prove that the refusal happened; this one proves that the
socket was never opened, because the transport is the real one and the
connection it is handed refuses to dial.

Nothing here reaches the internet, and nothing here needs a key: the test is
about what does *not* happen.
"""

from __future__ import annotations

import httpx
import pytest

from raven.sources import PolicyClient, SourceRefusedError, send

pytestmark = pytest.mark.integration


class _NoSocket(httpx.AsyncBaseTransport):
    """A transport that records a dial and answers nothing."""

    def __init__(self) -> None:
        self.dials = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.dials += 1
        raise AssertionError("the request reached the transport before the registry agreed to it")


@pytest.mark.asyncio
async def test_a_refused_source_never_opens_a_socket() -> None:
    transport = _NoSocket()
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SourceRefusedError):
            await send(client, "GET", "https://api.tavily.com/search", source="linkedin", use="call")

    assert transport.dials == 0


@pytest.mark.asyncio
async def test_a_source_nobody_looked_at_never_opens_a_socket() -> None:
    transport = _NoSocket()
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SourceRefusedError):
            await send(client, "POST", "https://api.example.test/search", source="never_heard_of_it", use="call")

    assert transport.dials == 0


@pytest.mark.asyncio
async def test_a_registered_source_reaches_the_transport() -> None:
    class _Answering(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.dials = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.dials += 1
            return httpx.Response(200, json={"ok": True})

    transport = _Answering()
    async with httpx.AsyncClient(transport=transport) as client:
        response = await send(client, "GET", "https://api.tavily.com/search", source="tavily", use="call")

    assert transport.dials == 1
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_client_carries_the_source_into_the_check() -> None:
    transport = _NoSocket()
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SourceRefusedError):
            await PolicyClient(client, "capterra").post("https://api.example.test/extract", json={})

    assert transport.dials == 0
