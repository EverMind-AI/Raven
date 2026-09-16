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
from collections.abc import Callable
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.types import Message, SendMessageResponse, Task
from a2a.utils.errors import JSON_RPC_ERROR_CODE_MAP
from a2a.utils.proto_utils import to_stream_response
from aiohttp import web
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtoMessage

from raven.a2a.auth import is_authorized
from raven.a2a.card import CARD_PATH, PROTOCOL_VERSION, build_agent_card
from raven.config.schema import A2aConfig

CALL_BASE_URL = "base_url"
"""Key under which a call's own interface URL rides `ServerCallContext.state`.

The URL a card advertises is a property of the request that asked for it, not
of the process: one face answers on every name it is reachable under -- an SSH
tunnel, a published container port, a reverse proxy -- and each caller must be
sent back to the origin it actually used. The card route has always read that
off the live request; this key is how the RPC route hands the handler the same
fact, so the extended card cannot disagree with the public one about where
this agent is.
"""

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
    "GetExtendedAgentCard": "on_get_extended_agent_card",
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


def _as_stream_response(event: Any) -> Any:
    """The wire envelope for one streamed event.

    `RequestHandler.on_message_send_stream` yields a bare `Event` -- a `Task`,
    `Message`, `TaskStatusUpdateEvent` or `TaskArtifactUpdateEvent` -- but the
    JSON-RPC binding carries a `StreamResponse`, whose oneof is what names which
    of the four arrived. A conformant client parses `result` as that envelope, so
    a bare event makes every frame unparseable: the SDK's own client rejects it
    with "StreamResponse has no field named ...", before any of the content is
    read. The SDK owns that mapping, and taking its helper instead of rewriting
    the four cases is the rule ERROR_CODES above already follows.

    The isinstance guard is the one `_to_jsonable` uses, and for the same reason:
    the opaque double in tests/test_a2a_routes.py yields plain dicts, which have
    no envelope to be put into.
    """
    return to_stream_response(event) if isinstance(event, ProtoMessage) else event


def _as_send_message_response(result: Any) -> Any:
    """The wire envelope for a non-streaming `SendMessage` reply.

    The same rule as `_as_stream_response`, on the other half of the dispatch.
    `RequestHandler.on_message_send` answers with a bare `Task` or `Message`,
    but the JSON-RPC binding carries `SendMessageResponse`, whose oneof is what
    names which of the two arrived. A conformant client parses `result` as that
    envelope and rejects a bare `Task` on the field the envelope has no room for
    -- so a successful turn is unreadable to every client that chooses the
    non-streaming call.

    Only this method needs it, measured against the SDK's own transport rather
    than assumed: `get_task` and `cancel_task` parse a bare `Task`, and
    `on_list_tasks` already returns `ListTasksResponse` itself.
    """
    if isinstance(result, Task):
        return SendMessageResponse(task=result)
    if isinstance(result, Message):
        return SendMessageResponse(message=result)
    # The opaque double in tests/test_a2a_routes.py answers with plain dicts,
    # which have no envelope to be put into -- the guard `_to_jsonable` uses.
    return result


RESPONSE_ENVELOPES: dict[str, Callable[[Any], Any]] = {"SendMessage": _as_send_message_response}
"""Methods whose JSON-RPC result is an envelope around the handler's return.

A table rather than a branch, beside `METHODS` and `STREAMING_METHODS`, because
the set is a property of the binding: a method added to `METHODS` has to be
checked against the SDK transport's parser, and an absent entry here is the
claim that its handler already returns the type the client parses.
"""


def _interface_url(request: web.Request, config: A2aConfig) -> str:
    """Where this agent's JSON-RPC interface is, as *this* caller reached it.

    One computation for both routes. `aiohttp.web` does not re-export `yarl.URL`
    (there is no `web.URL`); the server's path is always absolute, so plain
    concatenation is enough.
    """
    return str(request.url.origin()) + config.server.path


async def _serve_stream(
    request: web.Request, handler: Any, method_name: str, params: Any, request_id: Any, context: Any
) -> web.StreamResponse:
    """One `data:` frame per event, each carrying the same JSON-RPC envelope a
    non-streaming call would return once. A mid-stream handler failure still
    reports through a frame -- headers are already flushed, so a status code is
    no longer available to carry the error."""
    response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    try:
        async for event in getattr(handler, method_name)(params, context):
            payload = {"jsonrpc": "2.0", "id": request_id, "result": _to_jsonable(_as_stream_response(event))}
            await response.write(f"data: {json.dumps(payload)}\n\n".encode())
    except Exception as exc:
        error = error_response(_error_name_for(exc), request_id)
        await response.write(f"data: {json.dumps(error)}\n\n".encode())
    return response


def add_a2a_routes(app: web.Application, config: A2aConfig, handler: Any) -> None:
    """Mount the card and the JSON-RPC endpoint onto `app`."""

    async def serve_card(request: web.Request) -> web.Response:
        base = _interface_url(request, config)
        # Asked of the handler rather than assumed: whether an extended card can
        # be served is the handler's fact, and a card that advertises one this
        # process cannot answer sends the caller to a refusal.
        extended = bool(getattr(handler, "serves_extended_card", False))
        card = build_agent_card(config, base_url=base, extended_available=extended)
        return web.json_response(MessageToDict(card))

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

        # `method` is checked for being a string, not just for being a known name:
        # a JSON array or object here is unhashable, and the dict lookup below
        # would raise past this function's only try block one line later.
        method = body.get("method", "")
        method_name = METHODS.get(method) if isinstance(method, str) else None
        if method_name is None:
            return web.json_response(error_response("MethodNotFoundError", request_id), status=200)

        params = body.get("params") or {}
        # Carries this request's own origin to the handler. `GetExtendedAgentCard`
        # builds a card and so needs the same answer `serve_card` gives; handing it
        # to every method keeps one dispatch rather than a second path that has to
        # be kept in step.
        context = ServerCallContext(state={CALL_BASE_URL: _interface_url(request, config)})

        if method in STREAMING_METHODS:
            return await _serve_stream(request, handler, method_name, params, request_id, context)

        try:
            result = await getattr(handler, method_name)(params, context)
            if (envelope := RESPONSE_ENVELOPES.get(method)) is not None:
                result = envelope(result)
            # Serialization stays inside the guarded region, same as the streaming branch's
            # json.dumps: a result that fails to convert or encode is a caller-facing
            # InternalError, not an unhandled exception that falls through to a bare 500.
            return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": _to_jsonable(result)})
        except Exception as exc:
            return web.json_response(error_response(_error_name_for(exc), request_id), status=200)

    app.router.add_get(CARD_PATH, serve_card)
    app.router.add_post(config.server.path, serve_rpc)
