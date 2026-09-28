"""Memory translations from host evidence to semantic retrieval and retention."""

from pydantic import Field, model_validator

from ..strategy import TaskBinding
from ..targets import EntryPoint

TARGET = "memory.strategy"


class MemoryBinding(TaskBinding):
    """Optional read and write paths; disabled paths have no implicit fallback."""

    composition: bool = Field(
        default=False,
        description="Enable compose(ContextRequest)->ContextView and compact(ContextRequest)->ContextView during native "
        "context assembly. The adapter preserves the native system and current user message, checks the "
        "host token estimate, and invokes compact if compose exceeds the allowance. This does not replace "
        "MemoryModule.shrink during iterations. Cannot combine with this binding's query/context addendum lane.",
    )

    query: EntryPoint | None = Field(
        default=None,
        description="translate(step: StepView) -> QueryT | None before a model call. None skips retrieval. "
        "StepView is raven.contracts.participant.StepView; it is a detached host observation.",
    )
    context: EntryPoint | None = Field(
        default=None,
        description="render(context: ContextT) -> str | None, paired with query. Replaces this participant's prior addendum.",
    )
    retain: EntryPoint | None = Field(
        default=None,
        description="translate(step: StepView) -> RecordT | None after an iteration. None means no useful evidence. "
        "Response is a proposal; use actual transcript results and IDs, tolerate repeated native observations. "
        "All translations are synchronous and cannot mutate memory state.",
    )

    @model_validator(mode="after")
    def retrieval_pair(self):
        if (self.query is None) != (self.context is None):
            raise ValueError("memory query and context must be supplied together")
        if self.composition and self.query is not None:
            raise ValueError("composition and the memory query/context addendum cannot both own context")
        return self
