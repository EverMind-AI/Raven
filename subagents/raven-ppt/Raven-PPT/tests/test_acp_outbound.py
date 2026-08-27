"""The half of the connection that asks the client something and waits.

Every case here is one of the four ways an outbound request ends -- answered,
refused, ignored, or orphaned by a close -- because the caller's right response
differs for each and a single "it failed" would collapse them.
"""

import asyncio

import pytest

from raven.acp import protocol
from raven.acp.outbound import ConnectionClosedError, OutboundRequests, RequestFailedError


class Wire:
    """Collects the frames an agent puts on the wire."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    def __call__(self, frame: dict) -> None:
        self.frames.append(frame)

    def methods(self) -> list[str]:
        return [f.get("method") for f in self.frames if "method" in f]


@pytest.mark.asyncio
async def test_a_request_carries_an_int_id_and_its_answer_comes_back():
    wire = Wire()
    outbound = OutboundRequests(wire)

    task = asyncio.create_task(outbound.call("elicitation/create", {"message": "which one"}))
    await asyncio.sleep(0)

    sent = wire.frames[0]
    assert sent["method"] == "elicitation/create"
    assert isinstance(sent["id"], int)
    assert sent["params"] == {"message": "which one"}

    assert outbound.resolve({"jsonrpc": "2.0", "id": sent["id"], "result": {"action": "accept"}}) is True
    assert await task == {"action": "accept"}
    assert outbound.in_flight == 0


@pytest.mark.asyncio
async def test_an_error_answer_is_raised_as_a_refusal_not_a_timeout():
    wire = Wire()
    outbound = OutboundRequests(wire)
    task = asyncio.create_task(outbound.call("session/request_permission"))
    await asyncio.sleep(0)

    outbound.resolve(
        {"jsonrpc": "2.0", "id": wire.frames[0]["id"], "error": {"code": -32601, "message": "no such method"}}
    )

    with pytest.raises(RequestFailedError) as caught:
        await task
    assert caught.value.code == -32601
    assert caught.value.method == "session/request_permission"


@pytest.mark.asyncio
async def test_a_client_that_never_answers_times_out_and_the_request_is_retracted():
    wire = Wire()
    outbound = OutboundRequests(wire)

    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await outbound.call("elicitation/create", timeout=0.01)

    # The client is still holding a form with no way to learn nobody is reading.
    assert protocol.CANCEL_REQUEST_METHOD in wire.methods()
    assert outbound.in_flight == 0


@pytest.mark.asyncio
async def test_closing_fails_what_is_outstanding_and_refuses_what_comes_next():
    wire = Wire()
    outbound = OutboundRequests(wire)
    task = asyncio.create_task(outbound.call("elicitation/create"))
    await asyncio.sleep(0)

    outbound.close()

    with pytest.raises(ConnectionClosedError):
        await task
    with pytest.raises(ConnectionClosedError):
        await outbound.call("elicitation/create")


@pytest.mark.asyncio
async def test_a_response_this_agent_did_not_ask_for_is_reported_as_not_ours():
    outbound = OutboundRequests(Wire())

    # A string id can only be a client answering something it invented: every id
    # this agent mints is a plain int.
    assert outbound.resolve({"jsonrpc": "2.0", "id": "client-1", "result": {}}) is False
    assert outbound.resolve({"jsonrpc": "2.0", "id": 999, "result": {}}) is False
    assert outbound.resolve({"jsonrpc": "2.0", "id": True, "result": {}}) is False


@pytest.mark.asyncio
async def test_a_second_answer_to_the_same_request_is_refused():
    wire = Wire()
    outbound = OutboundRequests(wire)
    task = asyncio.create_task(outbound.call("elicitation/create"))
    await asyncio.sleep(0)
    request_id = wire.frames[0]["id"]

    assert outbound.resolve({"jsonrpc": "2.0", "id": request_id, "result": {"action": "accept"}}) is True
    await task
    assert outbound.resolve({"jsonrpc": "2.0", "id": request_id, "result": {"action": "decline"}}) is False
