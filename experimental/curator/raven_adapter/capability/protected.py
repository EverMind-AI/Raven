"""Preserve native root delegation while generated strategies select other capabilities."""

from copy import deepcopy
from dataclasses import replace

from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.agent.subagent.spawn_tool import SpawnTool
from raven.agent.tools.create_playbook import CreatePlaybookTool
from raven.agent.tools.load_playbook import LoadPlaybookTool


class DelegationAction:
    """Guard the final model request and native pre-dispatch judgement seam."""

    def __init__(self, loop, rows):
        self.loop, self.action = loop, loop.harness.action
        self.rows = {row.name: row.model_copy(deep=True) for row in rows}
        expected = {
            "spawn": SpawnTool,
            "run_subagent_dag": SubAgentDagTool,
            "load_playbook": LoadPlaybookTool,
            "create_playbook": CreatePlaybookTool,
        }
        if disabled := set(expected) & loop._disabled_tools:
            raise ValueError(f"root delegation tools cannot be disabled: {sorted(disabled)}")
        self.tools = {name: loop.tools.get(name) for name in expected}
        for name, tool in self.tools.items():
            if type(tool) is not expected[name]:
                raise ValueError(f"root delegation requires the native {name} implementation")
        self.check()

    def check(self):
        for name, tool in self.tools.items():
            if self.loop.tools.get(name) is not tool:
                raise ValueError(f"protected delegation tool was replaced: {name}")
        for name, config in self.rows.items():
            row = self.loop.subagents.registry.get(name)
            if row is None or row.config != config:
                raise ValueError(f"protected child registration changed: {name}")

    async def decide(self, request):
        self.check()
        native = {
            row["function"]["name"]: row
            for row in self.loop.tools.get_definitions()
            if row["function"]["name"] in self.tools
        }
        selected = [row for row in request.tools or () if row["function"]["name"] not in self.tools]
        return await self.action.decide(replace(request, tools=[*selected, *deepcopy(list(native.values()))]))

    def ask_judge(self, *args, **kwargs):
        self.check()
        return self.action.ask_judge(*args, **kwargs)

    async def ask_review(self, *args, **kwargs):
        return await self.action.ask_review(*args, **kwargs)

    async def ask_salvage(self, *args, **kwargs):
        return await self.action.ask_salvage(*args, **kwargs)


def protect_delegation(loop, rows):
    loop.harness = replace(loop.harness, action=DelegationAction(loop, rows))
