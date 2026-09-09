"""ConfirmBroker — sync↔async confirm round-trip for in-process CLI dispatch.

Mirrors :class:`SubscriptionEmitter`: owned by
the RPC server, constructed with ``send_frame`` bound to ``RpcServer.send_frame``,
and passed into ``register_confirm_methods(dispatcher, confirm_broker=...)``.

Problem: ``cli.dispatch`` runs the EC CLI in ``asyncio.to_thread``; a
``typer.confirm`` inside that worker thread reads the non-TTY stdin and raises
``click.Abort``. Approach A intercepts the confirm (see ``_confirm_injection``)
and bridges into this broker: the worker thread blocks on
``run_coroutine_threadsafe(broker.await_confirm(...), loop).result()`` while the
event loop emits a ``confirm.request`` notification and awaits the matching
``confirm.respond`` (delivered via :meth:`resolve`).

Every fail-safe path (hard-limit timeout, connection EOF via :meth:`cancel_all`,
internal error) resolves to the prompt's ``default`` — ``False`` for the 7
destructive call sites, i.e. "cancel".
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from loguru import logger

# Hard upper bound on how long a single confirm may stay pending on the
# backend, independent of the frontend's 30s visible countdown (35 = 30 + 5s
# network slack). On expiry the wait fail-safes to the prompt default.
_CONFIRM_HARD_LIMIT_S = 35.0

SendFrame = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class _PendingConfirm:
    future: asyncio.Future
    default: bool


class ConfirmBroker:
    """Emits ``confirm.request`` notifications and awaits ``confirm.respond``."""

    def __init__(self, send_frame: SendFrame) -> None:
        self._send_frame = send_frame
        self._pending: dict[str, _PendingConfirm] = {}

    async def await_confirm(self, prompt: str, *, default: bool) -> bool:
        """Emit a ``confirm.request`` and await the matching answer.

        Returns ``default`` on hard-limit timeout, cancellation, EOF
        (:meth:`cancel_all`), or any internal error — never raises to the
        caller (the worker thread must always get a bool back).
        """
        request_id = uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = _PendingConfirm(future=future, default=default)
        try:
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "confirm.request",
                    "params": {
                        "request_id": request_id,
                        "prompt": prompt,
                        "default": default,
                    },
                }
            )
            return await asyncio.wait_for(future, _CONFIRM_HARD_LIMIT_S)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return default
        except Exception:  # noqa: BLE001 — fail-safe: worker thread needs a bool
            logger.exception("confirm_broker: await_confirm failed for {}", request_id)
            return default
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, answer: bool) -> bool:
        """Resolve a pending confirm. Idempotent: unknown/done → ``False``."""
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(answer)
        return True

    def cancel_all(self) -> None:
        """Fail-safe every pending confirm to its default (connection EOF)."""
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(pending.default)


__all__ = ["ConfirmBroker", "SendFrame", "_CONFIRM_HARD_LIMIT_S"]
