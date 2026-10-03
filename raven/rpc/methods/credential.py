"""Answer a credential card: the value the user typed, or a skip.

``credential.submit`` carries the value to the broker that owns the open
request, which writes it through the request's sink before the waiting tool
resumes; the reply says only whether it was saved, and why not. The value is
not logged here or anywhere it travels. ``credential.pending`` redraws the
cards a reloaded page lost. All three are scoped the way the request was sent
(``connection.conversation_scoped``): a socket that does not own the
conversation cannot answer, skip or even see its request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.rpc.connection import owns_conversation

if TYPE_CHECKING:
    from raven.rpc.credential_broker import CredentialBroker
    from raven.rpc.dispatcher import Dispatcher


def _conversation(params: dict[str, Any]) -> str:
    return str(params.get("session_id") or params.get("conversation_id") or "")


async def credential_submit(params: dict[str, Any], *, credential_broker: "CredentialBroker") -> dict[str, Any]:
    conversation_id = _conversation(params)
    request_id = str(params.get("request_id") or "")
    value = params.get("value")
    if not request_id or not conversation_id or not isinstance(value, str) or not owns_conversation(conversation_id):
        return {"ok": False, "error": "This request is no longer open."}
    return await credential_broker.submit(request_id, conversation_id, value)


async def credential_skip(params: dict[str, Any], *, credential_broker: "CredentialBroker") -> dict[str, bool]:
    conversation_id = _conversation(params)
    request_id = str(params.get("request_id") or "")
    if not request_id or not conversation_id or not owns_conversation(conversation_id):
        return {"ok": False}
    return {"ok": credential_broker.skip(request_id, conversation_id)}


async def credential_pending(
    params: dict[str, Any], *, credential_broker: "CredentialBroker"
) -> dict[str, list[dict[str, Any]]]:
    asked = credential_broker.pending(_conversation(params) or None)
    return {"requests": [r for r in asked if owns_conversation(r.get("conversation_id"))]}


def register_credential_methods(dispatcher: "Dispatcher", *, credential_broker: "CredentialBroker") -> None:
    async def _submit(params: dict[str, Any]) -> dict[str, Any]:
        return await credential_submit(params, credential_broker=credential_broker)

    async def _skip(params: dict[str, Any]) -> dict[str, bool]:
        return await credential_skip(params, credential_broker=credential_broker)

    async def _pending(params: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        return await credential_pending(params, credential_broker=credential_broker)

    dispatcher.register("credential.submit", _submit)
    dispatcher.register("credential.skip", _skip)
    dispatcher.register("credential.pending", _pending)


__all__ = ["credential_pending", "credential_skip", "credential_submit", "register_credential_methods"]
