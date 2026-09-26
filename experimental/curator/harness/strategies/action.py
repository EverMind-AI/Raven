"""Action assessment and recovery decisions independent of loop scheduling."""

from abc import abstractmethod
from typing import Protocol


class ActionStrategy[ProposalT, FailureT, DecisionT](Protocol):
    """Own rules for accepting action, requesting revision and recovering progress.

    Decisions use explicit evidence and dependencies, including other strategies.
    Concrete decision types express what the executor should do next; this owner
    does not create a second executor or silently bypass its permissions.
    """

    @abstractmethod
    async def assess(self, proposal: ProposalT) -> DecisionT:
        """Assess a proposed action or an observed outcome and decide what follows.

        The input must distinguish intent from actual execution evidence. A
        request to retry does not undo effects that already occurred. Repeated
        assessments must account for repeated observations and bounded retries.
        """
        ...

    @abstractmethod
    async def recover(self, failure: FailureT) -> DecisionT:
        """Decide how to proceed when normal progress failed or cannot continue.

        Admit missing evidence; do not invent a successful outcome. Express
        recovery through the same decision language as assess. A consumer may
        support fewer decisions at a terminal recovery point and must reject
        unsupported effects rather than treating them as success.
        """
        ...

    async def guide(self, proposal: ProposalT) -> str | None:
        """Provide pre-decision guidance from a concrete execution situation.

        The same domain input type may describe a situation without a proposed
        action yet. Rendering may use Prompt resources. None means no guidance;
        it does not mean missing implementation. Hosts select this operation
        explicitly and must reject an inherited unsupported method.
        """
        raise NotImplementedError("pre-decision guidance is not implemented")
