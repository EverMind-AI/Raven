"""Raven's A2A server over a real socket, driven by hand-built requests.

The handler under test is the real ``DefaultRequestHandler`` assembled by
``raven.a2a.runtime.build_request_handler``, so a pass here is proof about the
SDK integration on the *server* side.

It is not proof of conformance, and the difference matters: the requests here
are written by hand and the frames read back with the same assumptions that
wrote them, so this file agrees with whatever raven happens to encode. Three
wire-level disagreements passed through it -- a missing required field, a
missing response envelope, and a reply read from the wrong arm of a oneof --
because the streaming test counts ``data:`` frames without parsing any of them.
``tests/test_a2a_interop.py`` is the one that points raven's own client at
raven's own server, and it lives in the default pytest scope because
``norecursedirs`` keeps this directory out of it.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from raven.a2a.card import CARD_PATH
from raven.a2a.routes_aiohttp import add_a2a_routes
from raven.a2a.runtime import build_request_handler, make_run_turn
from raven.agent.tools.ask_user import AskUserTool
from raven.config.schema import A2aConfig
from raven.contracts.tool import ToolResult

CONFIG = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken", "path": "/a2a"}})


@pytest.fixture
async def server() -> AsyncIterator[TestServer]:
    """`pytest-aiohttp` is absent; aiohttp's own TestServer is what this repo uses."""

    async def run_turn(prompt, *, conversation_id, broker):
        return f"echo: {prompt}"

    app = web.Application()
    add_a2a_routes(app, CONFIG, build_request_handler(CONFIG, run_turn))
    srv = TestServer(app)
    await srv.start_server()
    try:
        yield srv
    finally:
        await srv.close()


async def test_the_card_round_trips_through_a_plain_fetch(server):
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.get(str(server.make_url(CARD_PATH)))
    assert resp.status_code == 200
    card = resp.json()
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["capabilities"]["streaming"] is True


async def test_send_message_returns_the_turn_answer(server):
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            str(server.make_url("/a2a")),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {
                    "message": {
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "messageId": "m1",
                    }
                },
            },
            headers={"Authorization": "Bearer t0ken", "A2A-Version": "1.0"},
        )
    body = resp.json()
    assert "error" not in body
    assert "echo: hello" in str(body["result"])


async def test_a_header_less_request_is_refused_over_the_wire(server):
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            str(server.make_url("/a2a")),
            json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}},
            headers={"Authorization": "Bearer t0ken"},
        )
    assert resp.json()["error"]["message"] == "VersionNotSupportedError"


async def test_streaming_delivers_more_than_one_event(server):
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as http:
        async with http.stream(
            "POST",
            str(server.make_url("/a2a")),
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "SendStreamingMessage",
                "params": {
                    "message": {
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "messageId": "m2",
                    }
                },
            },
            headers={"Authorization": "Bearer t0ken", "A2A-Version": "1.0"},
        ) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            lines = [line async for line in resp.aiter_lines() if line.startswith("data:")]
    assert len(lines) >= 2


class _StandInLoop:
    """Minimal stand-in for `AgentLoop`: exposes `.tools` and `.run_turn` the way
    `runtime._run_one_shot_turn` expects, without building a real agent loop.

    The tool under `self.tools["ask_user"]` is a real `AskUserTool`, reached the
    same way the production turn path reaches it: `_run_one_shot_turn` calls
    `set_broker` on it before `run_turn` runs, and `run_turn` itself here calls
    `set_context`, mirroring the one call `turn_path.py` would make in the real
    loop. Nothing in this test reaches into the executor's or the broker's
    private state to shortcut the ask.
    """

    def __init__(self) -> None:
        self.tools = {"ask_user": AskUserTool()}
        self.prompts: list[str] = []

    async def run_turn(self, req, emit, drain, *, stream, text_sink):
        self.prompts.append(req.text)
        ask_tool = self.tools["ask_user"]
        ask_tool.set_context(req.conversation)
        result = await ask_tool.execute(questions=[{"question": "which one?", "options": ["first", "second"]}])
        text_sink["text"] = result.display_text if isinstance(result, ToolResult) else str(result)


async def test_a_resume_answers_the_parked_turn_instead_of_starting_a_second_one():
    """Pins the session-key gap: a resume must reach the turn that is already parked.

    `runtime.py`'s one-shot turn mints a fresh session on every call, and nothing
    else remembers which task belongs to which turn. `_RequestHandlerAdapter.on_message_send`
    closes that gap by answering a waiting `RavenAgentExecutor` broker (see
    `raven/a2a/asking.py`) before a resume ever reaches that one-shot path. This
    uses its own server, not the `server` fixture above, because it needs a
    `run_turn` that asks a real question instead of echoing.

    The ask itself goes through a real `AskUserTool`, driven by the real
    `make_run_turn` / `_run_one_shot_turn` production chain via `_StandInLoop`
    above -- not a fake that reaches into the executor's broker dict directly.
    """
    import httpx

    loop = _StandInLoop()
    run_turn = make_run_turn(loop)

    app = web.Application()
    handler = build_request_handler(CONFIG, run_turn)
    add_a2a_routes(app, CONFIG, handler)
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:

            async def rpc(method: str, params: dict) -> dict:
                resp = await http.post(
                    str(srv.make_url("/a2a")),
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    headers={"Authorization": "Bearer t0ken", "A2A-Version": "1.0"},
                )
                return resp.json()

            first = await rpc(
                "SendMessage",
                {"message": {"role": "ROLE_USER", "parts": [{"text": "start"}], "messageId": "m1"}},
            )
            assert "error" not in first
            task_id = first["result"]["id"]
            assert first["result"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

            second = await rpc(
                "SendMessage",
                {
                    "message": {
                        "role": "ROLE_USER",
                        "parts": [{"text": "the second one"}],
                        "messageId": "m2",
                        "taskId": task_id,
                    }
                },
            )
            # The consumer that writes "done" into the task store races this
            # response (see runtime.py's on_message_send), so the state here is
            # not asserted -- only that the resume was accepted for the same task.
            assert "error" not in second
            assert second["result"]["id"] == task_id

            final = None
            for _ in range(100):
                polled = await rpc("GetTask", {"id": task_id})
                assert "error" not in polled
                if polled["result"]["status"]["state"] == "TASK_STATE_COMPLETED":
                    final = polled
                    break
                await asyncio.sleep(0.01)
    finally:
        await srv.close()

    assert final is not None, "the parked turn never completed after being answered"
    # "answered: the second one" is AskUserTool's own real display text for a
    # single question (raven/agent/tools/ask_user.py) -- not a fake's stand-in
    # string -- so this pins that the real tool answered the real ask.
    assert final["result"]["status"]["message"]["parts"][0]["text"] == "answered: the second one"
    # Exactly one invocation: the resume answered the live turn instead of
    # starting a second one through runtime.py's one-shot path.
    assert loop.prompts == ["start"]


async def test_ask_user_tool_without_a_broker_reports_its_own_error():
    """Regression: a real `AskUserTool` with no broker wired fails with its own
    literal error text instead of hanging or raising -- the same tool the resume
    test above drives, exercised without any A2A machinery around it at all.
    """
    tool = AskUserTool()
    tool.set_context("some-conversation")

    result = await tool.execute(questions=[{"question": "which one?", "options": ["first", "second"]}])

    assert result == "Error: ask_user not configured (no question broker)"
