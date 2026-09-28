"""Capability example supplying a tool, its skill and task-directed exposure."""

from pydantic import BaseModel, ConfigDict

from experimental.curator.harness.strategies import CapabilityStrategy
from experimental.curator.raven_adapter.capability.contracts import CapabilityResources
from raven.contracts.tool import Tool


class Need(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offered: list[str]


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    names: list[str]


class Probe(Tool):
    name = "evidence_probe"
    description = "Obtain the task's local evidence."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs):
        return "FACT:cobalt"


class Capability(CapabilityStrategy[CapabilityResources, Need, Selection]):
    def __init__(self, state):
        self.state = state

    def provide(self) -> CapabilityResources:
        return CapabilityResources(
            tools=(Probe(),),
            skills={
                "evidence/SKILL.md": "---\nname: evidence\ndescription: Read execution evidence\nalways: true\n---\nCAPABILITY_SKILL_RUNTIME: call evidence_probe before making factual claims."
            },
        )

    async def select(self, need: Need) -> Selection:
        self.state["selections"] = self.state.get("selections", 0) + 1
        return Selection(names=[name for name in need.offered if name in {"evidence_probe", "curator_planning"}])


def create(state, task):
    return Capability(state)
