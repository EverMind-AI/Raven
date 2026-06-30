"""Delivery: what a channel can do (Capabilities), the streaming opt-in
(SupportsStreaming), the per-channel send surface (Outlet), and the hub that
routes each deliverable to its outlet (DeliveryHub).

The hub keeps one bounded queue and one serial worker per outlet: a deliverable
is routed by its source channel into that outlet's queue, and the queue is the
backpressure point — a full queue blocks only that channel's sender, never the
others (no cross-outlet head-of-line blocking), while same-channel order is held
by the single worker. This mirrors the lane model on the delivery side.

spine never imports channels; channels import the vocabulary here (via the
channels.contract re-export), not the reverse.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from loguru import logger

from raven.spine.events import (
    Deliverable,
    StreamDelta,
    TurnEnded,
    TurnEvent,
    TurnFailed,
    TurnStarted,
)


@dataclass(frozen=True)
class Capabilities:
    """What a channel can do, declared explicitly (not inferred from methods).

    Only capabilities with a real consumer live here. ``media``/``reactions``
    are adapter-internal today (nothing routes on them) — add them back with
    their consumer when one exists.
    """

    interactive_login: bool = False  # QR / scan login (weixin, whatsapp); read by CLI `channel login`
    streaming: bool = False  # SupportsStreaming slot; activated in B


@runtime_checkable
class SupportsStreaming(Protocol):
    """Opt-in incremental delivery (edit-in-place). Inert until the agent loop
    is wired to produce stream chunks (scope B)."""

    async def send_stream_chunk(self, chat_id: str, stream_id: str, delta: str, *, done: bool = False) -> None: ...


@runtime_checkable
class Outlet(Protocol):
    """A channel's send surface. ``deliver`` either renders the deliverable or, if
    the channel can't express it, eats it with a normal return (logging its own
    skip). Only a real failure — transport error, bug — raises, which the hub
    retries. Eating is not failure. Lifecycle (connect/teardown) stays on the
    channel; an outlet is just the send seam."""

    name: str
    capabilities: Capabilities

    async def deliver(self, out: Deliverable) -> None: ...


_SEND_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds; doubles each retry (1, 2, 4)
_OUTLET_QUEUE_MAXSIZE = 100  # per-outlet backpressure bound; config knob lands with its consumer


@dataclass(frozen=True)
class _StreamClose:
    """A marker the hub puts on an outlet's queue so the stream's done=True chunk
    is sent after the last StreamDelta still in flight (a sourceless lifecycle
    event can't ride the queue itself, but this can)."""

    conversation_id: str


class DeliveryHub:
    """Routes each deliverable into its source channel's bounded queue, where a
    per-outlet serial worker delivers it (retrying a raising send with backoff).
    Holds the outlet registry plus a queue and worker per outlet; no turn state.

    Streaming rides the same queue: StreamDelta is sent via send_stream_chunk and
    close_stream enqueues a marker so the closing chunk follows the deltas. The
    open-stream table is the worker's alone (single owner); the channel a stream
    rides is recorded synchronously on enqueue so close_stream can route to it."""

    def __init__(self, send_max_retries: int = _SEND_MAX_RETRIES) -> None:
        self._send_max_retries = send_max_retries
        self._outlets: dict[str, Outlet] = {}
        self._queues: dict[str, asyncio.Queue[Deliverable | _StreamClose]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        # conversation_id -> channel, written on enqueue (sink path), read by
        # close_stream to route its marker; the open-stream table below is the
        # worker's (conversation_id -> chat_id, present iff the stream is open).
        self._stream_channel: dict[str, str] = {}
        self._open_streams: dict[str, str] = {}

    def register(self, outlet: Outlet) -> None:
        # Register-once, at startup: a running worker captures its outlet when it
        # starts, so re-registering a different outlet for a live channel does not
        # hot-swap it.
        self._outlets[outlet.name] = outlet

    async def dispatch(self, out: Deliverable) -> None:
        await self._enqueue(out)

    async def post(self, out: Deliverable) -> None:
        """Send a deliverable that did not come from a turn (e.g. a Sentinel menu).
        Routes like dispatch; the caller stamps source.channel. Returns once the
        event is queued, not once delivered — delivery is the outlet worker's, and
        a full queue backpressures this channel's caller."""
        await self._enqueue(out)

    async def close_stream(self, conversation_id: str) -> None:
        """End a conversation's stream. Routes a close marker through the outlet's
        queue so its done=True chunk follows the last StreamDelta still in flight.
        Driven by a lifecycle event (TurnEnded / TurnFailed); a conversation with
        no open stream is a no-op."""
        channel = self._stream_channel.pop(conversation_id, None)
        if channel is None:
            return
        queue = self._queues.get(channel)
        if queue is not None:
            await queue.put(_StreamClose(conversation_id))

    async def _enqueue(self, out: Deliverable) -> None:
        if out.source is None:
            raise ValueError(f"cannot route a {type(out).__name__} with no source")
        channel = out.source.channel
        if channel not in self._outlets:
            logger.warning("no outlet for channel {!r}; dropping {}", channel, type(out).__name__)
            return
        if isinstance(out, StreamDelta):
            # Remember the channel this stream rides so a later close_stream (driven
            # by a sourceless lifecycle event) can route its marker here.
            self._stream_channel.setdefault(out.conversation_id, channel)
        queue = self._queues.get(channel)
        if queue is None:
            queue = asyncio.Queue(maxsize=_OUTLET_QUEUE_MAXSIZE)
            self._queues[channel] = queue
        worker = self._workers.get(channel)
        if worker is None or worker.done():
            # Restart on done() too, not just absence: the worker is resident
            # (blocks on get), so a dead one would leave its queue unconsumed and
            # silently deadlock this channel's senders. Mirrors the lane worker.
            self._workers[channel] = asyncio.create_task(self._run_outlet(channel))
        await queue.put(out)  # full queue blocks only this channel (per-outlet backpressure)

    async def _run_outlet(self, channel: str) -> None:
        queue = self._queues[channel]
        outlet = self._outlets[channel]
        while True:
            item = await queue.get()
            try:
                if isinstance(item, _StreamClose):
                    await self._close_stream_chunk(outlet, item.conversation_id)
                elif isinstance(item, StreamDelta):
                    await self._stream_chunk(outlet, item)
                else:
                    await self._deliver_with_retry(outlet, item)
            finally:
                # Always mark done — including the eat / retries-exhausted path —
                # so wait_idle's join() reflects every dequeued item, never hangs.
                queue.task_done()

    async def _stream_chunk(self, outlet: Outlet, ev: StreamDelta) -> None:
        # A non-streaming outlet eats the delta (the full text reaches it another
        # way); only an outlet that both can and declares streaming gets chunks.
        if not (isinstance(outlet, SupportsStreaming) and outlet.capabilities.streaming):
            return
        chat_id = ev.source.chat_id
        self._open_streams.setdefault(ev.conversation_id, chat_id)  # first delta opens the stream
        await outlet.send_stream_chunk(chat_id, ev.conversation_id, ev.delta, done=False)

    async def _close_stream_chunk(self, outlet: Outlet, conversation_id: str) -> None:
        chat_id = self._open_streams.pop(conversation_id, None)
        if chat_id is None:
            return  # no open stream (empty turn, or a non-streaming outlet) -> no-op
        if isinstance(outlet, SupportsStreaming) and outlet.capabilities.streaming:
            await outlet.send_stream_chunk(chat_id, conversation_id, "", done=True)

    async def _deliver_with_retry(self, outlet: Outlet, out: Deliverable) -> None:
        delay = _RETRY_BASE_DELAY
        for attempt in range(self._send_max_retries + 1):
            try:
                await outlet.deliver(out)
                return
            except Exception as exc:
                if attempt == self._send_max_retries:
                    logger.error(
                        "delivery failed after {} retries: channel={!r} event={} reason={}",
                        self._send_max_retries,
                        outlet.name,
                        type(out).__name__,
                        exc,
                    )
                    return
                await asyncio.sleep(delay)
                delay *= 2

    def drain(self) -> int:
        """Drop every not-yet-delivered (still-queued) event and return the count.
        Synchronous (no await) so it is atomic against the live workers. This only
        drops queued events; a best-effort flush within a shutdown window is not yet
        implemented."""
        dropped = 0
        for queue in self._queues.values():
            while not queue.empty():
                queue.get_nowait()
                queue.task_done()  # keep unfinished count consistent so join() can't hang
                dropped += 1
        if dropped:
            logger.warning("delivery hub drained {} undelivered events on shutdown", dropped)
        return dropped

    async def wait_idle(self, channel: str) -> None:
        """Block until this channel's outlet has delivered everything queued — the
        render barrier a caller awaits after a turn's result() before it treats the
        output as on-screen (result() means 'no more events', not 'delivered'). A
        channel with nothing ever queued is already idle."""
        queue = self._queues.get(channel)
        if queue is None:
            return
        await queue.join()

    async def aclose(self) -> None:
        """Cancel every outlet worker. Abrupt: in-flight delivery (mid-retry) is
        cancelled, not finished. Finishing the current send within a window is not yet
        implemented."""
        for worker in self._workers.values():
            worker.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()


def make_hub_sink(hub: DeliveryHub) -> Callable[[TurnEvent], Awaitable[None]]:
    """Adapt the hub into a scheduler EventSink: deliverables route through the
    hub; lifecycle events carry no source, so they are dropped here and never
    reach the deliverable-only enqueue path (lifecycle -> taps lands later). The
    REPL and the gateway share this sink; the TUI keeps its own (it fires
    message.complete / error after the render barrier)."""

    async def sink(event: TurnEvent) -> None:
        if isinstance(event, (TurnStarted, TurnFailed, TurnEnded)):
            return
        await hub.dispatch(event)

    return sink
