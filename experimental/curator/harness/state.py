"""Worker state descriptions shared by contracts and generation plans."""

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class StateUse(BaseModel):
    """Describe state ownership and lifetime without creating a state store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource: str = Field(min_length=1)
    scope: Literal["turn", "session", "task", "generation", "worker"]
    access: str = Field(min_length=1)
    lifecycle: str = Field(min_length=1)


class Task(BaseModel):
    """Host-owned identity and initial goal for a worker's current task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
