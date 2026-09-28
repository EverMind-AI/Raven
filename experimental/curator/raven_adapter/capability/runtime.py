"""Install strategy-provided resources and translate capability selection into native exposure."""

from copy import deepcopy
from pathlib import Path
from typing import get_type_hints

from raven.agent.hook.participant import ParticipantHook
from raven.agent.tools.registry import admit_tool
from raven.config.raven import LocalDirConfig
from raven.contracts.participant import AgentParticipant, Intake

from ...harness import Artifact
from ...harness.strategies import CapabilityStrategy
from ..calls import translator
from ..materialize import _write
from ..strategy import BoundStrategy
from .contracts import CapabilityResources


class BoundCapability(BoundStrategy):
    def __init__(self, config, task, path, package, recorder, *, infer=None, plan=None):
        super().__init__(
            "capability", CapabilityStrategy, config, task, path, package, recorder, infer=infer, plan=plan
        )
        if get_type_hints(self.strategy.provide).get("return") is not CapabilityResources:
            raise TypeError("capability provide must declare CapabilityResources as its return type")
        self.need = translator(config.need, package, 2)
        self.expose = translator(config.expose, package, 1)
        self.render = translator(config.context, package, 1)
        self.resources = self.translate("provide", self.strategy.provide)
        if not isinstance(self.resources, CapabilityResources):
            raise TypeError("capability provide must return CapabilityResources")
        names = [admit_tool(tool).name for tool in self.resources.tools]
        if len(names) != len(set(names)):
            raise ValueError("capability resources contain duplicate tool names")
        self.skill_files = Artifact(values={}, files=self.resources.skills).files
        self.skill_root = None

    def stage_skills(self, root, config):
        if not self.skill_files:
            return
        self.skill_root = root / "capability-skills"
        for name, content in self.skill_files.items():
            _write(self.skill_root / name, content.encode())
        config.skill_forge.local_dirs.append(LocalDirConfig(path=str(self.skill_root), name="curator-capability"))

    def install(self, runtime):
        for tool in self.resources.tools:
            if runtime.loop.tools.get(tool.name) is not None:
                raise ValueError(f"capability tool would replace an existing tool: {tool.name}")
            runtime.loop.tools.register(tool)
        if self.skill_root:
            roots = [
                Path(row["path"]).parent.resolve()
                for row in runtime.loop.context.skills.list_skills(filter_unavailable=False)
            ]
            for name in self.skill_files:
                path = (self.skill_root / name).resolve()
                if not any(path.is_relative_to(root) for root in roots):
                    raise ValueError(f"capability skill is not discoverable: {name}")
        self.recorder.add(
            "capability.resources", tools=[tool.name for tool in self.resources.tools], skills=list(self.skill_files)
        )

    async def select(self, need):
        return await self.call("select", need, source="selection")

    def facts(self):
        return {
            **super().facts(),
            "tools": [admit_tool(tool).schema for tool in self.resources.tools],
            "skills": deepcopy(self.skill_files),
        }

    def hook(self):
        owner = self

        class CapabilityParticipant(AgentParticipant):
            def __init__(self):
                self.selection = None
                self.has_selection = False

            async def choose(self, offered, step):
                self.has_selection = False
                need = owner.translate("need", owner.need, offered, step, output=owner.types["select"][0][0])
                self.selection = await owner.select(need)
                self.has_selection = True
                return self.selection

            @owner.callback
            async def select_tools(self, offered, step):
                if owner.need is None:
                    return None
                result = await self.choose(offered, step)
                if owner.expose is None:
                    return None
                names = owner.translate("expose", owner.expose, result, output=list[str] | None)
                if names is None:
                    return None
                available = {row["function"]["name"]: row for row in offered}
                if len(names) != len(set(names)) or set(names) - available.keys():
                    raise ValueError("capability selection must contain unique offered tool names")
                return [deepcopy(available[name]) for name in names]

            @owner.callback
            async def system_addendum(self, step):
                if owner.render is None:
                    return None
                try:
                    if not self.has_selection:
                        await self.choose(list(step.tools), step)
                    content = owner.translate("context", owner.render, self.selection, output=str | None)
                    return Intake(content) if content is not None else None
                finally:
                    self.has_selection = False

        return ParticipantHook("curator-capability", CapabilityParticipant, rolls_back=False)
