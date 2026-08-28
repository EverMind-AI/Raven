"""Host-side unix socket endpoints that front a held MCP connection."""

import asyncio
import contextlib
import json
import os
import stat
import sys
import time
from unittest import mock

import pytest
from loguru import logger

from raven.config.schema import MCPServerConfig
from raven.mcp import endpoint
from raven.mcp.endpoint import McpEndpoints, bridge_command


async def _resolved(executor):
    """An ``executor_provider`` body: resolve to an already-built executor."""
    return executor


_ECHO_SERVER = """
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": {"saw": req.get("method")}}), flush=True)
"""


def test_socket_path_is_a_short_hash_not_a_readable_name():
    # AF_UNIX caps the whole path at 104 bytes, and playbook + node + server
    # names concatenate past that easily.
    p = McpEndpoints().path_for("a-very-long-node-identifier-from-a-playbook", "an-equally-long-server-name")
    assert len(str(p).encode()) < 104
    assert "very-long" not in str(p)


def test_one_instance_keeps_one_path_per_pair():
    endpoints = McpEndpoints()
    assert endpoints.path_for("n", "s") == endpoints.path_for("n", "s")
    assert endpoints.path_for("n", "s") != endpoints.path_for("n", "t")


def test_two_instances_never_share_a_path_for_the_same_pair():
    # Node ids are unique within one run, not across runs, so a path derived
    # from the pair alone made two concurrent runs of the same playbook bind,
    # unlink and reap each other's sockets. Nothing derives these paths -- the
    # creator hands each one over -- so the salt costs nothing.
    assert McpEndpoints().path_for("n", "s") != McpEndpoints().path_for("n", "s")


def test_the_bridge_command_is_this_builds_own_raven(monkeypatch, tmp_path):
    """Not a PATH lookup by name, and the difference is not cosmetic.

    ``mcp bridge`` is a facility of the raven holding the socket. Resolving the
    name found whatever raven came first on the adapter's PATH, and an older
    build answers the relay with "No such command 'mcp'": every bridged server
    for that dispatch died as a closed connection, while the sub-agent still
    answered the turn without them.
    """
    own = tmp_path / "bin" / "raven"
    own.parent.mkdir()
    own.write_text("#!/bin/sh\nexit 0\n")
    own.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(own.parent / "python"))

    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "raven").write_text("#!/bin/sh\nexit 0\n")
    (other / "raven").chmod(0o755)

    assert bridge_command(path=str(other)) == [str(own), "mcp", "bridge"]


def test_the_bridge_command_falls_back_to_the_path_then_to_nothing(monkeypatch, tmp_path):
    """A raven installed where the adapter runs but not beside this
    interpreter is still worth handing over; no raven at all is ``None``."""
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-bin" / "python"))

    on_path = tmp_path / "onpath"
    on_path.mkdir()
    (on_path / "raven").write_text("#!/bin/sh\nexit 0\n")
    (on_path / "raven").chmod(0o755)
    assert bridge_command(path=str(on_path)) == [str(on_path / "raven"), "mcp", "bridge"]

    assert bridge_command(path=str(tmp_path / "empty")) is None


@pytest.mark.asyncio
async def test_an_open_endpoint_relays_to_the_held_upstream(tmp_path):
    cfg = MCPServerConfig(command=sys.executable, args=["-c", _ECHO_SERVER])
    endpoints = McpEndpoints()
    try:
        path = await endpoints.open("node-1", "echo", cfg)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b'{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n')
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=20)
        writer.close()
    finally:
        await endpoints.aclose()

    assert json.loads(line)["result"] == {"saw": "tools/list"}
    assert not path.exists()


@pytest.mark.asyncio
async def test_close_returns_even_while_a_downstream_is_still_connected():
    # Server.wait_closed returns only once every accepted connection has
    # detached, so closing the listener alone blocks for as long as the
    # sub-agent keeps its bridge open -- which is exactly when a node ends.
    # Cancelling the in-flight relays is what makes this return.
    cfg = MCPServerConfig(command=sys.executable, args=["-c", _ECHO_SERVER])
    endpoints = McpEndpoints()
    path = await endpoints.open("held", "echo", cfg)
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    await writer.drain()
    await asyncio.wait_for(reader.readline(), timeout=20)  # the relay is up

    try:
        await asyncio.wait_for(endpoints.close("held"), timeout=15)
    finally:
        writer.close()
    assert not path.exists()


@pytest.mark.asyncio
async def test_a_malformed_frame_is_dropped_without_killing_the_connection(tmp_path):
    cfg = MCPServerConfig(command=sys.executable, args=["-c", _ECHO_SERVER])
    endpoints = McpEndpoints()
    try:
        path = await endpoints.open("node-2", "echo", cfg)
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"this is not json\n")
        writer.write(b'{"jsonrpc":"2.0","id":8,"method":"ping"}\n')
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=20)
        writer.close()
    finally:
        await endpoints.aclose()

    assert json.loads(line)["result"] == {"saw": "ping"}


_TOOLS_SERVER = """
import json, sys

TOOLS = [
    {"name": "search", "description": "", "inputSchema": {}},
    {"name": "delete", "description": "", "inputSchema": {}},
    {"name": "actions/download-logs", "description": "", "inputSchema": {}},
]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    if req.get("method") == "tools/list":
        result = {"tools": TOOLS}
    else:
        result = {"called": (req.get("params") or {}).get("name")}
    print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": result}), flush=True)
"""


async def _round_trip(path, frames):
    """Send frames down one downstream connection and read back one reply each.

    Keyed by id rather than returned in order: a refusal the endpoint answers
    itself can overtake an upstream reply that is still in flight, and JSON-RPC
    correlates by id, not by arrival.
    """
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        for frame in frames:
            writer.write(json.dumps(frame).encode() + b"\n")
        await writer.drain()
        replies = {}
        for _ in frames:
            reply = json.loads(await asyncio.wait_for(reader.readline(), timeout=20))
            replies[reply["id"]] = reply
        return replies
    finally:
        writer.close()


@contextlib.contextmanager
def _warnings():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        yield lines
    finally:
        logger.remove(sink)


# ── Teardown: the relay's pumps are owned by the relay ──────────────


@pytest.mark.asyncio
async def test_close_does_not_wait_out_its_timeout_when_cancellation_lands_mid_open():
    """A reap while an upstream is still being dialled must still be prompt.

    ``close`` cancels the relay, and the cancellation can arrive anywhere --
    including inside the upstream open, before there is an ``up_read`` to end.
    Nothing then closed the downstream writer, so the accepted connection never
    detached and ``wait_closed`` sat out the whole ``_CLOSE_TIMEOUT_S``: measured
    10.00s, with a ``did not settle`` warning, on a plain stdio upstream caught
    mid-spawn.
    """
    opening = asyncio.Event()

    @contextlib.asynccontextmanager
    async def _never_opens(*args, **kwargs):
        opening.set()
        await asyncio.Event().wait()  # the dial that has not finished yet
        yield None, None

    with mock.patch.object(endpoint, "open_mcp_transport", _never_opens):
        endpoints = McpEndpoints()
        path = await endpoints.open("mid-open", "echo", MCPServerConfig(command=sys.executable, args=["-c", ""]))
        reader, writer = await asyncio.open_unix_connection(str(path))
        await asyncio.wait_for(opening.wait(), timeout=10)

        with _warnings() as lines:
            started = time.monotonic()
            await asyncio.wait_for(endpoints.close("mid-open"), timeout=endpoint._CLOSE_TIMEOUT_S + 5)
            elapsed = time.monotonic() - started

    writer.close()
    assert elapsed < 2.0, f"close() took {elapsed:.2f}s"
    assert not [line for line in lines if "did not settle" in line]
    # The downstream was closed rather than merely abandoned, which is what let
    # wait_closed return at all.
    assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    assert not path.exists()


@pytest.mark.asyncio
async def test_close_leaves_no_relay_task_behind():
    """Cancelling the outer relay is not enough on its own.

    ``asyncio.wait`` cancels none of the futures it was waiting on, so a
    cancellation delivered there used to leave both pumps running with nobody
    holding them -- the upstream teardown they were attached to finished
    detached, or not at all.
    """
    cfg = MCPServerConfig(command=sys.executable, args=["-c", _ECHO_SERVER])
    endpoints = McpEndpoints()
    before = asyncio.all_tasks()
    path = await endpoints.open("owned", "echo", cfg)
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    await writer.drain()
    await asyncio.wait_for(reader.readline(), timeout=20)

    await asyncio.wait_for(endpoints.close("owned"), timeout=endpoint._CLOSE_TIMEOUT_S + 5)
    writer.close()

    assert {task for task in asyncio.all_tasks() if task not in before} == set()


# ── Sandbox: a bridged stdio upstream is spawned where a host one is ────


class _FakeBox:
    """An executor that runs the server elsewhere and hands back its streams.

    The shape ``BoxliteExecutor.start_process`` has: the process lives in the
    microVM and the host talks to it over anyio memory streams, which is why the
    sub-agent's socket -- touched only by this process -- is unaffected by where
    the server ends up.
    """

    is_sandboxed = True
    supports_process_spawning = True

    def __init__(self) -> None:
        self.spawned: list[tuple[str, tuple[str, ...]]] = []
        self._pumps: list[asyncio.Task] = []

    async def start_process(self, command, args, env=None):
        import anyio
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCMessage

        self.spawned.append((command, tuple(args)))
        read_send, read_recv = anyio.create_memory_object_stream(16)
        write_send, write_recv = anyio.create_memory_object_stream(16)

        async def _serve() -> None:
            async for outgoing in write_recv:
                request = outgoing.message.root
                reply = JSONRPCMessage.model_validate(
                    {"jsonrpc": "2.0", "id": request.id, "result": {"in_the_box": request.method}}
                )
                await read_send.send(SessionMessage(reply))

        self._pumps.append(asyncio.create_task(_serve()))
        return read_recv, write_send

    async def aclose(self) -> None:
        for pump in self._pumps:
            pump.cancel()
        await asyncio.gather(*self._pumps, return_exceptions=True)


class _NoSpawnBox:
    """A sandbox that confines but cannot host a long-running child."""

    is_sandboxed = True
    supports_process_spawning = False


@pytest.mark.asyncio
async def test_a_bridged_stdio_upstream_is_spawned_through_the_host_executor():
    # The command is unresolvable on purpose: a host spawn would raise instead
    # of quietly running the server outside the sandbox the config asked for.
    cfg = MCPServerConfig(command="raven-mcp-not-on-this-host", args=["--root", "/x"])
    box = _FakeBox()
    endpoints = McpEndpoints(executor_provider=lambda: _resolved(box))
    try:
        path = await endpoints.open("boxed", "files", cfg)
        replies = await _round_trip(path, [{"jsonrpc": "2.0", "id": 3, "method": "tools/list"}])
    finally:
        await endpoints.aclose()
        await box.aclose()

    assert box.spawned == [("raven-mcp-not-on-this-host", ("--root", "/x"))]
    assert replies[3]["result"] == {"in_the_box": "tools/list"}


@pytest.mark.asyncio
async def test_a_sandbox_that_cannot_spawn_fails_the_bridge_instead_of_escaping_it():
    # Same hard failure connect_mcp_server makes: falling back to a host spawn
    # would drop the confinement with nothing to show it had happened.
    cfg = MCPServerConfig(command=sys.executable, args=["-c", _ECHO_SERVER])
    endpoints = McpEndpoints(executor_provider=lambda: _resolved(_NoSpawnBox()))
    try:
        path = await endpoints.open("no-spawn", "echo", cfg)
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        await writer.drain()
        # Either shape of "the connection went away" counts: the host closes the
        # socket once the upstream refuses, and whether the peer then reads a
        # clean EOF or a reset is the kernel's choice -- macOS gives EOF, Linux
        # gives ECONNRESET. What must not happen is a working server.
        try:
            assert await asyncio.wait_for(reader.read(), timeout=20) == b""
        except ConnectionResetError:
            pass
        writer.close()
    finally:
        await endpoints.aclose()


@pytest.mark.asyncio
async def test_the_executor_is_not_resolved_until_a_sub_agent_actually_connects():
    calls: list[int] = []

    async def _provider():
        calls.append(1)
        return None

    endpoints = McpEndpoints(executor_provider=_provider)
    try:
        await endpoints.open("lazy", "echo", MCPServerConfig(command=sys.executable, args=["-c", _ECHO_SERVER]))
    finally:
        await endpoints.aclose()

    assert calls == []


# ── The host's off-switch, across the bridge ────────────────────────


@pytest.mark.asyncio
async def test_a_disabled_tool_is_neither_listed_nor_callable_across_the_bridge():
    # 'mcp_files_actions/download-logs' is the pre-sanitising spelling, so the
    # entry matches through `spellings` and could not have been matched by
    # gluing the wire name to the server name.
    cfg = MCPServerConfig(command=sys.executable, args=["-c", _TOOLS_SERVER])
    endpoints = McpEndpoints(disabled_tools=frozenset({"mcp_files_delete", "mcp_files_actions/download-logs"}))
    try:
        path = await endpoints.open("denied", "files", cfg)
        replies = await _round_trip(
            path,
            [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delete"}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search"}},
            ],
        )
    finally:
        await endpoints.aclose()

    assert [tool["name"] for tool in replies[1]["result"]["tools"]] == ["search"]
    # An error, not an upstream result: the call never reached the server, which
    # would have answered with {"called": "delete"}.
    assert "result" not in replies[2]
    assert replies[2]["error"]["code"] == -32602
    assert "delete" in replies[2]["error"]["message"]
    assert replies[3]["result"] == {"called": "search"}


@pytest.mark.asyncio
async def test_nothing_is_filtered_when_the_host_disabled_nothing():
    cfg = MCPServerConfig(command=sys.executable, args=["-c", _TOOLS_SERVER])
    endpoints = McpEndpoints()
    try:
        path = await endpoints.open("open", "files", cfg)
        replies = await _round_trip(
            path,
            [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delete"}},
            ],
        )
    finally:
        await endpoints.aclose()

    assert [tool["name"] for tool in replies[1]["result"]["tools"]] == ["search", "delete", "actions/download-logs"]
    assert replies[2]["result"] == {"called": "delete"}


@pytest.mark.asyncio
async def test_an_entry_naming_another_servers_tool_does_not_deny_this_one():
    cfg = MCPServerConfig(command=sys.executable, args=["-c", _TOOLS_SERVER])
    endpoints = McpEndpoints(disabled_tools=frozenset({"mcp_docs_delete", "exec"}))
    try:
        path = await endpoints.open("other", "files", cfg)
        replies = await _round_trip(path, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    finally:
        await endpoints.aclose()

    assert [tool["name"] for tool in replies[1]["result"]["tools"]] == ["search", "delete", "actions/download-logs"]
