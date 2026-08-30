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
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
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


def _loop(tmp_path, plugin_tools) -> AgentLoop:
    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=2),
        host=HostWiring(),
        tools=ToolWiring(restrict_to_workspace=True, plugin_tools=plugin_tools),
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
