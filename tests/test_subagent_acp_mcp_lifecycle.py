"""Endpoints an ACP dispatch opens are the endpoints it closes."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from raven.agent.acp_client.capabilities import CapabilitySnapshot
from raven.agent.acp_client.pool import close_pool
from raven.agent.acp_client.protocol import SESSION_MCP_CAPABILITY, AcpRemoteError
from raven.agent.subagent.backends import build_third_party_backend
from raven.agent.subagent.backends.acp_agent import AcpAgentBackend
from raven.agent.subagent.mcp_grant import McpServerView
from raven.config.schema import MCPServerConfig, ThirdPartyAcpSubagentConfig
from raven.mcp.endpoint import McpEndpoints, bridge_command, socket_dir

_STUB = Path(__file__).with_name("acp_stub_server.py")
# A server that never answers: nothing here talks MCP to it, and the endpoint
# does not spawn it until a sub-agent actually connects to the socket.
_IDLE_SERVER_ARGS = ["-c", "import sys; sys.stdin.read()"]


def _idle_config() -> MCPServerConfig:
    return MCPServerConfig(command=sys.executable, args=_IDLE_SERVER_ARGS)


@pytest.mark.asyncio
async def test_a_nodes_endpoints_are_gone_when_that_node_ends():
    cfg = _idle_config()
    endpoints = McpEndpoints()
    a = await endpoints.open("node-a", "s1", cfg)
    b = await endpoints.open("node-b", "s1", cfg)
    assert a.exists() and b.exists()

    await endpoints.close("node-a")
    assert not a.exists()
    assert b.exists(), "closing one node must not take another node's endpoint with it"

    await endpoints.aclose()
    assert not b.exists()


@pytest.mark.asyncio
async def test_two_nodes_asking_for_the_same_server_get_different_endpoints():
    endpoints = McpEndpoints()
    assert endpoints.path_for("node-a", "s1") != endpoints.path_for("node-b", "s1")


@pytest.mark.asyncio
async def test_reopening_after_a_crash_left_the_socket_file_behind():
    cfg = _idle_config()
    endpoints = McpEndpoints()
    path = endpoints.path_for("node-c", "s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")  # a stale file, not a socket
    try:
        reopened = await endpoints.open("node-c", "s1", cfg)
        assert reopened == path
    finally:
        await endpoints.aclose()


# ---- the dispatch's own wiring ---------------------------------------------


class _Source:
    def __init__(self, servers: dict[str, MCPServerConfig], *, disabled_tools: frozenset[str] = frozenset()) -> None:
        self._servers = servers
        self._disabled_tools = disabled_tools

    def server(self, name: str) -> McpServerView | None:
        config = self._servers.get(name)
        return None if config is None else McpServerView(name=name, config=config, state="connected")

    def tools(self, name: str) -> tuple[Any, ...]:
        return ()

    def disabled_tools(self) -> frozenset[str]:
        return self._disabled_tools


@pytest.fixture(autouse=True)
async def _no_pooled_connections():
    """Close pooled connections so a stub process never outlives its test."""
    yield
    await close_pool()


@pytest.fixture
def raven_on_the_adapter_path(tmp_path: Path) -> Path:
    """A ``raven`` the adapter's PATH can find, which is what a grant requires.

    A shim rather than the installed console script: the assertions below name
    the executable the sub-agent was handed, and this makes that name a fact of
    the test rather than of whatever happens to be on the runner's PATH.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "raven"
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)
    return shim


def _backend(shim: Path, *, mode: str = "ok", mcps: list[str] | None = None) -> AcpAgentBackend:
    cfg = ThirdPartyAcpSubagentConfig(
        name="bridged",
        command=f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": mode, "PATH": f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}"},
        ready_timeout_ms=15000,
        mcps=["echo"] if mcps is None else mcps,
    )
    backend = build_third_party_backend(cfg)
    backend.set_mcp_source(_Source({"echo": _idle_config()}))
    return backend


@pytest.mark.asyncio
async def test_session_new_is_handed_a_live_bridge_endpoint(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path
) -> None:
    """The stanza that reaches the adapter names the bridge and a bound socket.

    Read at ``_open_session`` because that is the last point the dispatch owns
    before ``session/new`` goes out, so a socket that exists here existed before
    the adapter was told to connect to it.
    """
    backend = _backend(raven_on_the_adapter_path)
    seen: dict[str, Any] = {}
    real = AcpAgentBackend._open_session

    async def spy(self, client, **kwargs):
        seen["mcp_servers"] = kwargs["mcp_servers"]
        seen["bound"] = [Path(entry["args"][-1]).exists() for entry in kwargs["mcp_servers"]]
        return await real(self, client, **kwargs)

    monkeypatch.setattr(AcpAgentBackend, "_open_session", spy)

    assert await backend.run("ping", task_id="node-x", workspace=tmp_path, executor=None) == "pong"

    # Read out of the stanza rather than predicted: the dispatch owns the
    # McpEndpoints instance that salts these paths, and nothing outside it has
    # any business deriving one.
    [entry] = seen["mcp_servers"]
    endpoint = Path(entry["args"][-1])
    # The command comes from the resolver rather than being restated here: which
    # raven is handed over is its rule (this build's own, not a PATH lookup) and
    # has its own tests. This one is about the stanza and a socket already bound.
    argv = bridge_command()
    assert argv is not None
    assert entry == {
        "name": "echo",
        "command": argv[0],
        "args": ["mcp", "bridge", str(endpoint)],
        "env": [],
    }
    assert endpoint.parent == socket_dir()
    assert endpoint.suffix == ".sock"
    assert len(str(endpoint).encode()) < 104, "AF_UNIX caps the whole path at 104 bytes"
    assert seen["bound"] == [True], "the endpoint must be listening before session/new names it"
    assert not endpoint.exists(), "the dispatch that opened it is over"


@pytest.mark.asyncio
async def test_a_failed_dispatch_still_reaps_its_endpoints(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path
) -> None:
    # `no_session` answers session/new with an error, so the dispatch fails at
    # the one moment its endpoints are already listening.
    backend = _backend(raven_on_the_adapter_path, mode="no_session")
    seen: dict[str, Any] = {}
    real = AcpAgentBackend._open_session

    async def spy(self, client, **kwargs):
        seen["paths"] = [Path(entry["args"][-1]) for entry in kwargs["mcp_servers"]]
        seen["bound"] = [path.exists() for path in seen["paths"]]
        return await real(self, client, **kwargs)

    monkeypatch.setattr(AcpAgentBackend, "_open_session", spy)

    with pytest.raises(AcpRemoteError):
        await backend.run("ping", task_id="node-y", workspace=tmp_path, executor=None)
    assert seen["bound"] == [True], "nothing is proven unless the endpoint was open when it failed"
    assert [path.exists() for path in seen["paths"]] == [False]


# ---- the per-session MCP promise -------------------------------------------


def _snapshot(*, agent_name: str, session_mcp: bool) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        agent="bridged",
        fingerprint="fp",
        status="ready",
        detail="",
        measured_at_ms=1,
        agent_name=agent_name,
        session_mcp=session_mcp,
    )


def _measured_backend(
    shim: Path, *, agent_name: str, session_mcp: bool, declared_isolating: bool = True
) -> AcpAgentBackend:
    """The same dispatch as ``_backend``, with a handshake already on record.

    ``session_mcp`` is what the *handshake* reported; ``declared_isolating`` is
    what the agent row says. They are separate inputs because they answer
    separate questions -- see ``AcpAgentBackend._session_mcp_refused``.
    """
    backend = AcpAgentBackend(
        name="bridged",
        command=f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": "ok", "PATH": f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}"},
        ready_timeout_ms=15000,
        mcps=["echo"],
        snapshot=_snapshot(agent_name=agent_name, session_mcp=session_mcp),
        session_mcp=declared_isolating,
    )
    backend.set_mcp_source(_Source({"echo": _idle_config()}))
    return backend


async def _dispatch(backend: AcpAgentBackend, monkeypatch, workspace: Path, node: str) -> tuple[list[Any], str]:
    """Run one task and return what ``session/new`` was handed, plus the reply."""
    seen: list[Any] = []
    real = AcpAgentBackend._open_session

    async def spy(self, client, **kwargs):
        seen.append(kwargs["mcp_servers"])
        return await real(self, client, **kwargs)

    monkeypatch.setattr(AcpAgentBackend, "_open_session", spy)
    reply = await backend.run("ping", task_id=node, workspace=workspace, executor=None)
    return seen[0], reply


def test_an_unmeasured_peer_is_never_refused() -> None:
    """No snapshot is not evidence of a refusal.

    The only peer that refuses is an old raven, and identifying one takes a
    measured ``agentInfo.name``. Reading absence as refusal would withhold MCP
    from every agent nobody has verified yet -- including all three third
    parties.
    """
    backend = AcpAgentBackend(name="unknown", command="/bin/true", snapshot=None)
    assert backend._session_mcp_refused is False


@pytest.mark.asyncio
async def test_a_raven_peer_without_the_promise_is_told_why_it_got_nothing(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path
) -> None:
    """An old raven answers a non-empty ``mcpServers`` with ``-32602``.

    So the field is withheld rather than sent -- and the withholding is on the
    reply, because a sub-agent handed an empty list looks exactly like one
    nobody configured, which is the harder failure to find.
    """
    backend = _measured_backend(raven_on_the_adapter_path, agent_name="raven", session_mcp=False)

    handed, reply = await _dispatch(backend, monkeypatch, tmp_path, "node-old-raven")

    assert handed == [], "an old raven must not be sent the field at all"
    assert reply.startswith("pong"), "withholding degrades the dispatch, it does not fail it"
    assert "'echo'" in reply and SESSION_MCP_CAPABILITY in reply
    assert "withheld" in reply


@pytest.mark.asyncio
async def test_a_raven_peer_that_declares_the_promise_is_handed_the_bridge(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path
) -> None:
    backend = _measured_backend(raven_on_the_adapter_path, agent_name="raven", session_mcp=True)

    handed, reply = await _dispatch(backend, monkeypatch, tmp_path, "node-new-raven")

    assert [entry["name"] for entry in handed] == ["echo"]
    assert reply == "pong", "nothing was degraded, so nothing is explained"


@pytest.mark.asyncio
async def test_an_agent_declared_as_not_isolating_is_withheld_and_told_why(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path
) -> None:
    """``sessionMcp: false`` withholds delivery, on a peer that would accept it.

    The stanza would be connected -- that is the point: on an agent whose sessions
    share a tool surface, accepting it is what makes this dispatch's servers
    reachable from every concurrent sub-agent of the same agent, because raven
    pools one connection per agent name. Withheld with a note rather than sent,
    for the same reason the raven branch withholds: a sub-agent that silently got
    nothing looks exactly like one nobody configured.
    """
    backend = _measured_backend(
        raven_on_the_adapter_path, agent_name="OpenCode", session_mcp=False, declared_isolating=False
    )

    handed, reply = await _dispatch(backend, monkeypatch, tmp_path, "node-not-isolating")

    assert handed == [], "a peer that does not isolate must not be sent the field"
    assert reply.startswith("pong"), "withholding degrades the dispatch, it does not fail it"
    assert "'echo'" in reply and "withheld" in reply
    assert "sessionMcp" in reply
    # Not the other branch's reason: this peer declares no promise either, and
    # naming that one would send the reader to upgrade a raven it is not.
    assert SESSION_MCP_CAPABILITY not in reply


@pytest.mark.asyncio
async def test_the_operator_log_names_which_refusal_it_was(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path, caplog
) -> None:
    """The two reasons call for different actions -- upgrade that agent's raven,
    or wait for the peer to isolate -- so a shared sentence sends half the
    readers to do the wrong thing. Caught in a real run: an agent declared as not
    isolating was logged as a raven build missing its promise.
    """

    from loguru import logger as _logger

    lines: list[str] = []
    sink = _logger.add(lambda m: lines.append(m), level="WARNING")
    try:
        declared_off = _measured_backend(
            raven_on_the_adapter_path, agent_name="OpenCode", session_mcp=False, declared_isolating=False
        )
        await _dispatch(declared_off, monkeypatch, tmp_path, "node-declared-off")

        old_raven = _measured_backend(raven_on_the_adapter_path, agent_name="raven", session_mcp=False)
        await _dispatch(old_raven, monkeypatch, tmp_path, "node-old-raven")
    finally:
        _logger.remove(sink)

    text = "".join(lines)
    assert "sessionMcp: false" in text
    assert SESSION_MCP_CAPABILITY in text
    # And not swapped: the declaration reason must not name the promise.
    declared_line = next(line for line in lines if "sessionMcp: false" in line)
    assert SESSION_MCP_CAPABILITY not in declared_line


def test_the_two_refusals_are_independent(raven_on_the_adapter_path: Path) -> None:
    """One is the agent's own property, the other is a build that would error.

    Pinned as a table because the interesting cell is the third: a peer declared
    as isolating and carrying the promise is the only one that gets delivery, and
    an old raven on a row that says it isolates is still refused.
    """
    cases = {
        # (declared_isolating, handshake promise) -> refused?
        (True, True): False,
        (True, False): True,  # an old raven build
        (False, True): True,  # the row says it does not isolate
        (False, False): True,
    }
    for (declared, promised), expected in cases.items():
        backend = _measured_backend(
            raven_on_the_adapter_path, agent_name="raven", session_mcp=promised, declared_isolating=declared
        )
        assert backend._session_mcp_refused is expected, f"declared={declared} promised={promised}"


def test_a_third_party_row_that_says_it_does_not_isolate_is_refused_too(
    raven_on_the_adapter_path: Path,
) -> None:
    """The declaration is not a raven-only branch.

    The promise test is, deliberately -- it identifies a build. This one is the
    peer's own property, so it has to bind on an agent that never declares
    anything, which is every third party.
    """
    backend = _measured_backend(
        raven_on_the_adapter_path, agent_name="OpenCode", session_mcp=False, declared_isolating=False
    )
    assert backend._session_mcp_refused is True


@pytest.mark.parametrize(
    "agent_name",
    [
        "@agentclientprotocol/claude-agent-acp",
        "@agentclientprotocol/codex-acp",
        "OpenCode",
    ],
)
@pytest.mark.asyncio
async def test_a_third_party_peer_keeps_its_bridge_without_declaring_anything(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path, agent_name: str
) -> None:
    """The three measured adapters never declare raven's ``_meta`` promise.

    They do not need to: a bridge stanza is an ordinary stdio server, which is
    the ACP baseline, and its per-session lifetime is the host socket's lifetime.
    Gating delivery on the promise would turn all three off, so this is the
    regression the promise must never cause.
    """
    backend = _measured_backend(raven_on_the_adapter_path, agent_name=agent_name, session_mcp=False)

    handed, reply = await _dispatch(backend, monkeypatch, tmp_path, f"node-{agent_name[-6:]}")

    assert [entry["name"] for entry in handed] == ["echo"]
    assert Path(handed[0]["args"][-1]).parent == socket_dir()
    assert reply == "pong"


# ---- what a dispatch pays for the adapter's PATH ---------------------------


class _Capture:
    """A ``login_shell_env`` stand-in that costs what the real one costs.

    Two properties of the real capture are modelled, because both assertions
    below hang off them: it runs the user's login shell under a 15-second budget,
    so one call blocks for as long as that profile takes; and it memoizes per
    process, so the first caller pays and every later one reads the answer.

    Whether the loop kept running is answered by the return value rather than by
    a clock: the blocking call waits for an event a coroutine sets, which a
    worker thread sees at once and the loop thread can never see -- nothing else
    runs on the loop while this is what it is doing.
    """

    def __init__(self, beat: threading.Event) -> None:
        self._beat = beat
        self.calls = 0
        self.paid_on: int | None = None
        self.loop_kept_running: bool | None = None
        self._env: dict[str, str] | None = None

    def __call__(self) -> dict[str, str]:
        self.calls += 1
        if self._env is None:
            self.paid_on = threading.get_ident()
            self._beat.clear()
            self.loop_kept_running = self._beat.wait(timeout=3)
            self._env = {}
        return dict(self._env)


@asynccontextmanager
async def _capture_under_a_beating_loop(monkeypatch):
    """Patch the capture, with a coroutine ticking for as long as it blocks."""
    beat = threading.Event()
    capture = _Capture(beat)
    monkeypatch.setattr("raven.agent.subagent.backends.acp_agent.login_shell_env", capture)

    async def heartbeat() -> None:
        while True:
            beat.set()
            await asyncio.sleep(0.01)

    task = asyncio.create_task(heartbeat())
    try:
        yield capture
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_dispatch_that_wants_no_mcp_never_captures_the_login_shell(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path
) -> None:
    """An entry exporting nothing must not pay for the adapter's PATH.

    Both halves of the dispatch are exercised, because each used to ask on its
    own: the preflight resolution built its target before it knew the effective
    list, and the endpoint context built the bridge argv before it looked at the
    grant. An empty grant is empty whatever the PATH turns out to be, so neither
    answer was ever used -- and on a slow shell profile each ask is up to 15
    seconds of a frozen event loop.
    """
    backend = _backend(raven_on_the_adapter_path, mcps=[])

    async with _capture_under_a_beating_loop(monkeypatch) as capture:
        assert backend.resolve_mcp_grant().granted == ()
        assert await backend.run("ping", task_id="node-no-mcp", workspace=tmp_path, executor=None) == "pong"

    assert capture.calls == 0, "a dispatch with nothing to bridge asked for the adapter's PATH anyway"


@pytest.mark.asyncio
async def test_the_capture_a_bridged_dispatch_needs_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path
) -> None:
    """A dispatch that does need the PATH must not block the loop for it.

    The capture shells out, so the one call that pays belongs on a worker thread
    -- the same move the cli transport and the ACP client spawn already make.
    Run on the loop instead, it stops every other turn in the process for as
    long as the login shell takes.
    """
    backend = _backend(raven_on_the_adapter_path)
    loop_thread = threading.get_ident()

    async with _capture_under_a_beating_loop(monkeypatch) as capture:
        reply = await backend.run("ping", task_id="node-bridged", workspace=tmp_path, executor=None)

    assert reply == "pong"
    assert capture.paid_on is not None, "nothing is proven unless the capture actually ran"
    assert capture.paid_on != loop_thread, "the capture ran on the event loop thread"
    assert capture.loop_kept_running is True, "no coroutine could run while the capture blocked"


@pytest.mark.asyncio
async def test_the_endpoint_gets_the_hosts_sandbox_and_deny_set(
    tmp_path: Path, raven_on_the_adapter_path: Path, monkeypatch
) -> None:
    """The policy travels from the host's MCP source to the endpoint.

    Without this wiring both review fixes are mechanism with nothing behind
    them: a bridged stdio server would spawn on the host whatever the sandbox
    config says, and a tool the host switched off would still be reachable
    through the relay.
    """
    seen: dict[str, object] = {}
    real_init = McpEndpoints.__init__

    def spy(self, *args, **kwargs):
        seen["executor_provider"] = kwargs.get("executor_provider")
        seen["disabled_tools"] = kwargs.get("disabled_tools")
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(McpEndpoints, "__init__", spy)

    async def provider():
        return "the-host-executor"

    backend = _backend(raven_on_the_adapter_path, mode="ok")
    source = _Source({"echo": _idle_config()}, disabled_tools=frozenset({"mcp_echo_ping"}))
    source.executor_provider = lambda: provider  # type: ignore[attr-defined]
    backend.set_mcp_source(source)

    with contextlib.suppress(Exception):
        async with backend._mcp_endpoints("node-policy", backend.resolve_mcp_grant(["echo"])):
            pass

    assert seen["executor_provider"] is provider
    assert seen["disabled_tools"] == frozenset({"mcp_echo_ping"})


async def test_a_bridged_oauth_server_is_dialled_with_the_hosts_credential(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path
) -> None:
    """The endpoint opens its own upstream, so it needs what the host attaches.

    Without it an OAuth server answered the bridge with 401 while the same
    server was connected and listed on the host: nothing on the host side looked
    wrong, and the sub-agent saw a server that never finished connecting. The
    401 then surfaced as an ExceptionGroup naming no cause, so neither half of
    the failure said what it was.

    The credential is used on the host's side of the relay; the sub-agent still
    receives only frames.
    """
    opened: dict[str, Any] = {}
    real_open = McpEndpoints.open

    async def spy(self, node_id, server, cfg, http_auth=None):
        opened[server] = http_auth
        return await real_open(self, node_id, server, cfg, http_auth=http_auth)

    monkeypatch.setattr(McpEndpoints, "open", spy)

    sentinel = object()

    async def fake_provider_for(name, cfg, **kwargs):
        opened["can_park"] = kwargs.get("can_park")
        return sentinel

    monkeypatch.setattr("raven.mcp.oauth.provider_for", fake_provider_for)

    backend = _backend(raven_on_the_adapter_path)
    cfg = _idle_config()
    cfg.auth = "oauth"
    backend.set_mcp_source(_Source({"echo": cfg}))

    with contextlib.suppress(Exception):
        async with backend._mcp_endpoints("node-auth", backend.resolve_mcp_grant(["echo"])):
            pass

    assert opened.get("echo") is sentinel
    # A dispatch cannot wait on a browser: a stored token attaches and works, and
    # one that needs authorizing costs this server rather than the node.
    assert opened.get("can_park") is False


async def test_a_bridged_server_without_oauth_is_dialled_with_no_credential(
    tmp_path: Path, monkeypatch, raven_on_the_adapter_path: Path
) -> None:
    """The other half: nothing is fabricated for a server that declares no auth."""
    opened: dict[str, Any] = {}
    real_open = McpEndpoints.open

    async def spy(self, node_id, server, cfg, http_auth=None):
        opened[server] = http_auth
        return await real_open(self, node_id, server, cfg, http_auth=http_auth)

    monkeypatch.setattr(McpEndpoints, "open", spy)
    backend = _backend(raven_on_the_adapter_path)
    backend.set_mcp_source(_Source({"echo": _idle_config()}))

    with contextlib.suppress(Exception):
        async with backend._mcp_endpoints("node-plain", backend.resolve_mcp_grant(["echo"])):
            pass

    assert opened.get("echo") is None
