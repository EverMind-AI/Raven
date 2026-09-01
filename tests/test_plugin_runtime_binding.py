"""Late binding: the contribution shape for tools that need the living loop.

A factory runs in the assembly root, before the loop exists. These pin the
whole grant path at the loop level: a declarer receives the frozen handles
exactly once with the loop's own objects in them, a non-declarer is left
alone, and a tool that raises while binding is unregistered loudly instead of
serving half-bound.
"""

from __future__ import annotations

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, HostWiring, ToolWiring, TurnPolicy
from raven.contracts.llm_provider import LLMResponse
from raven.plugins.context import RuntimeHandles
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


class _Bindable:
    name = "bindable_probe"
    description = "records the handles the loop grants"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.bound: list[RuntimeHandles] = []

    def bind_runtime(self, handles: RuntimeHandles) -> None:
        self.bound.append(handles)

    async def execute(self, **kwargs) -> str:
        return "ok"


class _Plain:
    name = "plain_probe"
    description = "declares nothing and must be left alone"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "ok"


class _Explosive:
    name = "explosive_probe"
    description = "raises while binding and must not serve half-bound"
    parameters = {"type": "object", "properties": {}}

    def bind_runtime(self, handles: RuntimeHandles) -> None:
        raise RuntimeError("no thanks")

    async def execute(self, **kwargs) -> str:
        return "ok"


def _loop(tmp_path, plugin_tools, playbook_config=None) -> AgentLoop:
    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=2),
        host=HostWiring(),
        tools=ToolWiring(restrict_to_workspace=True, plugin_tools=plugin_tools),
        engine=EngineWiring(playbook_config=playbook_config),
    )

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


def test_a_declarer_is_bound_once_with_the_loops_own_handles(tmp_path):
    probe = _Bindable()
    plain = _Plain()
    loop = _loop(tmp_path, [probe, plain])

    assert len(probe.bound) == 1
    handles = probe.bound[0]
    assert handles.subagent_registry is loop.subagents.registry
    assert handles.session_dir == loop.sessions.session_dir
    assert handles.subagents_paused() == loop.subagents.paused
    assert loop.tools.get("plain_probe") is plain, "a non-declarer is registered untouched"
    with pytest.raises(Exception):
        handles.session_dir = None  # type: ignore[misc]


def test_a_tool_that_raises_while_binding_is_unregistered(tmp_path):
    loop = _loop(tmp_path, [_Explosive(), _Plain()])

    assert loop.tools.get("explosive_probe") is None, "half-bound tools must not serve"
    assert loop.tools.get("plain_probe") is not None, "one bad plugin does not take the others down"


def test_the_playbook_funnel_reaches_the_bundled_tools_by_binding(tmp_path):
    """The whole grant path for the shelf's first real resident: the loop
    assembles the funnel, and the two bundled tools serve exactly that object
    -- same runtime, same generator, same store, same adopt."""
    from raven.agent.tools.create_playbook import CreatePlaybookTool
    from raven.agent.tools.load_playbook import LoadPlaybookTool
    from raven.config.schema import PlaybookConfig

    load, create = LoadPlaybookTool(), CreatePlaybookTool()
    loop = _loop(tmp_path, [load, create], playbook_config=PlaybookConfig(enabled=True))

    assert loop._playbooks is not None, "the loop was configured to build the funnel"
    assert loop.tools.get("load_playbook") is load
    assert loop.tools.get("create_playbook") is create
    assert load._runtime is loop._playbooks
    assert create._generator is loop._playbooks.generator
    assert create._store is loop._playbooks.store
    assert create._adopt == loop._playbooks.adopt


def test_a_loop_without_playbooks_takes_the_bundled_tools_off_the_table(tmp_path):
    """No funnel, no service: the tools decline and are unregistered quietly,
    so `playbooks.enabled: false` turns the feature exactly as far off as it
    was when the loop registered the tools itself."""
    from raven.agent.tools.create_playbook import CreatePlaybookTool
    from raven.agent.tools.load_playbook import LoadPlaybookTool

    loop = _loop(tmp_path, [LoadPlaybookTool(), CreatePlaybookTool(), _Plain()])

    assert loop.tools.get("load_playbook") is None, "an unbound loader must not serve"
    assert loop.tools.get("create_playbook") is None
    assert loop.tools.get("plain_probe") is not None, "a decline does not take the others down"


def test_the_wake_grant_is_namespaced_by_the_build_stamp(tmp_path):
    """[seam-1] The loop mints the wake grant per tool from the identity the
    plugin build stamped -- a namespace the plugin chose for itself would be a
    namespace it could steal -- and a tool without a stamp, or a host without
    a scheduler, gets None."""
    import time

    from raven.proactive_engine.schedulers.cron.service import CronService

    stamped = _Bindable()
    stamped.contributed_by = "plug-a"
    bare = _Bindable()
    bare.name = "bindable_bare"

    cron = CronService(tmp_path / "jobs.json", allowed_channels=None)
    loop = _loop(tmp_path, [stamped, bare])
    assert stamped.bound[0].wake_scheduler is None, "no scheduler on this host, no grant"

    loop2 = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=2),
        host=HostWiring(cron_service=cron),
        tools=ToolWiring(restrict_to_workspace=True, plugin_tools=[stamped, bare]),
    )
    granted = stamped.bound[-1].wake_scheduler
    assert granted is not None
    job = granted.schedule_wake("c1", int(time.time() * 1000) + 60_000, "look", channel="tui")
    assert job.id == "wake:plug-a:c1"
    assert bare.bound[-1].wake_scheduler is None, "no stamped identity, no namespace to grant into"
    assert loop2 is not None
