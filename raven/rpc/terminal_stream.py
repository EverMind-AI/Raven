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


def encode_frame(handle: str, seq: int, payload: bytes, *, replay: bool = False) -> bytes:
    fields = {"handle": handle, "seq": seq}
    if replay:
        fields["replay"] = True
    header = json.dumps(fields, separators=(",", ":")).encode("utf-8")
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
    event_subscriptions: dict[int, str] = field(default_factory=dict)


class TerminalStream:
    """One host subscription per handle, bounded by the slowest subscribed connection."""

    def __init__(self, host, broadcast, emitter=None):
        self.host = host
        self.broadcast = broadcast
        self.emitter = emitter
        self.outputs: dict[str, _Output] = {}
        self.cleanup_tasks: set[asyncio.Task] = set()

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
        async with output.lock:
            if identity not in output.acks:
                output.acks[identity] = output.seq
                subscriptions.add(handle)
                state.setdefault("on_disconnect", []).append(lambda: self._remove(handle, identity))
                try:
                    if self.emitter is not None:
                        record = self.host.show(handle)
                        output.event_subscriptions[identity] = await self.emitter.register(record.worktree_id)
                    snapshot = self.host.raw_snapshot(handle)
                    if output.unsubscribe is None:

                        async def receive(data):
                            await self._emit(handle, output, data)

                        async def replay_captured(data):
                            # Replay is per connection; the shared live callback must not duplicate it.
                            pass

                        output.unsubscribe = self.host.subscribe(handle, receive, replay_callback=replay_captured)
                    sink = connection.current_frame_sink() or self.broadcast
                    for offset in range(0, len(snapshot), ACK_BYTES):
                        await sink(encode_frame(handle, output.seq, snapshot[offset : offset + ACK_BYTES], replay=True))
                except BaseException:
                    subscriptions.discard(handle)
                    self._remove(handle, identity)
                    raise
        result = {"handle": handle, "enabled": True, "seq": output.seq, "ackBytes": ACK_BYTES}
        if identity in output.event_subscriptions:
            result["subscription_id"] = output.event_subscriptions[identity]
        return {"subscription": result}

    def _remove(self, handle, identity):
        output = self.outputs.get(handle)
        if output is None:
            return
        output.acks.pop(identity, None)
        subscription = output.event_subscriptions.pop(identity, None)
        if subscription is not None:
            task = asyncio.create_task(self.emitter.unregister(subscription))
            self.cleanup_tasks.add(task)
            task.add_done_callback(self.cleanup_tasks.discard)
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

    async def shutdown(self):
        self.close()
        if self.cleanup_tasks:
            await asyncio.gather(*self.cleanup_tasks)
