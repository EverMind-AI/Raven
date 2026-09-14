"""The A2A JSON-RPC binding, on aiohttp.

Written to be deleted. The SDK ships ``add_a2a_routes_to_fastapi()``,
``create_jsonrpc_routes()`` and ``create_agent_card_routes()`` for an ASGI host;
this file exists only because the gateway is aiohttp. If that ever changes, drop
this module and call those -- no A2A logic moves with it.

Two orderings are load-bearing: the version header is checked before the method
is dispatched, and authentication is checked before anything reaches the
request handler, so an unknown caller never starts a turn.
"""

from __future__ import annotations

import json
from typing import Any

from a2a.utils.errors import JSON_RPC_ERROR_CODE_MAP
from aiohttp import web
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtoMessage

from raven.a2a.auth import is_authorized
from raven.a2a.card import CARD_PATH, PROTOCOL_VERSION, build_agent_card
from raven.config.schema import A2aConfig

VERSION_HEADER = "A2A-Version"

#: The JSON-RPC codes this binding emits, derived from the SDK's own canonical map so a
#: conformant client reconstructs the error class we actually meant. Do NOT hand-write these
#: numbers: an earlier draft invented them and disagreed with the SDK on four of nine, which
#: made a real client decode VersionNotSupportedError as TaskNotFoundError.
ERROR_CODES: dict[str, int] = {cls.__name__: code for cls, code in JSON_RPC_ERROR_CODE_MAP.items()}

#: JSON-RPC method -> the ``RequestHandler`` coroutine that serves it.
METHODS: dict[str, str] = {
    "SendMessage": "on_message_send",
    "SendStreamingMessage": "on_message_send_stream",
    "GetTask": "on_get_task",
    "ListTasks": "on_list_tasks",
    "CancelTask": "on_cancel_task",
    "SubscribeToTask": "on_subscribe_to_task",
}

#: The two methods whose handler coroutine is an async generator, not a coroutine
#: returning one value -- these get an SSE response instead of one JSON body.
STREAMING_METHODS: frozenset[str] = frozenset({"SendStreamingMessage", "SubscribeToTask"})


def error_response(name: str, request_id: Any) -> dict[str, Any]:
    """A JSON-RPC error body naming an A2A error type."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": ERROR_CODES.get(name, ERROR_CODES["InternalError"]), "message": name},
    }


def _error_name_for(exc: Exception) -> str:
    """The A2A error name a raised exception should be reported under.

    A real `DefaultRequestHandler`-backed handler raises `InvalidParamsError`
    from two places: malformed params fail conversion before the handler is
    even called, and well-formed but incomplete params pass conversion but
    then fail the SDK's own required-field validation inside the handler
    call. Both must reach the caller as `InvalidParamsError`, not a flattened
    `InternalError` -- so any exception whose class name is one of the SDK's
    own error types is reported as itself; anything else still falls back to
    `InternalError`, unchanged from before.
    """
    name = type(exc).__name__
    return name if name in ERROR_CODES else "InternalError"


def _to_jsonable(value: Any) -> Any:
    """A real `RequestHandler` answers with protobuf (`Task`, `Message`, ...); the
    opaque test double in tests/test_a2a_routes.py answers with a plain dict. Both
    must reach `json_response`/`json.dumps`, so only the protobuf case converts."""
    if isinstance(value, ProtoMessage):
        return MessageToDict(value)
    return value


async def _serve_stream(
    request: web.Request, handler: Any, method_name: str, params: Any, request_id: Any
) -> web.StreamResponse:
    """One `data:` frame per event, each carrying the same JSON-RPC envelope a
    non-streaming call would return once. A mid-stream handler failure still
    reports through a frame -- headers are already flushed, so a status code is
    no longer available to carry the error."""
    response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    try:
        async for event in getattr(handler, method_name)(params, None):
            payload = {"jsonrpc": "2.0", "id": request_id, "result": _to_jsonable(event)}
            await response.write(f"data: {json.dumps(payload)}\n\n".encode())
    except Exception as exc:
        error = error_response(_error_name_for(exc), request_id)
        await response.write(f"data: {json.dumps(error)}\n\n".encode())
    return response


def add_a2a_routes(app: web.Application, config: A2aConfig, handler: Any) -> None:
    """Mount the card and the JSON-RPC endpoint onto `app`."""

    async def serve_card(request: web.Request) -> web.Response:
        # `aiohttp.web` does not re-export `yarl.URL` (there is no `web.URL`); the
        # server's path is always absolute, so plain concatenation is enough.
        base = str(request.url.origin()) + config.server.path
        return web.json_response(MessageToDict(build_agent_card(config, base_url=base)))

    async def serve_rpc(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except Exception:
            body = None
        # A body that parses to something other than a JSON object (null, a number, an
        # array, ...) has no "id" or "method" to read; treat it the same as a parse failure
        # rather than let `.get()` raise past this point.
        if not isinstance(body, dict):
            return web.json_response(error_response("InvalidRequestError", None), status=400)
        request_id = body.get("id")

        if request.headers.get(VERSION_HEADER, "").strip() != PROTOCOL_VERSION:
            return web.json_response(error_response("VersionNotSupportedError", request_id), status=400)

        if not is_authorized(config.server, request.headers.get("Authorization")):
            return web.json_response(error_response("InvalidRequestError", request_id), status=401)

        method = body.get("method", "")
        method_name = METHODS.get(method)
        if method_name is None:
            return web.json_response(error_response("MethodNotFoundError", request_id), status=200)

        params = body.get("params") or {}

        if method in STREAMING_METHODS:
            return await _serve_stream(request, handler, method_name, params, request_id)

        try:
            result = await getattr(handler, method_name)(params, None)
            # Serialization stays inside the guarded region, same as the streaming branch's
            # json.dumps: a result that fails to convert or encode is a caller-facing
            # InternalError, not an unhandled exception that falls through to a bare 500.
            return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": _to_jsonable(result)})
        except Exception as exc:
            return web.json_response(error_response(_error_name_for(exc), request_id), status=200)

    app.router.add_get(CARD_PATH, serve_card)
    app.router.add_post(config.server.path, serve_rpc)
