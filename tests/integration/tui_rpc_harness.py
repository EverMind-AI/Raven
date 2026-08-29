"""The host's TUI RPC server, in-process, driven the way the TUI drives it.

Tier 1 of ``docs/specs/2026-08-26-tui-screen-harness.md``: no terminal. The
real ``_run_rpc_server_until_done`` serves a real ``AgentLoop`` over a
socketpair, and a test speaks the same JSON-RPC frames ``dist/entry.js`` would
-- ``system.hello``, ``turn.subscribe``, ``turn.send``, the ``subagents.*``
methods -- and reads the same ``event`` notifications back. What this reaches
that the unit suites cannot is the seam between processes: a steer landing in
a running ACP turn, a record read back after the turn settles, one turn
queued behind another.

The only stub is below the loop: a scripted LLM provider and, where a test
asks for one, the ACP stub agent in ``tests/acp_stub_server.py``.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop.bundles import SubagentWiring, ToolWiring, TurnPolicy
from raven.agent.loop.main import AgentLoop
from raven.contracts.llm_provider import LLMResponse

_TOKEN = "harness"
CLIENT_VERSION = "0.1.0"


class ScriptedProvider:
    """A provider whose every reply is the same short text.

    Direct-chat turns never reach it; a main-agent turn gets one sentence and
    no tool calls, which is all the harness needs to prove a turn ran.
    """

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub"

    async def chat_with_retry(self, *, messages, tools=None, model=None, **_kwargs):
        self.calls += 1
        return LLMResponse(content=self.reply, finish_reason="stop")

    async def chat(self, *args, **kwargs):
        return await self.chat_with_retry(*args, **kwargs)

    async def chat_stream(self, *, messages, tools=None, model=None, **_kwargs):
        from raven.contracts.llm_provider import ChatDelta

        response = await self.chat_with_retry(messages=messages, tools=tools, model=model)
        yield ChatDelta(content=response.content or "")


async def make_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, agents: list | None = None, measure: bool = True
) -> AgentLoop:
    """A real loop whose registry, home and workspace live under ``tmp_path``.

    Repointed rather than mocked, like ``test_direct_chat_smoke``: the tests
    read the records this writes, and a developer's own ``~/.raven`` must never
    be touched by a test run.

    ``measure`` records a capability snapshot for every ACP agent first, the
    way ``/subagents`` -> test does: the agent table reads statefulness from
    that snapshot, and an unmeasured agent cannot be direct-chatted at all.
    """
    from raven.agent.subagent import instances as instances_mod
    from raven.config.schema import ThirdPartyAcpSubagentConfig

    monkeypatch.setattr(instances_mod, "_registry", instances_mod.InstanceRegistry(path=tmp_path / "instances.json"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    if measure:
        from raven.agent.acp_client.capabilities import SnapshotStore, verify_agent

        store = SnapshotStore()
        for cfg in agents or []:
            if isinstance(cfg, ThirdPartyAcpSubagentConfig):
                store.record(await verify_agent(cfg))
    return AgentLoop(
        provider=ScriptedProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        subagents=SubagentWiring(agents=agents),
    )


class RpcError(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(f"{error.get('code')}: {error.get('message')}")
        self.error = error


class TuiRpcHarness:
    """One connection to the host's RPC server, with the handshake done.

    ``call`` is a request; ``events`` holds every ``event`` notification in
    arrival order and ``wait_event`` blocks until one matches.
    """

    def __init__(self, loop: AgentLoop, monkeypatch: pytest.MonkeyPatch) -> None:
        self._loop = loop
        self._monkeypatch = monkeypatch
        self.events: list[dict[str, Any]] = []
        self._replies: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._changed = asyncio.Event()
        self._proc_done = asyncio.Event()

    async def __aenter__(self) -> TuiRpcHarness:
        from raven.cli import tui_commands

        # The server builds its loop from the user's config; the harness hands
        # it the one the test built instead.
        self._monkeypatch.setattr(tui_commands, "_build_agent_loop", lambda **_kw: self._loop)
        server_sock, client_sock = socket.socketpair()
        self._server = asyncio.create_task(
            tui_commands._run_rpc_server_until_done(server_sock, _TOKEN, 10.0, self._proc_done)
        )
        self._reader, self._writer = await asyncio.open_connection(sock=client_sock)
        self._writer.write(f"{_TOKEN}\n".encode())
        await self._writer.drain()
        self._pump = asyncio.create_task(self._read_frames())
        await self.call("system.hello", {"client_version": CLIENT_VERSION})
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._proc_done.set()
        self._pump.cancel()
        self._writer.close()
        try:
            await asyncio.wait_for(self._server, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            self._server.cancel()
        from raven.agent.acp_client.pool import close_pool

        await close_pool()

    async def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._replies[rid] = fut
        frame = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        self._writer.write((json.dumps(frame) + "\n").encode())
        await self._writer.drain()
        reply = await asyncio.wait_for(fut, timeout)
        if "error" in reply:
            raise RpcError(reply["error"])
        return reply.get("result")

    async def wait_event(self, matches: Callable[[dict[str, Any]], bool], *, timeout: float = 30.0) -> dict[str, Any]:
        """The first event satisfying ``matches``, already received or to come."""
        deadline = asyncio.get_running_loop().time() + timeout
        seen = 0
        while True:
            for event in self.events[seen:]:
                if matches(event):
                    return event
            seen = len(self.events)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"no event matched within {timeout}s; saw {[e.get('type') for e in self.events]}")
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except asyncio.TimeoutError:
                continue

    async def poll(
        self, read: Callable[[], Awaitable[Any]], until: Callable[[Any], bool], *, timeout: float = 30.0
    ) -> Any:
        """Repeat a read until its result satisfies ``until``; the last result."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            result = await read()
            if until(result):
                return result
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"condition not met within {timeout}s; last result: {str(result)[:400]}")
            await asyncio.sleep(0.1)

    async def _read_frames(self) -> None:
        while True:
            line = await self._reader.readline()
            if not line:
                return
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if frame.get("method") == "event":
                self.events.append(frame.get("params", {}).get("event", {}))
                self._changed.set()
            elif "id" in frame:
                fut = self._replies.pop(frame["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(frame)


__all__ = ["CLIENT_VERSION", "RpcError", "ScriptedProvider", "TuiRpcHarness", "make_loop"]
