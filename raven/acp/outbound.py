"""Requests this agent makes of its client, and the answers coming back.

Nothing in raven had this. ``Dispatcher`` is inbound-only and ``send_frame`` is
fire-and-forget, so a method that needs the client to *decide* something -- which
is what a permission prompt is -- had no mechanism at all. The only working
example in the repo is on the other side of the wire, in
``raven/agent/acp/client.py``, and this is its mirror.

Three things make it more than a dict:

* **The id space is this agent's.** A client's request ids and an agent's are
  independent, so ``"id": 1`` inbound and ``"id": 1`` outbound are unrelated
  frames. Mixing them into one map would let a client's own request id resolve a
  promise the agent is holding.
* **Every wait ends.** A client is free to never answer -- and one of the four
  measured failure modes is exactly that. A pending future with no deadline is a
  turn that never finishes, so every call carries a timeout and the timeout is a
  normal outcome rather than an error.
* **Shutdown is an answer.** When the connection closes, everything still
  waiting is failed rather than left for the garbage collector, because the code
  awaiting it has cleanup to run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from typing import Any

from loguru import logger

from raven.acp import protocol

DEFAULT_REQUEST_TIMEOUT_S = 300.0
"""How long to wait for a client to answer, by default.

Five minutes, not the thirty-five seconds the RPC approval broker uses. That
ceiling exists because a terminal overlay owns a visible countdown; here the
person is reading a diff in an editor, and a permission prompt that expires
itself after half a minute reads as an agent that gave up. Long, but finite: a
client that has stopped answering must not hold a turn open forever.
"""


class RequestFailedError(Exception):
    """The client answered with a JSON-RPC error."""

    def __init__(self, method: str, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{method} failed: [{code}] {message}")
        self.method = method
        self.code = code
        self.message = message
        self.data = data


class ConnectionClosedError(Exception):
    """The connection went away while this request was outstanding."""


@dataclass
class _Pending:
    method: str
    future: asyncio.Future[Any]


class OutboundRequests:
    """Mint outbound requests, and match responses back to them.

    ``emit`` writes one finished frame. It is synchronous for the same reason the
    translator's is: the frame writer is a write plus a flush with no suspension
    point, which is what keeps the order of frames on the wire equal to the order
    they were produced in.
    """

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit = emit
        self._ids = count(1)
        self._pending: dict[int, _Pending] = {}
        self._closed = False

    @property
    def in_flight(self) -> int:
        return len(self._pending)

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> Any:
        """Ask the client for something and return its result.

        Raises :class:`RequestFailedError` when the client answers with an error,
        :class:`ConnectionClosedError` when the connection goes first, and
        ``TimeoutError`` when nothing arrives. Three distinct exceptions because
        the caller's right answer differs: an error is the client refusing, a
        close is the session ending, and a timeout is a client that is still
        there and not answering.
        """
        if self._closed:
            raise ConnectionClosedError(f"{method} not sent: the connection is closed")
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _Pending(method=method, future=future)
        try:
            self._emit(protocol.request(request_id, method, params))
            return await asyncio.wait_for(future, timeout)
        finally:
            # Popped here rather than in ``resolve``: a timeout and a cancellation
            # both leave the entry behind otherwise, and a late response would
            # then resolve a future nobody is waiting on.
            self._pending.pop(request_id, None)

    def resolve(self, frame: dict[str, Any]) -> bool:
        """Match one inbound response frame, reporting whether it was ours.

        ``False`` means the frame answers no request this agent sent. Not an
        error -- a client may answer a request it invented, or one this agent
        already timed out -- but the caller wants to know, because a frame that
        belongs to nobody is otherwise indistinguishable from one that was
        handled.
        """
        request_id = frame.get("id")
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            # Every id this agent mints is a plain int. A string id in a response
            # can only be a client answering something it made up, or echoing an
            # id in the wrong type -- and either way there is nothing here to
            # match it against.
            return False
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        error = frame.get("error")
        if isinstance(error, dict):
            pending.future.set_exception(
                RequestFailedError(
                    pending.method,
                    int(error.get("code", protocol.INTERNAL_ERROR)),
                    str(error.get("message", "request failed")),
                    error.get("data"),
                )
            )
            return True
        pending.future.set_result(frame.get("result"))
        return True

    def close(self) -> None:
        """Fail everything outstanding, and refuse anything new.

        A one-way latch, and both halves matter: failing the pending futures lets
        the code awaiting them run its cleanup, and refusing new calls stops a
        handler that is still unwinding from writing to a closed channel.
        """
        self._closed = True
        pending, self._pending = self._pending, {}
        for request_id, entry in pending.items():
            if not entry.future.done():
                entry.future.set_exception(
                    ConnectionClosedError(f"{entry.method} (id {request_id}) was outstanding when the client left")
                )
        if pending:
            logger.debug("acp: failed {} outstanding outbound request(s) on close", len(pending))


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_S",
    "ConnectionClosedError",
    "OutboundRequests",
    "RequestFailedError",
]
