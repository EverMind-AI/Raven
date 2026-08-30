"""A real ACP adapter reaching a host-held MCP server through a real bridge.

The one combination nothing else runs. ``test_mcp_bridge_e2e.py`` drives the
bridge with the MCP SDK's own ``ClientSession`` standing in for the sub-agent,
and ``tests/test_subagent_acp_mcp_lifecycle.py`` drives the ACP side against a
stub adapter that never speaks MCP. Here all three parties are the real ones:
the adapter each preset pins, ``raven mcp bridge`` as the adapter spawns it, and
a FastMCP server the host holds.

What is asserted is that the sub-agent *used* the server, not that ``session/new``
was accepted -- an adapter that ignored the stanza outright would pass that. Three
facts, each of which can only be produced by one hop of the chain working:

1. the upstream's pid file exists. The host spawns the server only when something
   connects to the endpoint socket, so this is the adapter having launched the
   bridge command and the bridge having reached the host.
2. the marker file holds the note this run put in the prompt. Only the sub-agent
   knew that note; only the host knew the path. So the sub-agent's ``tools/call``,
   carrying its own argument, arrived at the real server.
3. the receipt the tool returns turns up in the adapter's ``session/update``
   stream. Nothing downstream was ever told that string, so the result travelled
   back up through the bridge into the adapter.

The receipt is looked for across the whole update stream rather than in the final
assistant text: the adapter reports a tool result whether or not the model
chooses to quote it, so this asks the transport rather than the model.

Marked ``integration``: real npx adapters, real network on a cold npx cache, and
each agent's own local login. Run with
``uv run pytest tests/integration/test_mcp_bridge_real_adapter.py -q -m integration``.
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from raven.acp_client import protocol
from raven.acp_client.client import AcpClient
from raven.acp_client.permissions import auto_approver
from raven.agent.subagent.mcp_grant import GrantedTool, McpServerView, acp_target, resolve_grant
from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS
from raven.config.schema import MCPServerConfig
from raven.mcp.endpoint import McpEndpoints, bridge_command

pytestmark = pytest.mark.integration

_UPSTREAM = Path(__file__).parent / "_mcp_bridge_upstream.py"

# The three presets whose adapter is a published package this test can pin by
# reading the preset rather than by naming a version here. The other acp presets
# are excluded on purpose: hermes and openclaw are local installs, and
# mirothinker is a remote endpoint, so none of them is the "adapter the operator
# gets from us" case this covers.
_AGENTS = ("claude_code", "codex", "opencode")

_SERVER_NAME = "bridgecheck"

# One prompt turn against a coding agent on a cold npx cache. Generous because
# the failure it must not produce is a timeout that reads as "the bridge did not
# work"; a real refusal comes back as an error long before this.
_PROMPT_TIMEOUT_S = 420.0

_PROMPT = (
    "You have an MCP server named {server!r} connected. It has one tool, 'leave_marker', "
    "which takes a single string argument 'note'. Call that tool exactly once with "
    "note set to the exact string {note!r}. Do not use any other tool, do not read or "
    "write any file, and do not ask any question. When the tool returns, reply with the "
    "exact text it returned and nothing else."
)


class _OneServer:
    """The smallest :class:`McpSource` ``resolve_grant`` accepts: one stdio server.

    Real enough for this path: an acp target never reads ``tools`` or
    ``disabled_tools`` (it requires no host connection and copies no tool list),
    so the only thing ``resolve_grant`` needs from a source here is the config it
    will hand the endpoint.
    """

    def __init__(self, cfg: MCPServerConfig) -> None:
        self._cfg = cfg

    def server(self, name: str) -> McpServerView | None:
        if name != _SERVER_NAME:
            return None
        return McpServerView(name=name, config=self._cfg, state=None)

    def tools(self, name: str) -> tuple[GrantedTool, ...]:
        return ()

    def disabled_tools(self) -> frozenset[str]:
        return frozenset()


@pytest.fixture
def real_home(monkeypatch):
    """Undo the suite's home isolation for the duration of one test.

    Two things break without it, and both are what this test exists to exercise.
    ``AcpClient.launch`` hands the adapter a capture of ``$SHELL -lic env``, and
    that capture starts from the current ``HOME``: pointed at an empty temp dir it
    reads no profile, so the child's PATH stays the bootstrap one and ``npx``
    cannot be found -- the lookup uses the child's PATH, so the failure is an exec
    error before the adapter ever runs. And each adapter authenticates from its own
    directory under the real home, which is the login state these agents are
    documented to reuse.

    ``pwd`` rather than ``$HOME`` or ``Path.home()``: both of those read the
    variable the autouse fixture has already replaced.

    The capture memoises per process, so its globals are cleared too -- otherwise
    whichever test ran first in this worker would decide the environment every
    later one gets. ``monkeypatch`` restores them afterwards, so a later test in
    the same worker is left with what it had.
    """
    from raven.agent.subagent.backends import env as backends_env

    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.setattr(backends_env, "_LOGIN_ENV", None)
    monkeypatch.setattr(backends_env, "_LOGIN_ENV_FAILED", False)


def _reap(pid_file: Path) -> None:
    """SIGKILL the upstream if tearing the endpoint down did not.

    The endpoint's teardown cancels the relay, and the MCP SDK's stdio teardown
    runs inside that cancelled scope, so the terminate it owes the child is not
    guaranteed to be reached. Cheap insurance against an upstream outliving the
    test that spawned it.
    """
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


@pytest.mark.parametrize("agent", _AGENTS)
async def test_a_real_acp_adapter_calls_a_host_held_mcp_server_through_the_bridge(agent, tmp_path, real_home):
    preset = THIRD_PARTY_SUBAGENT_PRESETS[agent]
    command = preset["command"]
    env = dict(preset.get("env") or {})
    budget = max(1.0, int(preset.get("readyTimeoutMs") or 30000) / 1000)

    pid_file = tmp_path / "upstream.pid"
    marker_file = tmp_path / "marker.txt"
    note = f"note-{uuid.uuid4().hex[:12]}"
    receipt = f"receipt-{uuid.uuid4().hex[:12]}"
    cfg = MCPServerConfig(
        command=sys.executable,
        args=[str(_UPSTREAM), str(pid_file), str(marker_file), receipt],
    )

    argv = bridge_command()
    assert argv is not None, "raven must be on this test process's PATH to build a bridge command"

    # The production projection, not a hand-built stanza: resolve_grant decides
    # the acp target's policy and for_acp is what a dispatch actually sends.
    grant = resolve_grant([_SERVER_NAME], _OneServer(cfg), acp_target(allow_secrets=False, stdio_path=None))
    assert grant.granted, f"the server was not granted: {grant.note_text()}"

    updates: list[str] = []

    async def on_notification(method: str, params: dict[str, Any]) -> None:
        updates.append(json.dumps({"method": method, "params": params}, default=str))

    endpoints = McpEndpoints()
    client: AcpClient | None = None
    try:
        path = await endpoints.open("real-adapter", _SERVER_NAME, cfg)
        stanzas = grant.with_endpoints({_SERVER_NAME: str(path)}).for_acp(argv)
        assert stanzas[0]["command"] == argv[0]
        assert stanzas[0]["args"][-1] == str(path)
        assert stanzas[0]["env"] == []

        client = await AcpClient.launch(
            name=agent,
            command=command,
            cwd=str(tmp_path),
            env=env,
            on_request=auto_approver(agent),
            on_notification=on_notification,
        )
        await client.request("initialize", protocol.initialize_params(), timeout=budget)
        session = await client.request("session/new", {"cwd": str(tmp_path), "mcpServers": stanzas}, timeout=budget)
        session_id = session["sessionId"]

        await client.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": _PROMPT.format(server=_SERVER_NAME, note=note)}],
            },
            timeout=_PROMPT_TIMEOUT_S,
            cancel_session=session_id,
        )

        stderr = client.stderr_tail(600)
        assert pid_file.exists(), (
            "the host never spawned the upstream, so nothing connected to the endpoint socket"
            f"; adapter stderr: {stderr}"
        )
        assert marker_file.exists(), (
            f"the sub-agent never called the tool on the host-held server; adapter stderr: {stderr}"
        )
        assert marker_file.read_text() == note, "the tool ran, but not with the note this run's prompt carried"
        assert any(receipt in line for line in updates), (
            "the tool result never reached the adapter: the receipt only the host and the upstream knew"
            f" is absent from {len(updates)} session updates; adapter stderr: {stderr}"
        )
    finally:
        if client is not None:
            await client.close()
        await endpoints.aclose()
        _reap(pid_file)
