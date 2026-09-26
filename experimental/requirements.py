"""Behavior requirements shared by the Analyst's feedback, simulation reviews and planning node requirements."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Requirement(BaseModel):
    """One behavior the worker should show, with the evidence that it currently does not."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    behavior: str = Field(min_length=1)
    observed: str = Field(min_length=1)
    evidence: tuple[str, ...] = Field(min_length=1)
    expectation: Literal["new", "unmet", "met_but_rejected"]
    acceptance: str = Field(min_length=1)
    locations: tuple[str, ...] = Field(
        default=(),
        description="Observed locations from the host's supplied names; empty means unknown. This locates evidence, not the required repair scope.",
    )
    strength: Literal["must_hold", "should"] = Field(
        description="must_hold when the source or its materials state it as holding every time (a rule, a must, "
        "a prohibition, a red line); should when an occasional miss is acceptable."
    )
    recurrence: int = Field(
        default=0, ge=0, description="How many earlier rounds in history already showed this behavior failing."
    )
