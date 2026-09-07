"""RVT1 framing, cumulative byte acknowledgements and subscription lifetime."""

import asyncio
import json
import struct
from unittest.mock import AsyncMock

import pytest

from raven.rpc import connection
from raven.rpc.errors import RpcError
from raven.rpc.terminal_stream import TerminalStream, decode_frame, encode_frame


class StubOutputHost:
    """A host that awaits subscribers before reading its next PTY chunk."""

    def __init__(self):
        self.callbacks = {}

    def show(self, handle):
        if handle != "term_test":
            raise KeyError(handle)

    def subscribe(self, handle, callback):
        self.callbacks[handle] = callback
        return lambda: self.callbacks.pop(handle, None)


def test_rvt1_frame_is_length_prefixed_json_and_raw_bytes():
    frame = encode_frame("term_test", 4, b"\x00\xff\r\n")
    assert frame[:4] == b"RVT1"
    size = struct.unpack(">I", frame[4:8])[0]
    assert json.loads(frame[8 : 8 + size]) == {"handle": "term_test", "seq": 4}
    assert decode_frame(frame) == ({"handle": "term_test", "seq": 4}, b"\x00\xff\r\n")


@pytest.mark.parametrize(
    "frame", [b"RVF1\x00\x00\x00\x00", b"RVT1", b"RVT1\xff\xff\xff\xff", b"RVT1\x00\x00\x00\x02{}"]
)
def test_invalid_or_browser_frames_do_not_decode_as_terminal(frame):
    with pytest.raises(ValueError):
        decode_frame(frame)


async def test_window_pauses_at_one_mib_until_cumulative_ack():
    host, sink = StubOutputHost(), AsyncMock()
    stream = TerminalStream(host, sink)
    token = connection.bind_connection()
    try:
        await stream.subscribe({"handle": "term_test"})
        output = asyncio.create_task(host.callbacks["term_test"](b"x" * (1024 * 1024 + 1)))
        await asyncio.sleep(0.01)
        assert not output.done()
        assert sum(len(decode_frame(c.args[0])[1]) for c in sink.await_args_list) == 1024 * 1024
        await stream.subscribe({"handle": "term_test", "ack": 65536})
        await asyncio.wait_for(output, 1)
        assert decode_frame(sink.await_args.args[0])[0]["seq"] == 1024 * 1024 + 1
        with pytest.raises(RpcError, match="ack"):
            await stream.subscribe({"handle": "term_test", "ack": 2 * 1024 * 1024})
    finally:
        connection.unbind_connection(token)
    assert host.callbacks == {}


async def test_disconnect_unblocks_backpressure_and_unsubscribes_host():
    host, sink = StubOutputHost(), AsyncMock()
    stream = TerminalStream(host, sink)
    token = connection.bind_connection()
    await stream.subscribe({"handle": "term_test"})
    output = asyncio.create_task(host.callbacks["term_test"](b"x" * (1024 * 1024 + 1)))
    await asyncio.sleep(0.01)
    assert not output.done()
    connection.unbind_connection(token)
    await asyncio.wait_for(output, 1)
    assert host.callbacks == {}


async def test_unsubscribed_connection_cannot_ack_another_subscriber():
    host, sink = StubOutputHost(), AsyncMock()
    stream = TerminalStream(host, sink)
    token = connection.bind_connection()
    try:
        await stream.subscribe({"handle": "term_test"})
        other = connection.bind_connection()
        try:
            with pytest.raises(RpcError) as refused:
                await stream.subscribe({"handle": "term_test", "ack": 0})
            assert refused.value.message == "not_subscribed"
        finally:
            connection.unbind_connection(other)
        await stream.subscribe({"handle": "term_test", "enabled": False})
        assert host.callbacks == {}
    finally:
        connection.unbind_connection(token)


async def test_gateway_filters_terminal_frames_and_preserves_browser_broadcast():
    from raven.rpc.transports.ws import WsGateway

    gateway = WsGateway()
    subscriber, other = AsyncMock(), AsyncMock()
    gateway._sockets.update((subscriber, other))
    gateway._connection_states[subscriber] = {"terminal_subscriptions": {"term_test"}}
    frame = encode_frame("term_test", 3, b"raw")
    await gateway.broadcast(frame)
    subscriber.send_bytes.assert_awaited_once_with(frame)
    other.send_bytes.assert_not_awaited()
    browser = b"RVF1browser"
    await gateway.broadcast(browser)
    other.send_bytes.assert_awaited_once_with(browser)
