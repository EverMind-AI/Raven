"""An adversarial ACP client that drives a real ``raven acp`` subprocess.

The inverse of ``tests/acp_stub_server.py``, and the inversion changes its shape.
That module is *exec'd*, so its fourteen behaviours are selected by an
environment variable; this one is the parent, so its behaviours are the test
functions that use it and what lives here is the driver.

Not a test module (pytest collects ``test_*``): this is the other end of the
executable under test. It speaks real newline-delimited JSON-RPC to a real child
process over real pipes, so what it exercises is launch, framing, request
correlation, concurrency and teardown -- none of which a mocked transport
touches.

The original's malice is kept. It exists to be the client a well-behaved agent
survives and a lucky one does not: string request ids rather than integers (an
agent that indexes its pending map by ``int`` fails), a version string where an
integer belongs, ids that are legal JSON-RPC and awkward (``0``, negative),
notifications for sessions that never existed, and a request pipelined behind a
suspended one. Where the stub server listed its permission options in reverse so
a client picking "the first option" failed rather than passing by luck, this one
sends its frames in orders an agent that assumes sequence will fail on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 60.0
"""How long to wait for one answer.

Generous because the agent builds an entire engine -- config, tools, cron, the
memory backend -- before it answers the first frame, and a machine under load
makes that seconds rather than milliseconds. A test that wants to prove
something is *fast* should assert on elapsed time, not on this.
"""


def raven_binary() -> Path:
    """The console script next to the interpreter running the tests.

    The same derivation ``tui_commands`` uses for its own child, so a test and a
    real launch resolve the same binary rather than whichever one PATH happens to
    hold.
    """
    return Path(sys.executable).with_name("raven.exe" if sys.platform == "win32" else "raven")


class StubClient:
    """One connection to a spawned agent, with a pending map and a frame log.

    Stdout is drained by a background task rather than on demand. An agent is
    free to send ``session/update`` notifications at any time, including while a
    request is outstanding, and a client that only read when it expected a
    response would deadlock the moment the agent filled the pipe buffer -- which
    is precisely what a streaming turn does.
    """

    def __init__(
        self,
        *,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self._argv = argv or [str(raven_binary()), "acp"]
        self._env = env
        self._cwd = str(cwd) if cwd is not None else None
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 0
        self.frames: list[dict[str, Any]] = []
        self.stderr: bytes = b""
        self.malformed: list[bytes] = []
        # Fired for every inbound notification, so a test can wait on the
        # streaming half without polling the frame log.
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # Requests the agent makes of the client. Left unanswered by default:
        # what a test wants to pin is usually what the agent does when nobody
        # replies, which is the failure the protocol has no timeout for.
        self.inbound_requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> "StubClient":
        env = dict(os.environ)
        if self._env:
            env.update(self._env)
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self._cwd,
            # Its own group, so a hung agent can be killed whole. An agent that
            # spawned children of its own -- and this one runs shell commands --
            # leaves them attached to the terminal otherwise.
            start_new_session=True,
        )
        self._reader_task = asyncio.create_task(self._drain_stdout())
        return self

    async def close(self) -> None:
        """Close stdin, let the agent exit, and kill it if it will not.

        Closing stdin rather than signalling: that is how an editor ends an ACP
        session, so it is the path worth exercising. The kill is the backstop, and
        it is a process-group kill because the agent's own children would survive
        a single-process one.
        """
        process = self._process
        if process is None:
            return
        with contextlib.suppress(Exception):
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=15.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._kill_group(process)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=5.0)
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        if process.stderr is not None:
            with contextlib.suppress(Exception):
                self.stderr = await process.stderr.read()
        for future in self._pending.values():
            if not future.done():
                future.cancel()

    @staticmethod
    def _kill_group(process: asyncio.subprocess.Process) -> None:
        if not hasattr(os, "killpg"):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            return
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    # -- sending ----------------------------------------------------------

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """Send a request and wait for the frame that answers it."""
        future = await self.send_request(method, params, request_id=request_id)
        return await asyncio.wait_for(future, timeout=timeout)

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: Any = None,
    ) -> asyncio.Future[dict[str, Any]]:
        """Send a request and return its future without waiting.

        The seam for pipelining: two requests in flight is legal, and an agent
        that handles frames inline instead of concurrently deadlocks on the
        second -- which is the whole point of testing it.
        """
        if request_id is None:
            self._next_id += 1
            # Strings rather than integers by default. Legal per JSON-RPC, and an
            # agent that keyed its pending map by int fails here instead of in
            # front of a user.
            request_id = f"stub-{self._next_id}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[_key(request_id)] = future
        await self.write({"jsonrpc": "2.0", "id": request_id, "method": method, **_params(params)})
        return future

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.write({"jsonrpc": "2.0", "method": method, **_params(params)})

    async def write(self, frame: dict[str, Any]) -> None:
        await self.write_bytes(json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n")

    async def write_bytes(self, payload: bytes) -> None:
        """Put bytes on the wire with no framing help at all.

        The malice seam: an oversized line, a truncated one, invalid UTF-8, two
        frames in one write, a frame split across two writes. All of these are
        things a real client does under load, and each has to produce an answer
        rather than a dead agent.
        """
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("the agent is not running")
        process.stdin.write(payload)
        await process.stdin.drain()

    # -- convenience ------------------------------------------------------

    async def handshake(self, **overrides: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"protocolVersion": 1, "clientCapabilities": {}}
        params.update(overrides)
        response = await self.request("initialize", params)
        return _result(response, "initialize")

    async def new_session(self, cwd: str | Path, **overrides: Any) -> str:
        params: dict[str, Any] = {"cwd": str(cwd), "mcpServers": []}
        params.update(overrides)
        response = await self.request("session/new", params)
        return _result(response, "session/new")["sessionId"]

    async def updates(
        self, session_id: str, *, until: Callable[[dict[str, Any]], bool], timeout: float = DEFAULT_TIMEOUT
    ) -> list[dict[str, Any]]:
        """Collect ``session/update`` payloads for one session until ``until``.

        Filtered by session id because a connection may carry several, and a test
        that collected all of them would pass on another session's stream.
        """
        collected: list[dict[str, Any]] = []

        async def _collect() -> None:
            while True:
                frame = await self.notifications.get()
                if frame.get("method") != "session/update":
                    continue
                params = frame.get("params") or {}
                if params.get("sessionId") != session_id:
                    continue
                update = params.get("update") or {}
                collected.append(update)
                if until(update):
                    return

        await asyncio.wait_for(_collect(), timeout=timeout)
        return collected

    def text_of(self, updates: list[dict[str, Any]], kind: str = "agent_message_chunk") -> str:
        return "".join(u.get("content", {}).get("text", "") for u in updates if u.get("sessionUpdate") == kind)

    # -- receiving --------------------------------------------------------

    async def _drain_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                return
            try:
                frame = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Recorded rather than raised. This is the assertion most tests
                # care about -- an agent whose stdout is not pure protocol -- and
                # it reads better as a collected fact than as an exception from a
                # background task.
                self.malformed.append(line)
                continue
            if not isinstance(frame, dict):
                self.malformed.append(line)
                continue
            self.frames.append(frame)
            if "method" in frame:
                if "id" in frame:
                    self.inbound_requests.put_nowait(frame)
                else:
                    self.notifications.put_nowait(frame)
                continue
            future = self._pending.pop(_key(frame.get("id")), None)
            if future is not None and not future.done():
                future.set_result(frame)


@contextlib.asynccontextmanager
async def stub_client(**kwargs: Any) -> AsyncIterator[StubClient]:
    """A started client, closed on the way out even if the test fails."""
    client = await StubClient(**kwargs).start()
    try:
        yield client
    finally:
        await client.close()


def _params(params: dict[str, Any] | None) -> dict[str, Any]:
    return {} if params is None else {"params": params}


def _key(request_id: Any) -> Any:
    """A hashable, type-stable key for the pending map.

    ``1`` and ``"1"`` are different request ids and must not collide, while an
    unhashable id (a list, which is illegal but sendable) must not crash the
    reader.
    """
    if isinstance(request_id, (str, int, float, bool)) or request_id is None:
        return (type(request_id).__name__, request_id)
    return ("repr", repr(request_id))


def _result(response: dict[str, Any], method: str) -> dict[str, Any]:
    if "error" in response:
        raise AssertionError(f"{method} failed: {response['error']}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise AssertionError(f"{method} answered with no result object: {response}")
    return result


__all__ = ["DEFAULT_TIMEOUT", "StubClient", "raven_binary", "stub_client"]
