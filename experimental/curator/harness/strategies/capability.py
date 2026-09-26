"""Capability construction and task-directed selection, including tools and skills."""

from abc import abstractmethod
from typing import Protocol


class CapabilityStrategy[ResourcesT, NeedT, SelectionT](Protocol):
    """Supply usable mechanisms and choose capabilities for a concrete need.

    Resources may be executable tools, readable skills or explicit delegates.
    Resource construction and runtime choice are separate: selection neither
    executes an action nor grants permission. Concrete representations belong to
    the implementation and its consumers, not to a particular agent loop.
    """

    @abstractmethod
    def provide(self) -> ResourcesT:
        """Construct inert resources for this strategy's lifetime.

        Consumers install and release them under their own lifecycle contracts.
        Do not run task actions, start services or mutate retained task state.
        Sharing an object with another strategy requires an explicit dependency.
        """
        ...

    @abstractmethod
    async def select(self, need: NeedT) -> SelectionT:
        """Choose capabilities and usage knowledge appropriate to this need.

        Distinguish unknown capabilities, unavailable resources, empty selection
        and delegation to an existing choice. A selection may carry explanatory
        knowledge; it never establishes that the chosen action has executed.
        """
        ...
