"""Repair a draft or explicitly reopen its design or target selection."""

from pydantic import BaseModel, ConfigDict, Field

from ...harness import Candidate
from ...harness.artifact import Selection
from ...harness.declaration import schema_for
from ..context.collect import Context
from ..context.render import tool
from . import implement

NAME = "revise_plan"


class Revision(BaseModel):
    """The concrete reason to discard a design and investigate again."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)


def reopen() -> dict:
    return tool(
        NAME, "Reopen design for the current selection. Discards the current design and draft.", schema_for(Revision)
    )


def materials(context: Context, selection: Selection, candidate: Candidate, validation) -> dict:
    """What repair adds: implementation's data plus the submitted candidate and its validation."""
    return {
        **implement.materials(context, selection, candidate.plan),
        "candidate": candidate.artifact.model_dump(mode="json"),
        "validation_errors": validation.errors,
        "validation_observations": validation.observations,
    }
