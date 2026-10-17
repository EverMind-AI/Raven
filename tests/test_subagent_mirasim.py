"""The Mirasim presets end to end: Raven's own ACP client driving the adapter over a stand-in Mirasim host."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from raven.acp_client.acp_agent import AcpAgentBackend
from raven.acp_client.capabilities import verify_agent
from raven.acp_client.client import AcpClient
from raven.acp_client.pool import close_pool
from raven.agent.subagent.instances import InstanceRegistry
from raven.agent.subagent.mirasim_presets import MIRASIM_AGENTS, MIRASIM_PRESETS
from raven.agent.subagent.presets import third_party_subagent_preset
from raven.config.schema import ThirdPartyAcpSubagentConfig
from raven.utils.commands import command_argv
from tests.mirasim_fake_host import FakeHost


@pytest.fixture(autouse=True)
async def _no_pooled_connections():
    yield
    await close_pool()


@pytest.fixture
async def host() -> Any:
    fake = FakeHost()
    await fake.start()
    yield fake
    await fake.stop()


def adapter_config(tmp_path: Path, host: FakeHost) -> ThirdPartyAcpSubagentConfig:
    """The shipped claude row, pointed at a stand-in host and homes of its own."""
    assert host.address is not None and host.address.port is not None
    row = third_party_subagent_preset("mirasim_claude")
    env = {
        "RAVEN_HOME": str(tmp_path / "home"),
        "MIRASIM_HOME": str(tmp_path / "mirasim"),
        "RAVEN_MIRASIM_HOST": str(host.address.port),
    }
    return ThirdPartyAcpSubagentConfig.model_validate({**row, "env": env})


def test_each_row_runs_the_adapter_on_this_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUBAGENT_PYTHON", raising=False)
    for key, (agent, label, _) in MIRASIM_AGENTS.items():
        row = third_party_subagent_preset(key)
        assert row["name"] == f"{label}-Mirasim"
        assert command_argv(row["command"]) == [sys.executable, "-P", "-m", "raven.acp.mirasim", "--agent", agent]
        assert row["sessionMcp"] is False
    # The table itself keeps the template, so nothing machine-specific is shipped.
    assert all(row["command"].startswith("{PYTHON} ") for row in MIRASIM_PRESETS.values())
    interpreter = str(Path("Python with spaces") / "python")
    monkeypatch.setenv("SUBAGENT_PYTHON", interpreter)
    assert command_argv(third_party_subagent_preset("mirasim_codex")["command"]) == [
        interpreter,
        "-P",
        "-m",
        "raven.acp.mirasim",
        "--agent",
        "codex",
    ]


@pytest.mark.slow
async def test_verify_records_a_resumable_agent_with_mirasims_menus(tmp_path: Path, host: FakeHost) -> None:
    snapshot = await verify_agent(adapter_config(tmp_path, host))
    assert snapshot.status == "ready", snapshot.detail
    assert snapshot.can_load and snapshot.can_steer
    assert snapshot.agent_name == "mirasim-claude"
    # Claude Code's own models twice, on its own account and on the platform.
    assert [(choice.group, choice.value) for choice in snapshot.model_choices] == [
        ("Claude Code", "own:sonnet"),
        ("Claude Code", "own:opus"),
        ("Mirasim", "platform:sonnet"),
        ("Mirasim", "platform:opus"),
        ("Mirasim", "glm-5.3-flash"),
        ("Own key", "profile:abc"),
    ]
    assert [mode.id for mode in snapshot.available_modes] == ["low", "high", "max"]
    # The handshake asked the host for its menus and started no turn.
    assert host.frames("listClis") and host.frames("getCatalog") and not host.frames("prompt")


@pytest.mark.slow
async def test_an_instance_continues_its_conversation_in_a_new_adapter_process(tmp_path: Path, host: FakeHost) -> None:
    cfg = adapter_config(tmp_path, host)
    snapshot = await verify_agent(cfg)
    registry = InstanceRegistry(path=tmp_path / "instances.json")

    def backend() -> AcpAgentBackend:
        return AcpAgentBackend(
            name=cfg.name, command=cfg.command, env=dict(cfg.env), snapshot=snapshot, registry=registry
        )

    workspace = tmp_path / "work"
    workspace.mkdir()
    first = await backend().run(
        "remember PELICAN-7", task_id="t1", workspace=workspace, session_key="s", instance="w", executor=None
    )
    match = re.match(r"turn 1 of (claude:\S+) on sonnet effort high: remember PELICAN-7", first)
    assert match, first
    await close_pool()
    second = await backend().run(
        "what was it?", task_id="t2", workspace=workspace, session_key="s", instance="w", executor=None
    )
    assert second.startswith(f"turn 2 of {match.group(1)} ")


@pytest.mark.slow
async def test_a_dispatch_cancelled_in_raven_leaves_no_turn_running(tmp_path: Path, host: FakeHost) -> None:
    host.respond = lambda prompt, session: [("text", "working"), ("hold",)]
    cfg = adapter_config(tmp_path, host)
    workspace = tmp_path / "work"
    workspace.mkdir()
    run = asyncio.create_task(
        AcpAgentBackend(name=cfg.name, command=cfg.command, env=dict(cfg.env)).run(
            "wait", task_id="t1", workspace=workspace, executor=None
        )
    )
    session = await host.wait_for(lambda: host.sessions and host.session().running and host.session(), limit=60)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    await close_pool()
    await host.wait_for(lambda: session.running is None, limit=10)
    assert host.frames("stop")[0]["sessionKey"] == session.key


@pytest.mark.slow
async def test_closing_the_adapter_stops_its_turn_on_the_shared_host(tmp_path: Path, host: FakeHost) -> None:
    from raven.acp.mirasim.host import HostConnection

    host.respond = lambda prompt, session: [("text", "working"), ("hold",)]
    cfg = adapter_config(tmp_path, host)
    client = await AcpClient.launch(name=cfg.name, command=cfg.command, cwd=str(tmp_path), env=dict(cfg.env))
    pending = None
    try:
        await client.request("initialize", {"protocolVersion": 1}, timeout=30)
        opened = await client.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}, timeout=30)
        pending = asyncio.create_task(
            client.request(
                "session/prompt",
                {"sessionId": opened["sessionId"], "prompt": [{"type": "text", "text": "wait"}]},
                timeout=30,
            )
        )
        session = await host.wait_for(lambda: host.sessions and host.session().running and host.session())
        await client.close()
        await host.wait_for(lambda: session.running is None)
        assert host.frames("stop")[0]["sessionKey"] == session.key
        connection = HostConnection(on_frame=lambda frame: None, locate=lambda: host.address)
        try:
            await connection.connect()
            assert connection.connected
        finally:
            await connection.close()
    finally:
        await client.close()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


async def test_the_agents_mirasim_has_are_asked_of_the_adapter(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    """The question the boot's verification asks before handshaking a preset: the host's agent list."""
    from raven.agent.subagent.mirasim_presets import installed_agents

    assert host.address is not None
    monkeypatch.setenv("RAVEN_MIRASIM_HOST", str(host.address.port))
    assert await installed_agents(path=None) == frozenset({"claude", "codex", "qwen"})


async def test_with_no_host_to_ask_the_adapter_answers_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not an empty set: that would read as every agent missing, and skip the handshakes it was meant to spare."""
    from raven.agent.subagent.mirasim_presets import installed_agents

    monkeypatch.setenv("RAVEN_MIRASIM_HOST", f"unix:{tmp_path / 'no.sock'}" if sys.platform != "win32" else "1")
    assert await installed_agents(path=None) is None
