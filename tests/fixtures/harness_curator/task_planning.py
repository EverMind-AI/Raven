"""Example generated planning code with explicit task checkpoint ownership."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from experimental.curator.harness.strategies import PlanningStrategy

GUARD_COMPLETION = False


class View(BaseModel):
    items: dict[str, bool]
    guarded: bool


class Complete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["complete"] = "complete"
    item: str


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["evidence"] = "evidence"
    item: str
    call_id: str
    succeeded: bool


type Change = Complete | Evidence


class Planning(PlanningStrategy[View, Change]):
    def __init__(self, state: dict[str, JsonValue]):
        self.state = state

    async def initialize(self, task: str) -> View:
        if not self.state:
            self.state.update(items={task: False}, seen=[], verified=[])
        return await self.view()

    async def view(self) -> View:
        return View(items=dict(self.state["items"]), guarded=GUARD_COMPLETION)

    async def revise(self, change: Change) -> View:
        if change.item not in self.state["items"]:
            raise ValueError("unknown planning item")
        if isinstance(change, Complete):
            if GUARD_COMPLETION and change.item not in self.state["verified"]:
                raise ValueError("completion requires verified execution evidence")
            self.state["items"][change.item] = True
        elif change.call_id not in self.state["seen"]:
            self.state["seen"].append(change.call_id)
            self.state["items"][change.item] = change.succeeded
            if change.succeeded:
                self.state["verified"].append(change.item)
        return await self.view()


def create(state: dict[str, JsonValue]) -> Planning:
    return Planning(state)
