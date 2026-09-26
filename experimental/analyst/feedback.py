"""The feedback contract: a decision plus behavior requirements stated in observable terms."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..requirements import Requirement


class Feedback(BaseModel):
    """What the round's judgements amount to; only `curate` reaches the Curator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["curate", "continue", "supplement", "clarify", "stop"]
    reason: str = Field(min_length=1)
    requirements: tuple[Requirement, ...] = ()
    filtered: tuple[str, ...] = ()
    task_updates: tuple[str, ...] = ()

    @model_validator(mode="after")
    def requirements_match_decision(self) -> "Feedback":
        if (self.decision == "curate") != bool(self.requirements):
            raise ValueError("curate requires at least one requirement; other decisions carry none")
        return self
