"""``confirm.respond`` RPC handler — confirm round-trip answer sink.

The frontend answers a ``confirm.request``
(emitted by :class:`ConfirmBroker` from inside a paused ``cli.dispatch``) by
calling ``confirm.respond {request_id, answer}``; this handler resolves the
matching pending future on the broker.

Registered via a closure that pre-binds the broker (mirrors
``register_turn_methods`` binding the ``emitter``). Gated on a non-None broker
by the umbrella, so the demo / test paths that build no broker do not register
it — keeping the umbrella-vs-production drift test balanced.

This method is intentionally NOT in ``METHOD_MODELS`` / ``openrpc.json`` — like
the existing clarify/sudo/secret round-trips, the confirm pair lives outside
the cross-language schema-parity contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.tui_rpc.confirm_broker import ConfirmBroker
    from raven.tui_rpc.dispatcher import Dispatcher


async def confirm_respond(params: dict[str, Any], *, confirm_broker: "ConfirmBroker") -> dict:
    """Resolve a pending confirm. Unknown/expired ``request_id`` → ``{ok: False}``."""
    request_id = str(params.get("request_id", ""))
    answer = bool(params.get("answer", False))
    ok = confirm_broker.resolve(request_id, answer)
    return {"ok": ok}


def register_confirm_methods(dispatcher: "Dispatcher", *, confirm_broker: "ConfirmBroker") -> None:
    """Register ``confirm.respond`` with the broker pre-bound."""

    async def _respond(params: dict[str, Any]) -> dict:
        return await confirm_respond(params, confirm_broker=confirm_broker)

    dispatcher.register("confirm.respond", _respond)


__all__ = ["confirm_respond", "register_confirm_methods"]
