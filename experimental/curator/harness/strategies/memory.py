"""Information retrieval and retention implemented by a task's memory owner."""

from abc import abstractmethod
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class ContextRequest(BaseModel):
    """Candidate model messages, a token allowance and protected message indices.

    The host owns the token allowance and its token estimator. Protected
    messages must survive unchanged and in order. Projection does not delete
    the underlying task history or retained memory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    messages: list[dict[str, JsonValue]]
    budget: int = Field(ge=0)
    required: list[int]

    @model_validator(mode="after")
    def valid_indices(self):
        if self.required != sorted(set(self.required)) or any(i < 0 or i >= len(self.messages) for i in self.required):
            raise ValueError("required indices must be unique, ordered and within messages")
        return self


class ContextView(BaseModel):
    """A concrete message projection whose budget and protected content the host checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    messages: list[dict[str, JsonValue]]


class MemoryStrategy[QueryT, ContextT, RecordT, ReceiptT](Protocol):
    """Own the selection, provenance and retention rules for task information.

    Concrete types express relevance, evidence and retention intent. Dependencies
    and storage are explicit constructor arguments. A retrieved statement is not
    automatically trusted, and a retention receipt is not proof of task success.
    """

    @abstractmethod
    async def recall(self, query: QueryT) -> ContextT:
        """Return detached relevant information without changing retained state.

        Empty results are valid. Unavailable stores and invalid queries are
        errors, not fabricated empty successes. Preserve source and uncertainty.
        """
        ...

    @abstractmethod
    async def retain(self, record: RecordT) -> ReceiptT:
        """Evaluate what to remember, update owned state and report the outcome.

        The concrete request may insert, correct or forget information. A valid
        no-op is distinct from failure. Repeated evidence must not accidentally
        duplicate facts; invalid requests preserve the preceding valid state.
        """
        ...

    async def compose(self, request: ContextRequest) -> ContextView:
        """Select and organize context within the host allowance.

        Return a projection, not a rewritten persistent archive. Hosts may
        invoke compact when this projection still exceeds the allowance.
        This operation is explicitly selected by the context binding.
        """
        raise NotImplementedError("context composition is not implemented")

    async def compact(self, request: ContextRequest) -> ContextView:
        """Reduce a context projection while preserving required messages and evidence.

        Summarization can use explicitly supplied inference; deterministic
        pruning is also valid. Fail when requirements cannot fit, rather than
        dropping protected content or claiming success without reducing size.
        """
        raise NotImplementedError("context compaction is not implemented")
