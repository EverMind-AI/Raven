"""Contributed background services: a resident host runs them, and owns them.

The lifecycle half of the services seam: the loop starts what the assembly
attached, exactly once, minting each service the same namespaced grants a
tool receives at bind time; a service that fails to start is left out
loudly; stop runs newest-first and is idempotent. Disposal order is pinned
by the dispose-roster guard beside the swap tests.
"""

from __future__ import annotations

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.contracts.llm_provider import LLMResponse
from raven.providers.base import LLMProvider


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="ok", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake/default"


class _Service:
    def __init__(self, name: str, log: list) -> None:
        self.contributed_by = name
        self._log = log
        self.handles = None

    async def start(self, handles) -> None:
        self.handles = handles
        self._log.append(f"start:{self.contributed_by}")

    async def stop(self) -> None:
        self._log.append(f"stop:{self.contributed_by}")


class _Explosive(_Service):
    async def start(self, handles) -> None:
        raise RuntimeError("no thanks")


def _loop(tmp_path, cron=None) -> AgentLoop:
    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=2),
        host=HostWiring(cron_service=cron),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    return loop


@pytest.mark.asyncio
async def test_start_is_once_stop_is_reversed_and_both_are_idempotent(tmp_path):
    log: list[str] = []
    a, b = _Service("plug-a", log), _Service("plug-b", log)
    loop = _loop(tmp_path)
    loop.plugin_services = (a, b)

    await loop.start_plugin_services()
    await loop.start_plugin_services()
    assert log == ["start:plug-a", "start:plug-b"], "started once each, in order"
    assert a.handles is not None and a.handles.wake_scheduler is None, "no scheduler on this host"

    await loop.stop_plugin_services()
    await loop.stop_plugin_services()
    assert log == ["start:plug-a", "start:plug-b", "stop:plug-b", "stop:plug-a"], "newest first, once"


@pytest.mark.asyncio
async def test_a_failing_service_is_left_out_loudly_and_the_rest_run(tmp_path):
    log: list[str] = []
    bad, good = _Explosive("plug-bad", log), _Service("plug-good", log)
    loop = _loop(tmp_path)
    loop.plugin_services = (bad, good)

    await loop.start_plugin_services()
    assert log == ["start:plug-good"]

    await loop.stop_plugin_services()
    assert log == ["start:plug-good", "stop:plug-good"], "the failed starter is never stopped"


@pytest.mark.asyncio
async def test_a_started_service_receives_the_namespaced_wake_grant(tmp_path):
    from raven.proactive_engine.schedulers.cron.service import CronService

    log: list[str] = []
    svc = _Service("plug-a", log)
    cron = CronService(tmp_path / "jobs.json", allowed_channels=None)
    loop = _loop(tmp_path, cron=cron)
    loop.plugin_services = (svc,)

    await loop.start_plugin_services()
    granted = svc.handles.wake_scheduler
    assert granted is not None
    import time

    job = granted.schedule_wake("c1", int(time.time() * 1000) + 60_000, "look", channel="tui")
    assert job.id == "wake:plug-a:c1", "the same namespaced grant a tool gets at bind"


def test_every_resident_host_starts_and_stops_the_services() -> None:
    """[seam-2] The resident hosts are exactly three assemblies: the gateway's
    generation loop, the rpc stack (serving the page and acp connections), and
    the tui's hand-built server. Each must both start and stop the services --
    a host that starts what it never stops leaks producers into teardown, and
    one that stops what it never started is a dead affordance."""
    import inspect

    from raven.cli import gateway_commands, tui_commands
    from raven.rpc import bootstrap

    for name, src in (
        ("build_rpc_stack", inspect.getsource(bootstrap.build_rpc_stack)),
        ("tui server", inspect.getsource(tui_commands._run_rpc_server_until_done)),
        ("gateway", inspect.getsource(gateway_commands.register)),
    ):
        assert "start_plugin_services" in src, f"{name} never starts the services"
        assert "stop_plugin_services" in src, f"{name} never stops the services"
