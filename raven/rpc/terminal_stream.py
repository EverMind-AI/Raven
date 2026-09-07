"""RVT1 raw terminal output with connection-scoped cumulative byte acknowledgements."""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass, field

from raven.rpc import connection
from raven.rpc.methods.terminal import _rpc_error, _text

MAGIC = b"RVT1"
ACK_BYTES = 64 * 1024
WINDOW_BYTES = 1024 * 1024


def encode_frame(handle: str, seq: int, payload: bytes) -> bytes:
    header = json.dumps({"handle": handle, "seq": seq}, separators=(",", ":")).encode("utf-8")
    return MAGIC + struct.pack(">I", len(header)) + header + payload


def decode_frame(frame: bytes) -> tuple[dict, bytes]:
    if len(frame) < 8 or frame[:4] != MAGIC:
        raise ValueError("Not an RVT1 frame")
    size = struct.unpack(">I", frame[4:8])[0]
    if size > 4096 or len(frame) < 8 + size:
        raise ValueError("Invalid RVT1 header size")
    try:
        header = json.loads(frame[8 : 8 + size])
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid RVT1 header") from exc
    if not isinstance(header, dict) or not isinstance(header.get("handle"), str) or not header["handle"]:
        raise ValueError("Invalid RVT1 handle")
    if type(header.get("seq")) is not int or not 0 <= header["seq"] <= 2**53 - 1:
        raise ValueError("Invalid RVT1 sequence")
    return header, frame[8 + size :]


@dataclass
class _Output:
    seq: int = 0
    acks: dict[int, int] = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    unsubscribe: object = None


class TerminalStream:
    """One host subscription per handle, bounded by the slowest subscribed connection."""

    def __init__(self, host, broadcast):
        self.host = host
        self.broadcast = broadcast
        self.outputs: dict[str, _Output] = {}

    async def subscribe(self, params):
        handle = _text(params, "handle")
        self.host.show(handle)
        state = connection.current_state()
        if not connection.is_bound(state):
            raise _rpc_error("connection_required", "Terminal streams require a live connection")
        identity = id(state)
        output = self.outputs.get(handle)
        if "ack" in params:
            ack = params["ack"]
            if output is None or identity not in output.acks:
                raise _rpc_error("not_subscribed", "Subscribe before acknowledging output")
            if type(ack) is not int or not output.acks[identity] <= ack <= output.seq:
                raise _rpc_error("invalid_argument", "ack must be a cumulative received byte offset", invalid=True)
            output.acks[identity] = ack
            output.ready.set()
            return {"subscription": {"handle": handle, "seq": output.seq, "ack": ack}}
        enabled = params.get("enabled", True)
        if type(enabled) is not bool:
            raise _rpc_error("invalid_argument", "enabled must be a boolean", invalid=True)
        subscriptions = state.setdefault("terminal_subscriptions", set())
        if not enabled:
            subscriptions.discard(handle)
            self._remove(handle, identity)
            return {"subscription": {"handle": handle, "enabled": False}}
        if output is None:
            output = _Output()
            self.outputs[handle] = output
        if identity not in output.acks:
            output.acks[identity] = output.seq
            subscriptions.add(handle)
            state.setdefault("on_disconnect", []).append(lambda: self._remove(handle, identity))
        if output.unsubscribe is None:

            async def receive(data):
                await self._emit(handle, output, data)

            try:
                output.unsubscribe = self.host.subscribe(handle, receive)
            except Exception:
                subscriptions.discard(handle)
                self._remove(handle, identity)
                raise
        return {"subscription": {"handle": handle, "enabled": True, "seq": output.seq, "ackBytes": ACK_BYTES}}

    def _remove(self, handle, identity):
        output = self.outputs.get(handle)
        if output is None:
            return
        output.acks.pop(identity, None)
        output.ready.set()
        if not output.acks:
            if output.unsubscribe is not None:
                output.unsubscribe()
            self.outputs.pop(handle, None)

    async def _emit(self, handle, output, data):
        async with output.lock:
            offset = 0
            while offset < len(data) and output.acks:
                available = WINDOW_BYTES - (output.seq - min(output.acks.values()))
                if available <= 0:
                    output.ready.clear()
                    await output.ready.wait()
                    continue
                chunk = data[offset : offset + min(ACK_BYTES, available)]
                output.seq += len(chunk)
                offset += len(chunk)
                await self.broadcast(encode_frame(handle, output.seq, chunk))

    def close(self):
        for handle, output in list(self.outputs.items()):
            for identity in list(output.acks):
                self._remove(handle, identity)
