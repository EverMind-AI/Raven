"""Action decisions translated into host-supported review and terminal recovery."""

from pydantic import Field, model_validator

from ..strategy import TaskBinding
from ..targets import EntryPoint

TARGET = "action.strategy"


class ActionBinding(TaskBinding):
    """Semantic decisions and native scheduling remain distinct responsibilities."""

    guidance: EntryPoint | None = Field(
        default=None,
        description="translate(step: StepView) -> ProposalT | None before each model decision. None skips guidance. "
        "Otherwise calls async strategy.guide(proposal) -> str | None and supplies its text as this "
        "participant's replaceable addendum. A selected guide must be implemented; no extra model call "
        "is needed for ordinary template rendering.",
    )

    proposal: EntryPoint | None = Field(
        default=None,
        description="translate(step: StepView) -> ProposalT | None at execute_tools and after_iteration. "
        "None skips assessment. Distinguish proposed calls from results using phase, not response text.",
    )
    decision: EntryPoint | None = Field(
        default=None,
        description="translate(decision: DecisionT) -> ReviewResult | None, paired with proposal. "
        "ReviewResult is experimental.curator.raven_adapter.targets.action.ReviewResult. "
        "Native composition and retry budgets apply; rollback never undoes external tool effects. "
        "reason is diagnostic; inject carries correction to the model.",
    )
    failure: EntryPoint | None = Field(
        default=None,
        description="translate(step: StepView) -> FailureT | None at answerless. This is terminal recovery, "
        "not permission to restart the loop; native synthesis or rerun may make this path unreachable.",
    )
    reply: EntryPoint | None = Field(
        default=None,
        description="translate(decision: DecisionT) -> str | None, paired with failure. Convert a supported "
        "terminal decision to truthful reply text, or None to defer. Reject unsupported recovery effects. "
        "All translations are synchronous and cannot mutate strategy state.",
    )

    @model_validator(mode="after")
    def translation_pairs(self):
        if (self.proposal is None) != (self.decision is None):
            raise ValueError("action proposal and decision must be supplied together")
        if (self.failure is None) != (self.reply is None):
            raise ValueError("action failure and reply must be supplied together")
        return self
