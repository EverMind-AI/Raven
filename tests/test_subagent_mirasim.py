"""The mirasim presets end to end: Raven's own ACP client driving the adapter over a stand-in mirasim."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from raven.acp_client.acp_agent import AcpAgentBackend
from raven.acp_client.capabilities import verify_agent
from raven.acp_client.pool import close_pool
from raven.agent.subagent.instances import InstanceRegistry
from raven.agent.subagent.mirasim_presets import MIRASIM_AGENTS, MIRASIM_PRESETS
from raven.agent.subagent.presets import third_party_subagent_preset
from raven.config.schema import ThirdPartyAcpSubagentConfig
from tests.mirasim_stub_cli import install

posix_only = pytest.mark.skipif(os.name == "nt", reason="the stand-in mirasim is a /bin/sh wrapper")


@pytest.fixture(autouse=True)
async def _no_pooled_connections():
    yield
    await close_pool()


def adapter_config(tmp_path: Path, mode: str = "ok") -> ThirdPartyAcpSubagentConfig:
    """The shipped claude row, pointed at a stand-in mirasim and a home of its own."""
    exe = install(tmp_path, mode)
    row = third_party_subagent_preset("mirasim_claude")
    return ThirdPartyAcpSubagentConfig.model_validate(
        {**row, "command": f"{row['command']} --executable {exe}", "env": {"RAVEN_HOME": str(tmp_path / "home")}}
    )


def test_each_row_runs_the_adapter_on_this_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUBAGENT_PYTHON", raising=False)
    for key, (agent, label, _) in MIRASIM_AGENTS.items():
        row = third_party_subagent_preset(key)
        assert row["name"] == f"{label}-Mirasim"
        assert row["command"] == f"{sys.executable} -P -m raven.acp.mirasim --agent {agent}"
        assert row["sessionMcp"] is False
    # The table itself keeps the template, so nothing machine-specific is shipped.
    assert all(row["command"].startswith("{PYTHON} ") for row in MIRASIM_PRESETS.values())
    monkeypatch.setenv("SUBAGENT_PYTHON", "/opt/raven/bin/python")
    assert third_party_subagent_preset("mirasim_codex")["command"].startswith("/opt/raven/bin/python -P ")


@posix_only
@pytest.mark.slow
async def test_verify_records_a_resumable_agent_with_mirasims_menus(tmp_path: Path) -> None:
    snapshot = await verify_agent(adapter_config(tmp_path))
    assert snapshot.status == "ready", snapshot.detail
    assert snapshot.can_load
    assert snapshot.agent_name == "mirasim-claude"
    assert [choice.value for choice in snapshot.model_choices] == ["sonnet", "opus", "glm-5.3-flash", "profile:abc"]
    assert [mode.id for mode in snapshot.available_modes] == ["low", "high", "max"]


@posix_only
@pytest.mark.slow
async def test_an_instance_continues_its_conversation_in_a_new_adapter_process(tmp_path: Path) -> None:
    cfg = adapter_config(tmp_path)
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
    assert first.startswith("turn 1 of sess-1 on sonnet effort high")
    await close_pool()
    second = await backend().run(
        "what was it?", task_id="t2", workspace=workspace, session_key="s", instance="w", executor=None
    )
    assert second.startswith("turn 2 of sess-1 ")


@posix_only
@pytest.mark.slow
async def test_a_dispatch_cancelled_in_raven_leaves_no_turn_running(tmp_path: Path) -> None:
    cfg = adapter_config(tmp_path, mode="hang")
    workspace = tmp_path / "work"
    workspace.mkdir()
    run = asyncio.create_task(
        AcpAgentBackend(name=cfg.name, command=cfg.command, env=dict(cfg.env)).run(
            "wait", task_id="t1", workspace=workspace, executor=None
        )
    )
    marker = tmp_path / "stub" / "grandchild.pid"
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    grandchild = int(marker.read_text())

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    await close_pool()

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    pytest.fail("the turn's grandchild outlived the cancelled dispatch")
