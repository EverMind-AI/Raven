"""The Mirasim presets end to end: Raven's own ACP client driving the adapter over a stand-in Mirasim host."""

from __future__ import annotations

import asyncio
import os
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
@pytest.mark.parametrize("queued", [False, True])
async def test_closing_the_adapter_stops_its_turn_on_the_shared_host(
    tmp_path: Path, host: FakeHost, queued: bool
) -> None:
    from raven.acp.mirasim.host import HostConnection

    host.respond = lambda prompt, session: [("text", "working"), ("hold",)]
    cfg = adapter_config(tmp_path, host)
    client = await AcpClient.launch(name=cfg.name, command=cfg.command, cwd=str(tmp_path), env=dict(cfg.env))
    pending = []
    try:
        await client.request("initialize", {"protocolVersion": 1}, timeout=30)
        opened = await client.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}, timeout=30)
        pending.append(
            asyncio.create_task(
                client.request(
                    "session/prompt",
                    {"sessionId": opened["sessionId"], "prompt": [{"type": "text", "text": "wait"}]},
                    timeout=30,
                )
            )
        )
        session = await host.wait_for(lambda: host.sessions and host.session().running and host.session())
        if queued:
            pending.append(
                asyncio.create_task(
                    client.request(
                        "session/prompt",
                        {"sessionId": opened["sessionId"], "prompt": [{"type": "text", "text": "queued"}]},
                        timeout=30,
                    )
                )
            )
            await asyncio.sleep(0)
            await client.request("session/set_mode", {"sessionId": opened["sessionId"], "modeId": "low"}, timeout=10)
        await client.close()
        await host.wait_for(lambda: session.running is None)
        assert host.frames("stop")[0]["sessionKey"] == session.key
        assert len(host.frames("prompt")) == 1
        connection = HostConnection(on_frame=lambda frame: None, locate=lambda: host.address)
        try:
            await connection.connect()
            assert connection.connected
        finally:
            await connection.close()
    finally:
        await client.close()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_closing_the_adapter_waits_for_a_first_prompt_the_host_takes_late_and_stops_it(
    tmp_path: Path, host: FakeHost
) -> None:
    host.respond = lambda prompt, session: [("text", "working"), ("hold",)]
    host.accepting = asyncio.Event()
    cfg = adapter_config(tmp_path, host)
    client = await AcpClient.launch(name=cfg.name, command=cfg.command, cwd=str(tmp_path), env=dict(cfg.env))
    pending = []
    try:
        await client.request("initialize", {"protocolVersion": 1}, timeout=30)
        opened = await client.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}, timeout=30)
        prompt = {"sessionId": opened["sessionId"], "prompt": [{"type": "text", "text": "wait"}]}
        pending.append(asyncio.create_task(client.request("session/prompt", prompt, timeout=30)))
        await host.wait_for(lambda: host.frames("prompt"))
        closing = asyncio.create_task(client.close())
        # Past the grace every local agent gets: only the one this adapter asks for covers it.
        await asyncio.sleep(1.5)
        host.accepting.set()
        await closing
        session = host.session()
        await host.wait_for(lambda: session.running is None)
        assert [frame["sessionKey"] for frame in host.frames("stop")] == [session.key]
    finally:
        host.accepting.set()
        await client.close()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.slow
async def test_the_live_preset_fixture_keeps_its_desktop_token_home(tmp_path: Path, monkeypatch) -> None:
    import importlib.util

    desktop = tmp_path / "desktop profile"
    state = desktop / ".mirasim"
    (state / "run").mkdir(parents=True)
    host = FakeHost(token="fixture-token")
    await host.start()
    try:
        assert host.address is not None
        (state / "run" / f"local-{host.address.port}.token").write_text(host.token, encoding="utf-8")
        monkeypatch.delenv("MIRASIM_HOME", raising=False)
        monkeypatch.setenv("HOME", str(desktop))
        monkeypatch.setenv("USERPROFILE", str(desktop))
        monkeypatch.setenv("RAVEN_MIRASIM_HOST", str(host.address.port))
        spec = importlib.util.spec_from_file_location(
            "mirasim_live_fixture", Path(__file__).parent / "integration" / "test_acp_mirasim_real_llm.py"
        )
        assert spec is not None and spec.loader is not None
        live = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(live)
        assert live.HOST == host.address
        isolated = tmp_path / "isolated profile"
        isolated.mkdir()
        monkeypatch.setenv("HOME", str(isolated))
        monkeypatch.setenv("USERPROFILE", str(isolated))
        workdir = tmp_path / "work"
        workdir.mkdir()
        monkeypatch.setattr(live, "WORKDIR", workdir)
        await asyncio.wait_for(live.test_raven_dispatches_to_a_mirasim_preset(tmp_path), 40)
        assert host.frames("prompt")[0]["prompt"] == "Reply with exactly: PONG"
    finally:
        await host.stop()


async def test_the_agents_mirasim_has_are_asked_of_the_adapter_in_the_login_shells_environment(
    host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The question the boot's verification asks before handshaking a preset: the host's agent list.

    Asked where the presets' adapters look -- the login shell's environment -- not in raven's own.
    """
    from raven.agent.subagent.backends import env as backends_env
    from raven.agent.subagent.mirasim_presets import installed_agents

    assert host.address is not None
    monkeypatch.setenv("RAVEN_MIRASIM_HOST", f"unix:{tmp_path / 'not-this.sock'}" if sys.platform != "win32" else "1")
    login = {**os.environ, "RAVEN_MIRASIM_HOST": str(host.address.port)}
    monkeypatch.setattr(backends_env, "login_shell_env", lambda: login)
    assert await installed_agents(path=None) == frozenset({"claude", "codex", "qwen"})


async def test_with_no_host_running_the_adapter_lists_no_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """None is installed as far as any handshake could tell: each would fail on the same missing host."""
    from raven.agent.subagent.backends import env as backends_env
    from raven.agent.subagent.mirasim_presets import installed_agents

    address = f"unix:{tmp_path / 'no.sock'}" if sys.platform != "win32" else "1"
    monkeypatch.setattr(backends_env, "login_shell_env", lambda: {**os.environ, "RAVEN_MIRASIM_HOST": address})
    assert await installed_agents(path=None) == frozenset()


async def test_a_host_that_does_not_list_its_agents_in_time_leaves_it_to_the_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from raven.agent.subagent import mirasim_presets
    from raven.agent.subagent.backends import env as backends_env

    monkeypatch.setattr(backends_env, "login_shell_env", lambda: dict(os.environ))
    monkeypatch.setattr(mirasim_presets, "INSTALLED_QUERY", '{PYTHON} -c "import sys; sys.exit(1)"')
    assert await mirasim_presets.installed_agents(path=None) is None


def test_a_rows_budgets_cover_the_adapters_own_slowest_answers() -> None:
    """Past its own limits the adapter refuses and says why; a budget under them reads as an agent that hung."""
    from raven.acp.mirasim import host as host_mod
    from raven.acp.mirasim import server as server_mod
    from raven.agent.subagent import mirasim_presets

    connect = host_mod.HEALTH_TIMEOUT_S + host_mod.CONNECT_TIMEOUT_S
    menus = server_mod.CLIS_TIMEOUT_S + server_mod.QUERY_TIMEOUT_S
    # A session/new may read the menus twice: again when the first read says the agent is not installed.
    for row in MIRASIM_PRESETS.values():
        assert row["readyTimeoutMs"] / 1000 > connect + 2 * menus
    assert mirasim_presets.INSTALLED_TIMEOUT_S > connect + server_mod.CLIS_TIMEOUT_S


def test_the_host_variables_a_row_can_name_are_the_ones_the_adapter_reads() -> None:
    from raven.acp.mirasim import host as host_mod
    from raven.agent.subagent.mirasim_presets import HOST_ENV_KEYS

    assert set(HOST_ENV_KEYS) == {host_mod.HOST_ENV, "MIRASIM_HOME"}
