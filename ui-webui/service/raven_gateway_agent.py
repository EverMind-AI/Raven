"""P2: drive the chat through a persistent ``raven gateway`` over its web
WebSocket channel. This is the only chat backend the service supports.

Two pieces:

- ``GatewayClient`` — one shared WebSocket connection to the gateway's web
  channel (ui-webui P1). It multiplexes: a background read loop resolves RPC
  responses (by ``id``) and routes event notifications (by ``subscription_id``)
  into per-session queues.
- ``RavenGatewayAgent`` — a duck-typed drop-in for the AgentScope chat agent.
  ``reply_stream`` sends one ``turn.send`` over the WS and translates the streamed
  spine wire events (message.start / thinking.delta / token.delta / tool.* /
  message.complete / error) into the AgentScope event objects the web UI already
  renders, sourced from structured events (no regex).

Wire protocol: see ui-webui/docs/plans/2026-07-23-p1-gateway-web-channel.md §4.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

from agentscope.event import (
    ReplyEndEvent,
    ReplyEndReason,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import ToolResultState
from agentscope.state import AgentState
from raven_kb_retrieval import KnowledgeRetriever

try:  # aiohttp is the WS client (present in the ravenx env)
    import aiohttp
except Exception:  # pragma: no cover - import guard
    aiohttp = None  # type: ignore

# Set by main.py; reserved for CUSTOM event publishing (subagent instances /
# DAG viz) in P2.3.
MESSAGE_BUS = None
STORAGE = None

GATEWAY_WS_URL = os.getenv("RAVEN_GATEWAY_WS_URL", "ws://127.0.0.1:8765/ws")
GATEWAY_WS_TOKEN = os.getenv("RAVEN_GATEWAY_WS_TOKEN") or None
_CLIENT_VERSION = "0.1.0"
_ERROR_DETAIL_MAX_CHARS = 200


def gateway_http_base() -> str:
    """HTTP origin of the gateway, derived from its WS URL.

    The manifest deliberately carries no host (it is persisted into message
    history, where a host captured at delivery time can later be wrong), so the
    origin is composed at request time from the one URL the service already has.
    """
    base = GATEWAY_WS_URL
    if base.endswith("/ws"):
        base = base[: -len("/ws")]
    if base.startswith("wss://"):
        return "https://" + base[len("wss://") :]
    if base.startswith("ws://"):
        return "http://" + base[len("ws://") :]
    return base


def _error_delta(payload: dict) -> str:
    """Render an ``error`` event's payload as the chat text the user sees.

    ``detail`` holds the underlying exception; without it ``turn_failed
    (internal)`` tells the user nothing about what broke. Trimmed to its first
    line, matching what the TUI renders (ui-tui/src/app/chatStream.ts).
    """
    head = f"[gateway error] {payload.get('message', '')} ({payload.get('reason', '')})"
    detail = (payload.get("detail") or "").split("\n")[0][:_ERROR_DETAIL_MAX_CHARS]
    return f"{head}: {detail}" if detail else head


def _rpc_error_message(method: str, error: object) -> str:
    """Render a JSON-RPC error frame as text safe to show a user directly.

    A handler that raises ``RpcError`` puts its whole point -- the readable
    validation message -- in ``data.detail``; without this, the caller only
    sees that dict's ``repr()`` wrapped in ``gateway rpc ... error: ...``, and
    the message the handler wrote is buried inside it. Anything else (a raw
    ``internal_error`` with a traceback tail, or a malformed frame) has no
    clean text to pull out, so it keeps the original verbose form.
    """
    if isinstance(error, dict):
        data = error.get("data")
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, str) and detail:
            return detail
    return f"gateway rpc {method} error: {error}"


class GatewayClient:
    """One shared WS connection to the gateway web channel, multiplexed."""

    _instance: "GatewayClient | None" = None
    _instance_lock = asyncio.Lock()

    def __init__(self, url: str = GATEWAY_WS_URL, token: str | None = GATEWAY_WS_TOKEN) -> None:
        self._url = url
        self._token = token
        self._session: "aiohttp.ClientSession | None" = None
        self._ws = None
        self._read_task: asyncio.Task | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        # subscription_id -> queue of event dicts; session_key -> subscription_id
        self._queues: dict[str, asyncio.Queue] = {}
        self._subs: dict[str, str] = {}
        self._connect_lock = asyncio.Lock()

    @classmethod
    async def shared(cls) -> "GatewayClient":
        async with cls._instance_lock:
            if cls._instance is None:
                cls._instance = GatewayClient()
            await cls._instance.ensure_connected()
            return cls._instance

    async def ensure_connected(self) -> None:
        if self._ws is not None and not self._ws.closed:
            return
        async with self._connect_lock:
            if self._ws is not None and not self._ws.closed:
                return
            if aiohttp is None:
                raise RuntimeError("aiohttp is required for the gateway WS client")
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(self._url, heartbeat=30)
            if self._token:
                await self._ws.send_str(self._token)
            self._pending.clear()
            self._subs.clear()  # re-subscribe on demand after a (re)connect
            self._read_task = asyncio.create_task(self._read_loop())
            await self.call("system.hello", {"client_version": _CLIENT_VERSION})

    async def _read_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except Exception:
                    continue
                if isinstance(frame, dict) and "id" in frame and frame.get("method") is None:
                    fut = self._pending.pop(frame["id"], None)
                    if fut is not None and not fut.done():
                        fut.set_result(frame)
                elif isinstance(frame, dict) and frame.get("method") == "event":
                    params = frame.get("params", {})
                    sub_id = params.get("subscription_id")
                    q = self._queues.get(sub_id)
                    if q is not None:
                        q.put_nowait(params.get("event") or {})
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            # Fail any in-flight RPCs so callers don't hang on a dropped socket.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("gateway ws closed"))
            self._pending.clear()
            # Wake any reply_stream blocked on a session queue so it ends its turn
            # promptly (with an error) instead of waiting out the read timeout.
            for q in self._queues.values():
                q.put_nowait({"type": "__disconnected__", "payload": {}})

    async def call(self, method: str, params: dict, *, timeout: float = 30) -> dict:
        # Reachable after close() nulls the socket: without this the caller gets
        # an AttributeError on None.send_str instead of the actual cause.
        if self._ws is None:
            raise RuntimeError(f"gateway ws is not connected; cannot send {method!r}")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._ws.send_str(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        try:
            frame = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            # The reader loop already pops on delivery; this is only reached
            # when wait_for raises, where nothing would otherwise remove the
            # future and every timed-out call would leak one forever.
            self._pending.pop(rid, None)
        if "error" in frame:
            raise RuntimeError(_rpc_error_message(method, frame["error"]))
        return frame.get("result", {})

    async def subscribe(self, session_key: str) -> asyncio.Queue:
        """Return the per-session event queue, subscribing once per session."""
        sub_id = self._subs.get(session_key)
        if sub_id is not None and sub_id in self._queues:
            return self._queues[sub_id]
        result = await self.call("turn.subscribe", {"session_key": session_key})
        sub_id = result["subscription_id"]
        q: asyncio.Queue = asyncio.Queue()
        self._subs[session_key] = sub_id
        self._queues[sub_id] = q
        return q

    async def send_turn(self, session_key: str, content: str) -> str:
        result = await self.call("turn.send", {"session_key": session_key, "content": content})
        return result.get("turn_id", "")

    async def aclose(self) -> None:
        """Close the read loop, WS, and HTTP session. Call on service shutdown."""
        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._ws = None
        self._session = None

    @classmethod
    async def aclose_shared(cls) -> None:
        async with cls._instance_lock:
            if cls._instance is not None:
                await cls._instance.aclose()
                cls._instance = None


def _extract_text(inputs) -> str:
    if inputs is None:
        return ""
    msgs = inputs if isinstance(inputs, list) else [inputs]
    parts = []
    for m in msgs:
        getter = getattr(m, "get_text_content", None)
        if callable(getter):
            t = getter()
            if t:
                parts.append(t)
    return "\n".join(parts).strip()


def _drop_stale_turn_events(queue: "asyncio.Queue") -> None:
    """Discard turn-stream leftovers from a previous turn, keeping custom events.

    ``GatewayClient`` caches one queue per session, so whenever a turn stops
    reading before its stream ended -- the idle clock firing, an aborted SSE --
    the tail stays queued. Replayed into the next turn it renders the previous
    answer and then ends that turn early on the stale ``message.complete``.
    Anything queued before this turn is submitted belongs to an earlier turn, so
    it is dropped; ``custom`` events (DAG progress, sub-agent instances) are
    out-of-band by design and not tied to a turn, so they are put back in order.
    """
    held: list[dict] = []
    while True:
        try:
            event = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if event.get("type") == "custom":
            held.append(event)
    for event in held:
        queue.put_nowait(event)


class RavenGatewayAgent:
    """Duck-typed chat agent that drives a persistent ``raven gateway`` over WS."""

    timeout_seconds = 900.0

    def __init__(self, *, name="Raven", state=None, model=None, middlewares=None, **_ignore):
        self.name = name or "Raven"
        self.state = state or AgentState()
        # `middlewares` used to fall into `_ignore` with everything else, which
        # silently dropped the session's knowledge bases: ChatService resolves
        # them, builds a RAGMiddleware and passes it here, and nothing said that
        # it landed nowhere. See raven_kb_retrieval for why the middleware
        # cannot simply be run as-is over this transport.
        self._retriever = KnowledgeRetriever.from_middlewares(middlewares)

    async def _publish_custom(self, name: str | None, value: dict | None) -> None:
        """Publish a CustomEvent on the message bus (live) so it reaches the
        session's SSE stream — used for out-of-band DAG / instance events."""
        if not name or MESSAGE_BUS is None:
            return
        sid = self.state.session_id
        if not sid:
            return
        try:
            from agentscope.app._service._session_projection import SessionProjection

            await SessionProjection(MESSAGE_BUS).publish(sid, name, value or {})
        except Exception:  # noqa: BLE001 - viz must never break the reply
            pass

    async def reply_stream(self, inputs=None):
        sid = self.state.session_id
        reply_id = uuid.uuid4().hex
        self.state.reply_id = reply_id
        session_key = f"web:{sid}" if sid else "web:default"

        yield ReplyStartEvent(session_id=sid, reply_id=reply_id, name=self.name)

        # Block lifecycle: at most one thinking/text block open at a time; a tool
        # call is a discrete segment that closes any open reasoning/text block.
        open_kind: str | None = None  # "think" | "text" | None
        open_id: str | None = None
        cur_tool: str | None = None
        cur_tool_blocking = False
        cur_tool_display: str | None = None

        def _close_block():
            nonlocal open_kind, open_id
            if open_kind == "think":
                ev = ThinkingBlockEndEvent(reply_id=reply_id, block_id=open_id)
            elif open_kind == "text":
                ev = TextBlockEndEvent(reply_id=reply_id, block_id=open_id)
            else:
                ev = None
            open_kind, open_id = None, None
            return ev

        try:
            client = await GatewayClient.shared()
            queue = await client.subscribe(session_key)
            _drop_stale_turn_events(queue)
            turn = _extract_text(inputs)
            if self._retriever is not None:
                turn = await self._retriever.augment(turn)
            await client.send_turn(session_key, turn)
        except Exception as exc:  # connection / submit failure
            tb = uuid.uuid4().hex
            yield TextBlockStartEvent(reply_id=reply_id, block_id=tb)
            yield TextBlockDeltaEvent(reply_id=reply_id, block_id=tb, delta=f"连接 gateway 失败: {exc}")
            yield TextBlockEndEvent(reply_id=reply_id, block_id=tb)
            yield ReplyEndEvent(session_id=sid, reply_id=reply_id, finished_reason=ReplyEndReason.COMPLETED)
            return

        while True:
            # No clock while a blocking tool is in flight. Those tools (spawn,
            # run_subagent_dag, deep_research) run a sub-agent, which the runtime
            # deliberately gives no automatic deadline -- a manual stop is the only
            # end -- and which emits nothing between its tool.start and
            # tool.complete. Clocking that gap ends the turn here while the run
            # continues upstream, so its tool result, the DAG's terminal node
            # events and the final answer arrive on a queue nobody is reading and
            # the UI is left frozen on the last status it saw.
            timeout = None if cur_tool_blocking else self.timeout_seconds
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            etype = ev.get("type")
            payload = ev.get("payload", {}) or {}

            if etype == "thinking.delta":
                if cur_tool is not None:
                    continue  # ignore stray reasoning while a tool is mid-flight
                if open_kind != "think":
                    end = _close_block()
                    if end is not None:
                        yield end
                    open_kind, open_id = "think", uuid.uuid4().hex
                    yield ThinkingBlockStartEvent(reply_id=reply_id, block_id=open_id)
                text = payload.get("text", "")
                if text:
                    yield ThinkingBlockDeltaEvent(reply_id=reply_id, block_id=open_id, delta=text)

            elif etype == "token.delta":
                if open_kind != "text":
                    end = _close_block()
                    if end is not None:
                        yield end
                    open_kind, open_id = "text", uuid.uuid4().hex
                    yield TextBlockStartEvent(reply_id=reply_id, block_id=open_id)
                text = payload.get("text", "")
                if text:
                    yield TextBlockDeltaEvent(reply_id=reply_id, block_id=open_id, delta=text)

            elif etype == "tool.start":
                end = _close_block()
                if end is not None:
                    yield end
                tcid = payload.get("tool_call_id") or uuid.uuid4().hex
                tname = payload.get("name") or "tool"
                cur_tool = tcid
                cur_tool_blocking = bool(payload.get("blocking"))
                # The tool-authored call label rides out on the result event's
                # metadata: ToolCallBlock comes from the published
                # @agentscope-ai/agentscope package and has no field for it,
                # while ToolResultBlock.metadata is already plumbed end to end.
                cur_tool_display = str(payload.get("display") or "") or None
                yield ToolCallStartEvent(reply_id=reply_id, tool_call_id=tcid, tool_call_name=tname)
                yield ToolCallDeltaEvent(
                    reply_id=reply_id,
                    tool_call_id=tcid,
                    delta=json.dumps(payload.get("arguments") or {}, ensure_ascii=False),
                )
                yield ToolCallEndEvent(reply_id=reply_id, tool_call_id=tcid)
                yield ToolResultStartEvent(reply_id=reply_id, tool_call_id=tcid, tool_call_name=tname)

            elif etype == "tool.complete":
                tcid = payload.get("tool_call_id") or cur_tool or uuid.uuid4().hex
                preview = payload.get("result_preview") or ""
                if preview:
                    yield ToolResultTextDeltaEvent(reply_id=reply_id, tool_call_id=tcid, delta=preview)
                yield ToolResultEndEvent(
                    reply_id=reply_id,
                    tool_call_id=tcid,
                    state=ToolResultState.SUCCESS,
                    metadata={
                        "truncated": bool(payload.get("truncated")),
                        **({"display": cur_tool_display} if cur_tool_display else {}),
                        **(payload.get("metadata") or {}),
                    },
                )
                cur_tool = None
                cur_tool_blocking = False
                cur_tool_display = None

            elif etype == "custom":
                # Out-of-band CustomEvent (DAG progress / subagent instances):
                # publish on the message bus so it reaches the session's SSE and
                # the web UI's DAG graph / instance panel, without entering the
                # reply's content blocks.
                await self._publish_custom(ev.get("name"), payload)

            elif etype == "message.complete":
                break

            elif etype == "__disconnected__":
                # The shared WS dropped mid-turn (gateway restart / network).
                end = _close_block()
                if end is not None:
                    yield end
                tb = uuid.uuid4().hex
                yield TextBlockStartEvent(reply_id=reply_id, block_id=tb)
                yield TextBlockDeltaEvent(reply_id=reply_id, block_id=tb, delta="[gateway 连接中断,本轮结束]")
                yield TextBlockEndEvent(reply_id=reply_id, block_id=tb)
                break

            elif etype == "error":
                end = _close_block()
                if end is not None:
                    yield end
                tb = uuid.uuid4().hex
                yield TextBlockStartEvent(reply_id=reply_id, block_id=tb)
                yield TextBlockDeltaEvent(reply_id=reply_id, block_id=tb, delta=_error_delta(payload))
                yield TextBlockEndEvent(reply_id=reply_id, block_id=tb)
                break

            # message.start and any unknown types: ignore.

        end = _close_block()
        if end is not None:
            yield end
        yield ReplyEndEvent(session_id=sid, reply_id=reply_id, finished_reason=ReplyEndReason.COMPLETED)
