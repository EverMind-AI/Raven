"""The one-shot turn `raven/a2a/runtime.py` runs for an inbound A2A task.

Driven against a real `AgentLoop` with only the LLM and sandbox edges faked.
That is the point of the file rather than an implementation detail: every other
test on this path substitutes `run_turn` itself, so the callables the one-shot
path builds are only ever handed to a double, and a double uses them however
the test author assumed they were used.

The adapter seam in the same module is covered here too, but against doubles
rather than a real ``DefaultRequestHandler``: what the adapter owes its caller
is that each JSON-RPC dict becomes the proto request the SDK declares and
reaches the handler unchanged, which a double can assert and a real handler
would only imply.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest
from a2a.utils.errors import InvalidParamsError
from aiohttp import ClientError, ClientSession

from raven.a2a.card import CARD_PATH
from raven.a2a.runtime import (
    _RequestHandlerAdapter,
    make_run_turn,
    make_run_turn_from_factory,
    serve_standalone,
)
from raven.agent.loop import AgentLoop
from raven.config.schema import A2aConfig
from raven.contracts.llm_provider import LLMResponse

CONFIG = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken", "path": "/a2a"}})


class _ScriptedChatProvider:
    """`_run_one_shot_turn` runs with ``stream=False``, so the turn takes the
    ``chat_with_retry`` path rather than ``chat_stream``."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._i = 0

    async def chat_with_retry(self, **_kwargs: object) -> LLMResponse:
        response = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return response

    def get_default_model(self) -> str:
        return "fake/model"


def _loop_without_edges(workspace: Path, *, reply: str) -> AgentLoop:
    """A real loop with the sandbox and MCP bring-up no-oped, so a text-only
    turn needs no VM and no server."""
    loop = AgentLoop(provider=_ScriptedChatProvider([LLMResponse(content=reply)]), workspace=workspace)

    async def _noop(**_kw: object) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


async def test_a_turn_against_a_real_loop_comes_back_with_its_text(tmp_path: Path) -> None:
    """The whole one-shot path, end to end, with nothing standing in for the turn.

    This is the only test that would have caught the runner callables being
    built to the wrong contract: `raven/spine/runner.py` types them
    asymmetrically -- `Emit` returns an Awaitable, `Drain` returns a list --
    and `turn_path.py` iterates the drain's return value directly, so an
    ``async def drain`` fails the turn with "'coroutine' object is not
    iterable" while every stand-in test stays green.
    """
    text = await make_run_turn(_loop_without_edges(tmp_path, reply="the real answer"))(
        "do the thing", conversation_id="a2a:real-loop", broker=None
    )

    assert text == "the real answer"


async def test_the_one_shot_turn_builds_runner_callables_to_the_spine_contract(tmp_path: Path) -> None:
    """The same defect stated as the contract it breaks, so a failure names the cause.

    The test above reports a TypeError from deep inside the turn path; this one
    says which callable is wrong and how. Both are kept: one proves the real
    turn runs, the other explains it when it stops.
    """
    captured: dict[str, object] = {}

    loop = _loop_without_edges(tmp_path, reply="unused")

    async def capturing_run_turn(_req, emit, drain, **_kwargs: object) -> None:
        captured["emit"], captured["drain"] = emit, drain

    loop.run_turn = capturing_run_turn
    await make_run_turn(loop)("do the thing", conversation_id="a2a:contract", broker=None)

    # Drain = Callable[[], list[TurnRequest]]: called with no arguments, and its
    # return value iterated without awaiting it.
    assert list(captured["drain"]()) == []
    # Emit = Callable[[RunnerEvent], Awaitable[None]]: the asymmetric one.
    assert await captured["emit"](object()) is None


def _free_port() -> int:
    """A port the site can have. Bound and released rather than picked, so two
    tests running side by side cannot choose the same one."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _RecordingExecutor:
    """The half of the SDK handler the adapter reaches past it for."""

    def __init__(self, *, answers: bool) -> None:
        self._answers = answers
        self.answered: list[tuple[str, str]] = []

    def answer(self, task_id: str, text: str) -> bool:
        self.answered.append((task_id, text))
        return self._answers

    def is_running(self, _task_id: str) -> bool:
        return False


class _RecordingHandler:
    """Stands in for ``DefaultRequestHandler``, recording what it was handed.

    A real handler would accept the parsed request and say nothing about it;
    the adapter's whole job is the parse, so the double keeps the request to
    let a test assert on the fields it produced.
    """

    def __init__(self, *, answers: bool = False) -> None:
        self.agent_executor = _RecordingExecutor(answers=answers)
        self.calls: list[tuple[str, Any]] = []

    async def on_message_send(self, request: Any, _context: Any) -> str:
        self.calls.append(("on_message_send", request))
        return "sent"

    async def on_get_task(self, request: Any, _context: Any) -> str:
        self.calls.append(("on_get_task", request))
        return "got"

    async def on_list_tasks(self, request: Any, _context: Any) -> str:
        self.calls.append(("on_list_tasks", request))
        return "listed"

    async def on_cancel_task(self, request: Any, _context: Any) -> str:
        self.calls.append(("on_cancel_task", request))
        return "cancelled"

    async def on_subscribe_to_task(self, request: Any, _context: Any):
        self.calls.append(("on_subscribe_to_task", request))
        yield "subscribed"


async def test_a_params_dict_the_proto_rejects_is_refused_as_invalid_params() -> None:
    """``ParseDict`` raises ``ParseError``, a name no JSON-RPC caller knows.

    The adapter is the only place that sees both vocabularies, so it is the
    only place that can restate the failure in the protocol's own word.
    """
    adapter = _RequestHandlerAdapter(_RecordingHandler())

    with pytest.raises(InvalidParamsError):
        await adapter.on_get_task({"id": ["not", "a", "string"]}, None)


async def test_each_task_method_hands_the_sdk_the_request_it_parsed() -> None:
    """Delegation is not the claim; the parse is.

    Asserting only that the handler was called would pass just as well if the
    adapter forwarded the raw dict, which is the one thing a real
    ``DefaultRequestHandler`` rejects. The fields are read back instead.
    """
    handler = _RecordingHandler()
    adapter = _RequestHandlerAdapter(handler)

    assert await adapter.on_get_task({"id": "t-get"}, None) == "got"
    assert await adapter.on_list_tasks({"pageSize": 7}, None) == "listed"
    assert await adapter.on_cancel_task({"id": "t-cancel"}, None) == "cancelled"
    assert [event async for event in adapter.on_subscribe_to_task({"id": "t-sub"}, None)] == ["subscribed"]

    parsed = dict(handler.calls)
    assert [name for name, _ in handler.calls] == [
        "on_get_task",
        "on_list_tasks",
        "on_cancel_task",
        "on_subscribe_to_task",
    ]
    assert parsed["on_get_task"].id == "t-get"
    assert parsed["on_list_tasks"].page_size == 7
    assert parsed["on_cancel_task"].id == "t-cancel"
    assert parsed["on_subscribe_to_task"].id == "t-sub"


async def test_a_send_naming_a_parked_task_answers_it_and_reports_that_task() -> None:
    """A resume must not become a second turn under one task id.

    The message never reaches ``on_message_send``: the parked turn takes the
    text and the caller is told that task's current status instead.
    """
    handler = _RecordingHandler(answers=True)
    adapter = _RequestHandlerAdapter(handler)

    result = await adapter.on_message_send({"message": {"taskId": "t-parked", "parts": [{"text": "the answer"}]}}, None)

    assert result == "got"
    assert handler.agent_executor.answered == [("t-parked", "the answer")]
    assert [name for name, _ in handler.calls] == ["on_get_task"]


async def test_a_stream_naming_a_parked_task_answers_it_and_resubscribes() -> None:
    """The streaming half of the same rule: the caller keeps its event stream,
    which resuming through ``on_message_send_stream`` would have restarted."""
    handler = _RecordingHandler(answers=True)
    adapter = _RequestHandlerAdapter(handler)

    events = [
        event
        async for event in adapter.on_message_send_stream(
            {"message": {"taskId": "t-parked", "parts": [{"text": "the answer"}]}}, None
        )
    ]

    assert events == ["subscribed"]
    assert handler.agent_executor.answered == [("t-parked", "the answer")]
    assert [name for name, _ in handler.calls] == ["on_subscribe_to_task"]


async def _card_once_up(http: ClientSession, url: str) -> dict[str, Any]:
    """The site starts on its own task, so the first GET can beat the bind.
    Polled rather than slept on: a fixed wait is either flaky or slow."""
    for _ in range(200):
        try:
            async with http.get(url) as response:
                response.raise_for_status()
                return await response.json()
        except (ClientError, OSError):
            await asyncio.sleep(0.02)
    raise AssertionError(f"{url} never answered")


async def test_the_standalone_site_serves_its_card_and_frees_the_port_when_cancelled() -> None:
    """Cancellation is how this coroutine ends -- it waits on an Event nothing
    sets -- so the cleanup that returns the port has to survive being cancelled
    rather than run on a normal return."""
    port = _free_port()

    async def run_turn(_prompt: str, *, conversation_id: str, broker: Any) -> str:
        raise AssertionError("the card route runs no turn")

    served = asyncio.create_task(serve_standalone(CONFIG, host="127.0.0.1", port=port, run_turn=run_turn))
    try:
        async with ClientSession() as http:
            card = await _card_once_up(http, f"http://127.0.0.1:{port}{CARD_PATH}")
        # The card names the origin the request actually arrived on, so this
        # also states that the site answering is the one this test started.
        assert card["supportedInterfaces"][0]["url"] == f"http://127.0.0.1:{port}{CONFIG.server.path}"
    finally:
        served.cancel()
        with pytest.raises(asyncio.CancelledError):
            await served

    with pytest.raises((ClientError, OSError)):
        async with ClientSession() as http, http.get(f"http://127.0.0.1:{port}{CARD_PATH}"):
            pass


async def test_a_factory_that_raises_is_reported_as_a_loop_that_is_not_ready() -> None:
    """A boot race is not this call's failure to explain. It degrades to the
    same "not ready yet" a factory with no loop yet produces."""

    def factory() -> Any:
        raise RuntimeError("still booting")

    with pytest.raises(RuntimeError, match="agent_loop is not available yet"):
        await make_run_turn_from_factory(factory)("do the thing", conversation_id="a2a:raised", broker=None)


async def test_a_factory_with_no_loop_yet_is_reported_the_same_way() -> None:
    """The quiet half of the same guard: no exception, just nothing to run on."""
    with pytest.raises(RuntimeError, match="agent_loop is not available yet"):
        await make_run_turn_from_factory(lambda: None)("do the thing", conversation_id="a2a:none", broker=None)


async def test_the_factory_path_runs_the_turn_but_drops_the_callers_broker(tmp_path: Path) -> None:
    """The gateway-mounted face shares the gateway's own loop, whose `ask_user`
    already answers to the person at the gateway. Installing the A2A caller's
    broker on top would redirect that person's questions to the caller."""
    loop = _loop_without_edges(tmp_path, reply="the factory answer")
    installed: list[Any] = []
    loop.tools.get("ask_user").set_broker = installed.append

    text = await make_run_turn_from_factory(lambda: loop)(
        "do the thing", conversation_id="a2a:factory", broker=object()
    )

    assert text == "the factory answer"
    assert installed == []


async def test_the_standalone_path_installs_the_callers_broker(tmp_path: Path) -> None:
    """The mirror of the rule above. A standalone host owns its loop outright,
    so a question raised mid-turn has nowhere to go but back to the caller."""
    loop = _loop_without_edges(tmp_path, reply="asked and answered")
    installed: list[Any] = []
    loop.tools.get("ask_user").set_broker = installed.append
    broker = object()

    await make_run_turn(loop)("do the thing", conversation_id="a2a:broker", broker=broker)

    assert installed == [broker]
