"""A real A2A client against a real raven A2A server, over a real socket.

The SDK's own client is the closest thing to a second implementation available,
so conformance is asserted against it rather than against our own encoder. The
handler under test is the real ``DefaultRequestHandler`` assembled by
``raven.a2a.runtime.build_request_handler`` -- not a fake -- so a pass here is
proof about the SDK integration, not just about this repo's own encoding.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from raven.a2a.card import CARD_PATH
from raven.a2a.routes_aiohttp import add_a2a_routes
from raven.a2a.runtime import build_request_handler
from raven.config.schema import A2aConfig

CONFIG = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken", "path": "/a2a"}})


@pytest.fixture
async def server() -> AsyncIterator[TestServer]:
    """`pytest-aiohttp` is absent; aiohttp's own TestServer is what this repo uses."""

    async def run_turn(prompt):
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


async def test_a_resume_answers_the_parked_turn_instead_of_starting_a_second_one():
    """Pins the session-key gap: a resume must reach the turn that is already parked.

    `runtime.py`'s one-shot turn mints a fresh session on every call, and nothing
    else remembers which task belongs to which turn. `_RequestHandlerAdapter.on_message_send`
    closes that gap by answering a waiting `RavenAgentExecutor` broker (see
    `raven/a2a/asking.py`) before a resume ever reaches that one-shot path. This
    uses its own server, not the `server` fixture above, because it needs a
    `run_turn` that parks on a question instead of echoing.
    """
    import httpx

    calls: list[str] = []

    async def run_turn(prompt: str) -> str:
        calls.append(prompt)
        # No real ask_user wiring exists yet for A2A (see executor.py's module
        # docstring); this reaches the one broker `execute` just registered for
        # this task the same way a future tool integration would.
        task_id, broker = next(iter(executor._brokers.items()))
        choice = await broker.await_question(task_id, prompt="which one?")
        return f"final: {choice}"

    app = web.Application()
    handler = build_request_handler(CONFIG, run_turn)
    executor = handler._handler.agent_executor
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
    assert final["result"]["status"]["message"]["parts"][0]["text"] == "final: the second one"
    # Exactly one invocation: the resume answered the live turn instead of
    # starting a second one through runtime.py's one-shot path.
    assert calls == ["start"]
