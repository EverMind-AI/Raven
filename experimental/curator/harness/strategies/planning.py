"""Planning operations implemented by generated strategies and their delegates."""

from abc import abstractmethod
from typing import Protocol


class PlanningStrategy[ViewT, ChangeT](Protocol):
    """Own a task's planning behavior through initialization, reading and revision.

    Concrete implementations define their view and change types, representation,
    decision rules and explicit dependencies. They may delegate to an existing
    component; callers still use one owner of the planning rules and state.

    A factory binds the task and its resources. An instance or its delegated
    state can outlive a turn. This protocol neither chooses storage nor maps
    operations to tools, prompts, skills or host callbacks.
    """

    @abstractmethod
    async def initialize(self, task: str) -> ViewT:
        """Create or resume the bound task's plan and return its current view.

        Existing progress survives repeated calls. The task text supplies the
        initial objective and constraints, not identity or a reset command.
        Subsequent requirement changes go through revise.
        """
        ...

    @abstractmethod
    async def view(self) -> ViewT:
        """Read a detached view without changing the plan.

        An uninitialized plan is an error. An empty plan is a valid concrete
        view; it must not be confused with missing initialization or None.
        """
        ...

    @abstractmethod
    async def revise(self, change: ChangeT) -> ViewT:
        """Evaluate a typed request and return the resulting current view.

        A valid request may leave the plan unchanged. Invalid or unsupported
        requests and dependency failures remain errors; they must not replace
        the last valid state or masquerade as an unchanged success. Revising
        planning state does not execute the task's external actions or change
        the Harness implementation.
        """
        ...
