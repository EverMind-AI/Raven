"""Loadable planning examples; their representations and policies are not worker defaults."""

from graphlib import TopologicalSorter

from pydantic import BaseModel, ConfigDict, Field, model_validator

from experimental.curator.harness.strategies import PlanningStrategy


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Item(Payload):
    name: str = Field(min_length=1)
    done: bool = False


class ChecklistView(Payload):
    items: list[Item]

    @model_validator(mode="after")
    def distinct_items(self):
        if len({item.name for item in self.items}) != len(self.items):
            raise ValueError("item names must be unique")
        return self


class StepResult(Payload):
    step: str
    succeeded: bool


type ChecklistChange = ChecklistView | StepResult


class ChecklistPlanning(PlanningStrategy[ChecklistView, ChecklistChange]):
    def __init__(self):
        self._plan: ChecklistView | None = None

    async def initialize(self, task: str) -> ChecklistView:
        if self._plan is None:
            self._plan = ChecklistView(items=[Item(name=task)])
        return await self.view()

    async def view(self) -> ChecklistView:
        if self._plan is None:
            raise RuntimeError("plan is not initialized")
        return self._plan.model_copy(deep=True)

    async def revise(self, change: ChecklistChange) -> ChecklistView:
        plan = await self.view()
        if isinstance(change, ChecklistView):
            plan = change.model_copy(deep=True)
        elif isinstance(change, StepResult):
            item = next((item for item in plan.items if item.name == change.step), None)
            if item is None:
                raise ValueError(f"unknown step: {change.step}")
            item.done = change.succeeded
        else:
            raise TypeError("unsupported checklist change")
        self._plan = ChecklistView.model_validate(plan.model_dump())
        return await self.view()


class Node(Payload):
    after: list[str] = Field(default_factory=list)
    done: bool = False


class GraphView(Payload):
    nodes: dict[str, Node]

    @model_validator(mode="after")
    def consistent_dependencies(self):
        for name, node in self.nodes.items():
            if not name or set(node.after) - self.nodes.keys():
                raise ValueError("dependencies must name existing nodes")
            if node.done and any(not self.nodes[dependency].done for dependency in node.after):
                raise ValueError("completed nodes require completed dependencies")
        tuple(TopologicalSorter({name: node.after for name, node in self.nodes.items()}).static_order())
        return self


type GraphChange = GraphView | StepResult


class GraphPlanning(PlanningStrategy[GraphView, GraphChange]):
    def __init__(self):
        self._plan: GraphView | None = None

    async def initialize(self, task: str) -> GraphView:
        if self._plan is None:
            self._plan = GraphView(nodes={task: Node()})
        return await self.view()

    async def view(self) -> GraphView:
        if self._plan is None:
            raise RuntimeError("plan is not initialized")
        return self._plan.model_copy(deep=True)

    async def revise(self, change: GraphChange) -> GraphView:
        plan = await self.view()
        if isinstance(change, GraphView):
            plan = change.model_copy(deep=True)
        elif isinstance(change, StepResult):
            if change.step not in plan.nodes:
                raise ValueError(f"unknown step: {change.step}")
            plan.nodes[change.step].done = change.succeeded
            if not change.succeeded:
                order = TopologicalSorter({name: node.after for name, node in plan.nodes.items()}).static_order()
                for name in order:
                    if any(not plan.nodes[dependency].done for dependency in plan.nodes[name].after):
                        plan.nodes[name].done = False
        else:
            raise TypeError("unsupported graph change")
        self._plan = GraphView.model_validate(plan.model_dump())
        return await self.view()


class DelegatingPlanning(PlanningStrategy[ChecklistView, ChecklistChange]):
    def __init__(self, owner: PlanningStrategy[ChecklistView, ChecklistChange]):
        self.owner = owner

    async def initialize(self, task: str) -> ChecklistView:
        return await self.owner.initialize(task)

    async def view(self) -> ChecklistView:
        return await self.owner.view()

    async def revise(self, change: ChecklistChange) -> ChecklistView:
        return await self.owner.revise(change)
