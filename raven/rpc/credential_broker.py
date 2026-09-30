"""Credential round-trip: the user types a secret into the page, never into the chat.

A tool that needs a key, a token or a password names where it goes
(:class:`~raven.contracts.asking.CredentialRequest`) and awaits
:meth:`CredentialBroker.request_credential`. The broker sends
``credential.request`` with what the card shows -- a label, a note, whether a
value is already set -- and never the target itself: the page does not need to
know where a value is written, and nothing on the wire invites it to choose.

The answer comes back as ``credential.submit`` (the value) or
``credential.skip``. A submit is written here, through the sink its target
names, before the waiting tool is resumed; the tool learns only ``SAVED`` or
``SKIPPED``. A sink that refuses the value (a bad key shape, a provider that
takes none) is reported to the page, which keeps its card up, and the request
stays open for another try. The value itself is not logged, not echoed and not
kept once the sink returns.

One card per conversation at a time, and a deadline: a request no one answers
within ``timeout_s`` is ``SKIPPED``, so a turn never waits on a card that
nobody is looking at. Every ending emits ``credential.closed``, and
``pending`` hands a page that reloaded the requests still open, the way the
approval broker does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from loguru import logger

from raven.contracts.asking import CredentialOutcome, CredentialRequest

SendFrame = Callable[[dict[str, Any]], Awaitable[None]]
#: Writes one value to where a target's reference points; raises with a
#: sentence the user can act on when it will not take it.
Sink = Callable[[str, str], Awaitable[None]]

CREDENTIAL_REQUEST_METHOD = "credential.request"
CREDENTIAL_CLOSED_METHOD = "credential.closed"

#: Long enough to go and find a key in another tab; short enough that a turn
#: nobody is watching does not sit on a card for the rest of the day.
DEFAULT_TIMEOUT_S = 15 * 60.0


class CredentialRefusedError(Exception):
    """A sink would not take the value; the message says why, without the value."""


@dataclass
class _Pending:
    conversation_id: str
    target: str
    future: asyncio.Future[CredentialOutcome]
    params: dict[str, Any]


class CredentialBroker:
    def __init__(
        self,
        send_frame: SendFrame,
        *,
        sinks: dict[str, Sink] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._send_frame = send_frame
        self._sinks: dict[str, Sink] = dict(sinks or {})
        self._timeout_s = timeout_s
        self._pending: dict[str, _Pending] = {}
        self._lanes: dict[str, asyncio.Lock] = {}

    def add_sink(self, name: str, sink: Sink) -> None:
        self._sinks[name] = sink

    def can_write(self, target: str) -> bool:
        return target.partition(":")[0] in self._sinks

    async def request_credential(
        self, *, conversation_id: str, turn_id: str, request: CredentialRequest
    ) -> CredentialOutcome:
        if not conversation_id or not self.can_write(request.target):
            return CredentialOutcome.SKIPPED
        lane = self._lanes.setdefault(conversation_id, asyncio.Lock())
        async with lane:
            return await self._ask(conversation_id, turn_id, request)

    async def _ask(self, conversation_id: str, turn_id: str, request: CredentialRequest) -> CredentialOutcome:
        request_id = uuid4().hex
        future: asyncio.Future[CredentialOutcome] = asyncio.get_running_loop().create_future()
        params = {
            "request_id": request_id,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "label": request.label,
            "note": request.note,
            "replaces": request.replaces,
        }
        self._pending[request_id] = _Pending(conversation_id, request.target, future, params)
        reason = "skipped"
        try:
            await self._send_frame({"jsonrpc": "2.0", "method": CREDENTIAL_REQUEST_METHOD, "params": params})
            outcome = await asyncio.wait_for(future, timeout=self._timeout_s)
            reason = outcome.value
            return outcome
        except TimeoutError:
            reason = "timeout"
            return CredentialOutcome.SKIPPED
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception:  # noqa: BLE001 - an undeliverable card is a skip, never a failed turn
            logger.exception("credential request could not be delivered")
            return CredentialOutcome.SKIPPED
        finally:
            self._pending.pop(request_id, None)
            try:
                await self._send_frame(
                    {
                        "jsonrpc": "2.0",
                        "method": CREDENTIAL_CLOSED_METHOD,
                        "params": {"request_id": request_id, "conversation_id": conversation_id, "reason": reason},
                    }
                )
            except Exception:  # noqa: BLE001 - the close is a courtesy to the page
                logger.debug("credential.closed could not be sent for {}", request_id)

    async def submit(self, request_id: str, conversation_id: str, value: str) -> dict[str, Any]:
        """Write ``value`` through the request's sink; resume the tool once it is written."""
        pending = self._pending.get(request_id)
        if pending is None or pending.conversation_id != conversation_id or pending.future.done():
            return {"ok": False, "error": "This request is no longer open."}
        if not value.strip():
            return {"ok": False, "error": "Nothing was entered."}
        sink_name, _, reference = pending.target.partition(":")
        sink = self._sinks.get(sink_name)
        if sink is None:
            return {"ok": False, "error": "This credential cannot be saved from here."}
        try:
            await sink(reference, value.strip())
        except CredentialRefusedError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - reported to the page; the value is not in the message
            logger.warning("credential sink {} failed: {}", sink_name, type(exc).__name__)
            return {"ok": False, "error": "It could not be saved; try again, or enter it in Settings."}
        if not pending.future.done():
            pending.future.set_result(CredentialOutcome.SAVED)
        return {"ok": True}

    def skip(self, request_id: str, conversation_id: str) -> bool:
        pending = self._pending.get(request_id)
        if pending is None or pending.conversation_id != conversation_id or pending.future.done():
            return False
        pending.future.set_result(CredentialOutcome.SKIPPED)
        return True

    def pending(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        return [
            dict(p.params)
            for p in self._pending.values()
            if not p.future.done() and (conversation_id is None or p.conversation_id == conversation_id)
        ]

    def pending_count(self) -> int:
        return sum(1 for p in self._pending.values() if not p.future.done())

    def cancel_all(self) -> None:
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(CredentialOutcome.SKIPPED)


__all__ = [
    "CREDENTIAL_CLOSED_METHOD",
    "CREDENTIAL_REQUEST_METHOD",
    "CredentialBroker",
    "CredentialRefusedError",
    "Sink",
]
