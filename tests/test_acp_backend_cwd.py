"""The workspace handed to the acp backend is the cwd every session gets.

The host resolves a turn's working directory (spawn captures workdir.current(),
direct chat passes the session workdir, DAG nodes read workdir.current() too)
and hands it to the backend as `workspace`; the backend must carry exactly that
into session/new AND session/load - the agent-side gate and workdir binding key
off it. The entry-level `cwd` field stays the operator override.
"""

import asyncio
from typing import Any

from raven.agent.subagent.backends.acp_agent import AcpAgentBackend


class _FakeClient:
    def __init__(self):
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict, timeout: float = 0) -> Any:
        self.requests.append((method, params))
        if method == "session/new":
            return {"sessionId": "acp:fresh"}
        return {}


class _FakeRegistry:
    def __init__(self, known: str | None):
        self._known = known
        self.unbound: list[tuple] = []

    async def lookup(self, *a, **k):
        return self._known

    async def unbind(self, *a):
        self.unbound.append(a)


class _Snapshot:
    def __init__(self, resume: bool, load: bool):
        self.can_resume = resume
        self.can_load = load


def _backend(snapshot=None) -> AcpAgentBackend:
    return AcpAgentBackend(
        name="Raven-Code",
        command="ignored --acp",
        snapshot=snapshot,
        registry=_FakeRegistry(None),
    )


def test_session_new_carries_the_passed_workspace():
    backend = _backend(snapshot=None)  # no snapshot -> stateless -> always session/new
    client = _FakeClient()
    session_id, resumed = asyncio.run(
        backend._open_session(client, cwd="/work/repo", skey="tui:s", handle="h", budget=5)
    )
    assert (session_id, resumed) == ("acp:fresh", False)
    assert client.requests == [("session/new", {"cwd": "/work/repo", "mcpServers": []})]


def test_session_load_carries_the_passed_workspace_too():
    backend = _backend(snapshot=_Snapshot(resume=True, load=True))
    backend._registry = _FakeRegistry("acp:old")
    client = _FakeClient()
    session_id, resumed = asyncio.run(
        backend._open_session(client, cwd="/work/repo", skey="tui:s", handle="h", budget=5)
    )
    assert (session_id, resumed) == ("acp:old", True)
    assert client.requests == [("session/load", {"sessionId": "acp:old", "cwd": "/work/repo", "mcpServers": []})]
