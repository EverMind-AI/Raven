"""Assembling the inbound face: executor, request handler, and a standalone site.

The SDK's ``DefaultRequestHandler`` implements all eleven protocol methods over a
task store and an event queue; the only thing it lacks is what a turn is, which
is ``RavenAgentExecutor``. This module is where those two meet, so no other file
has to know how the SDK is put together.

``DefaultRequestHandler`` speaks protobuf request types and a real
``ServerCallContext``; ``routes_aiohttp`` speaks plain JSON-RPC ``params`` dicts
and passes ``None`` for context (that is what Task 9's fake-handler tests
exercise, and it is fine for a test double, but a real ``DefaultRequestHandler``
rejects both). ``_RequestHandlerAdapter`` below is the seam between the two: it
presents the same six generic method names ``routes_aiohttp`` calls, and it is
where dict-to-protobuf conversion happens, exactly once per method, so
``routes_aiohttp`` itself never has to know the SDK's proto types exist.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

from a2a.helpers import get_message_text
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    SendMessageRequest,
    SubscribeToTaskRequest,
)
from a2a.utils.errors import InvalidParamsError
from aiohttp import web
from google.protobuf.json_format import ParseDict
from google.protobuf.message import Message as ProtoMessage
from loguru import logger

from raven.a2a.card import build_agent_card
from raven.a2a.executor import RavenAgentExecutor, RunTurn
from raven.a2a.routes_aiohttp import add_a2a_routes
from raven.config.schema import A2aConfig
from raven.contracts.asking import QuestionResponder
from raven.permissions.turn import start_permission_turn
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

#: JSON-RPC method name -> the protobuf request type ``ParseDict`` should build.
#: Keyed by the same ``on_*`` names ``routes_aiohttp.METHODS`` maps JSON-RPC
#: method names to, so this table and that one stay in lockstep by construction.
_REQUEST_TYPES: dict[str, type[ProtoMessage]] = {
    "on_message_send": SendMessageRequest,
    "on_message_send_stream": SendMessageRequest,
    "on_get_task": GetTaskRequest,
    "on_list_tasks": ListTasksRequest,
    "on_cancel_task": CancelTaskRequest,
    "on_subscribe_to_task": SubscribeToTaskRequest,
}


def _parse_params(method_name: str, params: Any) -> ProtoMessage:
    """Dict -> the protobuf request type the real handler wants.

    Two distinct failures both need to reach the caller as ``InvalidParamsError``:
    a malformed shape (a string where a repeated field belongs) makes ``ParseDict``
    itself raise, caught and re-raised here; a well-formed but incomplete one (a
    required field simply absent) parses fine here and only fails later, inside
    the delegating handler call, via the SDK's own ``validate_proto_required_fields``
    -- that second path is deliberately left to propagate uncaught out of this
    module, since ``routes_aiohttp``'s existing broad ``except Exception`` around
    the whole call already covers it.
    """
    message = _REQUEST_TYPES[method_name]()
    try:
        ParseDict(params or {}, message, ignore_unknown_fields=True)
    except Exception as exc:
        raise InvalidParamsError(message=str(exc)) from exc
    return message


class _RequestHandlerAdapter:
    """Presents the six generic ``on_*`` names ``routes_aiohttp`` calls, backed by
    a real ``DefaultRequestHandler``.

    Deliberately not a ``DefaultRequestHandler`` subclass: every method it
    overrides would take a different, incompatible signature (a dict instead of
    a proto request, ``None`` instead of a real context), which is a Liskov
    violation dressed up as inheritance. Composition says the same thing
    honestly, and ``routes_aiohttp.add_a2a_routes``'s ``handler`` parameter is
    typed ``Any`` -- nothing statically requires the literal SDK class.
    """

    def __init__(self, handler: DefaultRequestHandler) -> None:
        self._handler = handler

    async def on_message_send(self, params: Any, _context: Any) -> Any:
        """Dispatch a fresh `SendMessage`, or answer one parked on a question.

        A resume names an existing task id in `message.task_id` (empty, its
        proto3 default, on a fresh call). When that task has a turn parked in
        `RavenAgentExecutor` (raven/a2a/asking.py's `A2aQuestionBroker`), this
        answers that waiting turn and returns its current status instead of
        handing the message to `DefaultRequestHandler`, which would otherwise
        start a second turn for the same task id.
        """
        request = _parse_params("on_message_send", params)
        task_id = request.message.task_id
        if task_id:
            executor = self._handler.agent_executor
            if executor.answer(task_id, get_message_text(request.message)):
                return await self._handler.on_get_task(GetTaskRequest(id=task_id), ServerCallContext())
        return await self._handler.on_message_send(request, ServerCallContext())

    async def on_message_send_stream(self, params: Any, _context: Any) -> AsyncGenerator[Any, None]:
        request = _parse_params("on_message_send_stream", params)
        async for event in self._handler.on_message_send_stream(request, ServerCallContext()):
            yield event

    async def on_get_task(self, params: Any, _context: Any) -> Any:
        request = _parse_params("on_get_task", params)
        return await self._handler.on_get_task(request, ServerCallContext())

    async def on_list_tasks(self, params: Any, _context: Any) -> Any:
        request = _parse_params("on_list_tasks", params)
        return await self._handler.on_list_tasks(request, ServerCallContext())

    async def on_cancel_task(self, params: Any, _context: Any) -> Any:
        request = _parse_params("on_cancel_task", params)
        return await self._handler.on_cancel_task(request, ServerCallContext())

    async def on_subscribe_to_task(self, params: Any, _context: Any) -> AsyncGenerator[Any, None]:
        request = _parse_params("on_subscribe_to_task", params)
        async for event in self._handler.on_subscribe_to_task(request, ServerCallContext()):
            yield event


def build_request_handler(
    config: A2aConfig,
    run_turn: RunTurn,
    *,
    base_url: str = "",
) -> _RequestHandlerAdapter:
    """A request handler serving `run_turn` as this agent's behaviour.

    Returns the dict-speaking adapter, not a bare ``DefaultRequestHandler``:
    ``routes_aiohttp``'s two call sites hand every method a JSON-RPC ``params``
    dict and a ``None`` context, which only the adapter accepts.

    `agent_card` is required by the SDK, not optional -- a two-argument call
    raises TypeError -- so the config has to reach here to build one.
    """
    handler = DefaultRequestHandler(
        agent_executor=RavenAgentExecutor(run_turn),
        task_store=InMemoryTaskStore(),
        agent_card=build_agent_card(config, base_url=base_url or config.server.path),
    )
    return _RequestHandlerAdapter(handler)


async def serve_standalone(
    config: A2aConfig,
    *,
    host: str,
    port: int,
    run_turn: RunTurn,
) -> None:
    """Run the A2A face on its own aiohttp site until cancelled."""
    app = web.Application()
    add_a2a_routes(app, config, build_request_handler(config, run_turn))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("A2A serving on http://{}:{}{}", host, port, config.server.path)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def _run_one_shot_turn(
    agent_loop: Any, prompt: str, *, conversation_id: str, broker: QuestionResponder | None
) -> str:
    """Run one A2A-inbound turn directly through `AgentLoop.run_turn`, bypassing
    the Scheduler/hub/dispatcher entirely -- mirrors `RpcTurnRunner`'s
    `Origin.CRON` branch in `raven/rpc/spine.py`, the precedent for a
    non-interactive, system-origin turn that wants one reply back rather than a
    token stream. `raven agent -m`'s own path (`raven/cli/agent_commands.py`)
    goes through `build_one_shot_spine` / `Scheduler.submit` / a `DeliveryHub`
    instead, but that path is for an interactive `Origin.USER` turn a person is
    watching; an inbound A2A call has no such watcher, so it does not need that
    machinery any more than a cron fan-out does.

    `Origin.HEARTBEAT`: an A2A call is not `CRON` (no cron job id, no per-session
    read-back table to write into), not `SUBAGENT` (no delegation chain, no
    parent conversation, and that origin gates personalization and skips hooks
    in ways that assume a sub-agent relay), and not `SENTINEL` (skips hooks and
    carries a `SentinelExtras` payload this call has none of) -- it is a
    proactive, system-origin turn a peer agent asked for, which is exactly what
    `HEARTBEAT` already means elsewhere (`raven/cli/gateway_commands.py`'s
    `on_heartbeat_execute`). `HEARTBEAT`'s only special case is scheduler lane
    pooling, irrelevant here since this path never reaches the Scheduler.

    `conversation_id` is minted by the caller (`RavenAgentExecutor.execute`), not
    here: an A2A `SendMessage` carries no conversation of its own (unlike a
    TUI/gateway turn, which reuses one across a chat), and the executor needs the
    value before this coroutine ever returns it, to translate a later `answer(task_id, ...)`
    into the conversation id `broker` actually parks futures under.

    `broker` is installed onto the loop's `ask_user` tool only when given: see
    `make_run_turn_from_factory` for why the gateway-mounted path always passes
    `None` here instead of forwarding its caller's broker.

    No defensive try/except here: `RavenAgentExecutor.execute` (raven/a2a/executor.py)
    already wraps its call into this function and reports a failed task on any
    exception, so catching again here would only hide the same failure twice.
    """
    req = TurnRequest(
        origin=Origin.HEARTBEAT,
        source=Source(channel="a2a", chat_id=conversation_id, sender_id="a2a", chat_type=ChatType.DM),
        text=prompt,
        conversation=conversation_id,
    )
    start_permission_turn(None, conversation_id=conversation_id, turn_id=req.turn_id or "")

    # Function-level: raven/rpc/spine.py's RpcTurnRunner.run defers this same
    # import with the same reasoning -- the acp client family is future shelf
    # cargo and must not be named at this module's import time.
    from raven.acp_client.asker import start_ask_turn

    start_ask_turn(None, None, conversation_id=conversation_id)

    # Same guard raven/rpc/bootstrap.py uses to wire the gateway's own broker:
    # `broker` is None on the gateway-mounted path, so this loop's `ask_user`
    # (already wired to the gateway's own broker there) is left untouched.
    if broker is not None and (ask_tool := agent_loop.tools.get("ask_user")) is not None:
        if hasattr(ask_tool, "set_broker"):
            ask_tool.set_broker(broker)

    async def emit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def drain(*_args: Any, **_kwargs: Any) -> None:
        return None

    text_sink: dict[str, str] = {}
    await agent_loop.run_turn(req, emit, drain, stream=False, text_sink=text_sink)
    return text_sink.get("text", "")


def make_run_turn(agent_loop: Any) -> RunTurn:
    """`run_turn` bound to one already-built, process-lifetime `AgentLoop` --
    what the standalone CLI (`raven a2a serve`) uses, since it owns exactly one
    loop for its whole run."""

    async def run_turn(prompt: str, *, conversation_id: str, broker: QuestionResponder | None) -> str:
        return await _run_one_shot_turn(agent_loop, prompt, conversation_id=conversation_id, broker=broker)

    return run_turn


def make_run_turn_from_factory(agent_loop_factory: Callable[[], Any | None]) -> RunTurn:
    """`run_turn` that resolves the gateway's `agent_loop_factory` fresh on every
    call, instead of capturing one loop at `build_app` time.

    Mirrors the try/except guard `raven/rpc/methods/session.py`'s
    `_safe_invoke_factory` and `raven/rpc/methods/turn.py`'s `turn_send` both
    place around `agent_loop_factory()`: a boot race or any other construction
    failure degrades to "not ready yet" rather than crashing the turn.
    """

    async def run_turn(prompt: str, *, conversation_id: str, broker: QuestionResponder | None) -> str:
        try:
            agent_loop = agent_loop_factory()
        except Exception:
            logger.exception("a2a runtime: agent_loop_factory raised")
            agent_loop = None
        if agent_loop is None:
            raise RuntimeError("agent_loop is not available yet")
        # This loop is the gateway's own, shared with its RPC/TUI turns, whose
        # `ask_user` already has the gateway's `QuestionBroker` installed
        # (raven/rpc/bootstrap.py). Installing `broker` on top of it here would
        # silently redirect every gateway user's question to the A2A caller
        # instead, so it is dropped: a question mid-turn still goes to the
        # gateway's own user, per docs/specs/2026-09-13-a2a-protocol-design.md.
        return await _run_one_shot_turn(agent_loop, prompt, conversation_id=conversation_id, broker=None)

    return run_turn
