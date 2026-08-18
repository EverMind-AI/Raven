"""Unit tests for the gateway-event -> AgentScope translation helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import raven_gateway_agent as rga
from raven_gateway_agent import RavenGatewayAgent, _error_delta


def test_error_delta_includes_the_payload_detail():
    """`detail` carries the underlying exception; dropping it leaves the user with
    a bare `turn_failed (internal)` and no way to tell what broke."""
    delta = _error_delta(
        {
            "code": -32099,
            "message": "turn_failed",
            "reason": "internal",
            "detail": "APIError: OpenrouterException - Server disconnected",
        }
    )

    assert "turn_failed" in delta
    assert "internal" in delta
    assert "APIError: OpenrouterException - Server disconnected" in delta


def test_error_delta_without_detail_keeps_the_short_form():
    delta = _error_delta({"code": -32099, "message": "turn_failed", "reason": "internal"})

    assert delta == "[gateway error] turn_failed (internal)"


def test_error_delta_uses_only_the_first_line_of_a_traceback():
    """A multi-line detail would blow up the chat bubble; the first line names the
    error, matching what the TUI renders."""
    delta = _error_delta(
        {
            "message": "turn_failed",
            "reason": "internal",
            "detail": "ValueError: bad model\n  File 'x.py', line 1\n    raise ValueError",
        }
    )

    assert delta == "[gateway error] turn_failed (internal): ValueError: bad model"


def test_error_delta_truncates_a_very_long_detail():
    delta = _error_delta({"message": "turn_failed", "reason": "internal", "detail": "x" * 500})

    assert len(delta) < 260
    assert delta.endswith("x")


def test_error_delta_tolerates_an_empty_payload():
    assert _error_delta({}) == "[gateway error]  ()"


# ---------------------------------------------------------------------------
# reply_stream's idle clock: it must not fire while a blocking tool is running.
#
# A sub-agent run (run_subagent_dag, deep_research, spawn) emits nothing between
# its tool.start and tool.complete, and routinely runs longer than the clock. A
# clock that fires there ends the turn in the UI while the run is still going:
# the tool result, the DAG's terminal node events and the final answer all land
# on a queue nobody is reading, so the graph freezes on its last-seen status.
# ---------------------------------------------------------------------------

_IDLE = 0.05


class _FakeGatewayClient:
    """Hands out one pre-seeded queue and pumps a scripted feed on send_turn."""

    def __init__(self, queue: asyncio.Queue, feed) -> None:
        self._queue = queue
        self._feed = feed
        self.task: asyncio.Task | None = None
        self.sent: list[str] = []

    async def subscribe(self, session_key: str) -> asyncio.Queue:
        return self._queue

    async def send_turn(self, session_key: str, content: str) -> str:
        self.sent.append(content)
        self.task = asyncio.get_running_loop().create_task(self._pump())
        return "turn-1"

    async def _pump(self) -> None:
        for delay, event in self._feed:
            if delay:
                await asyncio.sleep(delay)
            self._queue.put_nowait(event)


def _drive(feed, *, seed=(), idle: float = _IDLE, retriever=None, sent=None):
    """Run one reply_stream turn against a scripted feed.

    ``seed`` pre-fills the queue as if a previous turn had left events behind.
    ``retriever`` replaces the agent's, and ``sent`` is a list the turn text
    handed to the gateway is appended to.
    Returns ``(event_class_names, concatenated_deltas, published_customs)``.
    """

    async def main():
        queue: asyncio.Queue = asyncio.Queue()
        for event in seed:
            queue.put_nowait(event)
        client = _FakeGatewayClient(queue, feed)

        async def _shared():
            return client

        original = rga.GatewayClient.shared
        rga.GatewayClient.shared = _shared
        agent = RavenGatewayAgent(state=SimpleNamespace(session_id="s1"))
        if retriever is not None:
            agent._retriever = retriever
        agent.timeout_seconds = idle
        customs: list[tuple] = []

        async def _publish_custom(name, value):
            customs.append((name, value))

        agent._publish_custom = _publish_custom
        try:
            out = [event async for event in agent.reply_stream(None)]
        finally:
            rga.GatewayClient.shared = original
            if sent is not None:
                sent.extend(client.sent)
            if client.task is not None and not client.task.done():
                client.task.cancel()
        names = [type(event).__name__ for event in out]
        text = "".join(getattr(event, "delta", "") or "" for event in out)
        return names, text, customs

    return asyncio.run(main())


def _start(name: str, *, blocking: bool) -> dict:
    return {
        "type": "tool.start",
        "payload": {"tool_call_id": "t1", "name": name, "blocking": blocking},
    }


_COMPLETE = {"type": "tool.complete", "payload": {"tool_call_id": "t1", "result_preview": "3 completed"}}
_DONE = {"type": "message.complete", "payload": {}}


def test_a_blocking_tool_outlives_the_idle_clock():
    names, _, _ = _drive([(0, _start("run_subagent_dag", blocking=True)), (_IDLE * 6, _COMPLETE), (0, _DONE)])

    assert "ToolResultEndEvent" in names, names
    assert names[-1] == "ReplyEndEvent"


def test_a_non_blocking_tool_still_ends_the_turn_on_the_idle_clock():
    """The clock stays as a backstop for a genuinely wedged non-blocking tool."""
    names, _, _ = _drive([(0, _start("grep", blocking=False)), (_IDLE * 6, _COMPLETE)])

    assert "ToolResultEndEvent" not in names, names
    assert names[-1] == "ReplyEndEvent"


def test_a_tool_start_without_the_flag_is_treated_as_non_blocking():
    """An older gateway omits the key; the pre-existing backstop must still hold."""
    names, _, _ = _drive(
        [
            (0, {"type": "tool.start", "payload": {"tool_call_id": "t1", "name": "grep"}}),
            (_IDLE * 6, _COMPLETE),
        ]
    )

    assert "ToolResultEndEvent" not in names, names


def test_a_stale_turn_tail_does_not_bleed_into_the_next_turn():
    """Turn-stream leftovers are dropped, out-of-band custom events are kept.

    Whenever a turn ends without draining its queue (the idle clock firing, an
    aborted stream), the tail sits there. Replayed into the next turn it renders
    the previous answer and then ends that turn early on the stale
    message.complete. Custom events are out-of-band by design, so they survive.
    """
    stale = [
        {"type": "custom", "name": "dag_run_completed", "payload": {"run_id": "r1"}},
        {"type": "token.delta", "payload": {"text": "PREVIOUS ANSWER"}},
        _DONE,
    ]
    names, text, customs = _drive(
        [(0, {"type": "token.delta", "payload": {"text": "fresh"}}), (0, _DONE)],
        seed=stale,
    )

    assert "PREVIOUS ANSWER" not in text
    assert "fresh" in text
    assert customs == [("dag_run_completed", {"run_id": "r1"})]
    assert names[-1] == "ReplyEndEvent"


# --------------------------------------------------------------- knowledge bases


def test_the_sessions_knowledge_bases_reach_the_agent():
    """The defect this wiring fixes was silent: ChatService resolved the session's
    knowledge bases, built a RAGMiddleware and passed it in, and the signature
    absorbed it with everything else it did not recognise. Documents were indexed
    and searchable and no turn could see any of it, with nothing reporting a
    problem. Asserting the retriever gets built is what makes that loud."""
    from agentscope.middleware import RAGMiddleware

    middleware = RAGMiddleware(
        knowledge_bases=[SimpleNamespace(name="kb")],
        parameters=RAGMiddleware.Parameters(),
    )

    agent = RavenGatewayAgent(state=SimpleNamespace(session_id="s1"), middlewares=[middleware])

    assert agent._retriever is not None, "the middleware was accepted and then dropped"


def test_no_knowledge_base_means_no_retriever():
    """The common case has to stay free of retrieval work."""
    agent = RavenGatewayAgent(state=SimpleNamespace(session_id="s1"), middlewares=[])

    assert agent._retriever is None


def test_the_retrieved_context_reaches_the_gateway():
    """Building the retriever is half of it; the turn actually sent has to be the
    augmented one. Both halves failed independently in a mutation pass."""

    class _Retriever:
        async def augment(self, text: str) -> str:
            return f"[1] (source: kb > s)\ncontext\n\n{text}"

    sent: list[str] = []
    _drive([(0, _DONE)], retriever=_Retriever(), sent=sent)

    assert sent, "the turn never reached the gateway"
    assert sent[0].startswith("[1] (source: kb > s)"), sent[0]
# --- routing a shared subscription between the main reply and a direct chat ---


def _client_with_subscription(session_key: str = "web:s1", sub_id: str = "sub-1"):
    """A GatewayClient with one session subscribed, no socket involved."""
    from raven_gateway_agent import GatewayClient

    client = GatewayClient.__new__(GatewayClient)
    client._queues = {sub_id: asyncio.Queue()}
    client._subs = {session_key: sub_id}
    client._sub_sessions = {sub_id: session_key}
    client._direct_queues = {}
    return client, sub_id


def _tagged(text: str, agent: str = "Coder", handle: str = "refactor") -> dict:
    return {
        "type": "token.delta",
        "payload": {"text": text, "target": {"agent": agent, "handle": handle}},
    }


def test_a_direct_turns_events_never_reach_the_main_reply():
    """The defect this routing exists to prevent.

    The main reply and a direct turn share one subscription. Fed into the reply
    loop, a sub-agent's deltas render as the main agent's answer and its
    ``message.complete`` ends the main turn early -- so the user's own reply
    stops mid-sentence because a different conversation finished.
    """
    client, sub_id = _client_with_subscription()
    watched = client.watch_direct("web:s1", "Coder", "refactor")

    client._route(sub_id, _tagged("hello"))
    client._route(sub_id, {"type": "token.delta", "payload": {"text": "main"}})

    assert watched.get_nowait()["payload"]["text"] == "hello"
    assert client._queues[sub_id].get_nowait()["payload"]["text"] == "main"
    assert watched.empty() and client._queues[sub_id].empty()


def test_two_instances_answering_at_once_do_not_share_a_queue():
    client, sub_id = _client_with_subscription()
    a = client.watch_direct("web:s1", "Coder", "one")
    b = client.watch_direct("web:s1", "Writer", "two")

    client._route(sub_id, _tagged("to a", "Coder", "one"))
    client._route(sub_id, _tagged("to b", "Writer", "two"))

    assert a.get_nowait()["payload"]["text"] == "to a"
    assert b.get_nowait()["payload"]["text"] == "to b"


def test_an_unwatched_instance_is_dropped_rather_than_buffered():
    """Nobody is reading it, and its record is on disk either way.

    Buffering would grow one queue per instance the session ever addressed, for
    a reader that may never come back.
    """
    client, sub_id = _client_with_subscription()
    client._route(sub_id, _tagged("nobody home"))
    assert client._queues[sub_id].empty(), "it must not fall through to the main reply"


def test_two_handles_that_concatenate_alike_do_not_collide():
    """Agent names and handles are both free-form, so the key is length-prefixed."""
    from raven_gateway_agent import GatewayClient

    assert GatewayClient.target_key("s", "a/b", "c") != GatewayClient.target_key("s", "a", "b/c")
