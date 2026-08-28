"""The per-session workdir RPC pair, mirroring the per-session model pair."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.workdir import WorkdirPolicy, WorkdirResolver
from raven.rpc.dispatcher import Dispatcher
from raven.session.manager import SessionManager
from raven.web_rpc.methods_config import register_config_methods


class _FakeChatProvider:
    def get_default_model(self) -> str:
        return "stub-model"


async def _dispatch(d: Dispatcher, method: str, params: dict, rid: int = 1) -> dict:
    """Same helper shape as the dispatch helper used for the per-session model config tests."""
    return await d.dispatch({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


@pytest.fixture
def agent(tmp_path: Path) -> AgentLoop:
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_CHANNEL,
        agent_home=tmp_path,
        session_root=tmp_path / "chanwork",
        sessions=SessionManager(tmp_path),
    )
    return AgentLoop(
        provider=_FakeChatProvider(),
        workspace=tmp_path,
        workdir_resolver=resolver,
        session_manager=SessionManager(tmp_path),
    )


@pytest.fixture
def dispatcher(agent: AgentLoop) -> Dispatcher:
    d = Dispatcher()
    register_config_methods(d, agent=agent)
    return d


@pytest.mark.asyncio
async def test_get_reports_none_and_the_default(dispatcher, agent, tmp_path):
    resp = await _dispatch(dispatcher, "raven.session.workdir.get", {"session_key": "web:abc"})
    assert "error" not in resp, resp
    assert resp["result"]["workdir"] is None
    assert resp["result"]["default"] == str(tmp_path / "chanwork" / "web")


@pytest.mark.asyncio
async def test_set_persists_to_session_metadata(dispatcher, agent, tmp_path):
    target = tmp_path / "project"
    target.mkdir()

    resp = await _dispatch(
        dispatcher,
        "raven.session.workdir.set",
        {"session_key": "web:abc", "workdir": str(target)},
    )

    assert "error" not in resp, resp
    assert agent.sessions.get_or_create("web:abc").metadata["workdir"] == str(target)


@pytest.mark.asyncio
async def test_set_rejects_a_relative_path(dispatcher):
    resp = await _dispatch(
        dispatcher,
        "raven.session.workdir.set",
        {"session_key": "web:abc", "workdir": "relative/dir"},
    )

    assert "error" in resp
    assert "absolute" in str(resp["error"])


@pytest.mark.asyncio
async def test_set_rejects_while_work_is_in_flight(dispatcher, agent, tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    agent.subagents._session_tasks["web:abc"] = {"task-1"}

    resp = await _dispatch(
        dispatcher,
        "raven.session.workdir.set",
        {"session_key": "web:abc", "workdir": str(target)},
    )

    assert "error" in resp
    assert "in flight" in str(resp["error"])


@pytest.mark.asyncio
async def test_clear_rejects_while_work_is_in_flight(dispatcher, agent, tmp_path):
    session = agent.sessions.get_or_create("web:abc")
    session.metadata["workdir"] = str(tmp_path)
    agent.subagents._session_tasks["web:abc"] = {"task-1"}

    resp = await _dispatch(dispatcher, "raven.session.workdir.set", {"session_key": "web:abc", "workdir": None})

    assert "error" in resp
    assert "in flight" in str(resp["error"])
    assert agent.sessions.get_or_create("web:abc").metadata["workdir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_null_clears_the_override(dispatcher, agent, tmp_path):
    session = agent.sessions.get_or_create("web:abc")
    session.metadata["workdir"] = str(tmp_path)

    resp = await _dispatch(dispatcher, "raven.session.workdir.set", {"session_key": "web:abc", "workdir": None})

    assert "error" not in resp, resp
    assert "workdir" not in agent.sessions.get_or_create("web:abc").metadata


@pytest.mark.asyncio
async def test_get_does_not_create_the_default_directory(dispatcher, tmp_path):
    """A read is a read: the UI asking where a session works must not make it so."""
    resp = await _dispatch(dispatcher, "raven.session.workdir.get", {"session_key": "web:abc"})

    assert "error" not in resp, resp
    assert not (tmp_path / "chanwork" / "web" / "abc").exists()


@pytest.mark.asyncio
async def test_set_rejects_agent_home_itself(dispatcher, tmp_path):
    resp = await _dispatch(
        dispatcher,
        "raven.session.workdir.set",
        {"session_key": "web:abc", "workdir": str(tmp_path)},
    )

    assert "error" in resp
    assert "agent home" in str(resp["error"])


@pytest.mark.asyncio
async def test_set_rejects_a_path_outside_the_sandbox_mount(dispatcher, agent, tmp_path, monkeypatch):
    """Storing an override every later turn refuses is worse than refusing it now."""
    outside = tmp_path.parent / "outside_the_mount"
    outside.mkdir(exist_ok=True)
    monkeypatch.setattr(type(agent._executor), "is_sandboxed", property(lambda self: True))

    resp = await _dispatch(
        dispatcher,
        "raven.session.workdir.set",
        {"session_key": "web:abc", "workdir": str(outside)},
    )

    assert "error" in resp
    assert "outside the sandbox mount" in str(resp["error"])
    assert "workdir" not in agent.sessions.get_or_create("web:abc").metadata
