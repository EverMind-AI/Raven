"""Durable two-level progress with budgets derived from each generation's own checkpoint."""

from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..generation.run import Generated, GenerationPausedError, Limits
from ..generation.state import GenerationState
from ..harness import Validation

COUNTERS = ("calls", "queries", "checks", "repairs")


class Progress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_id: str
    directory: str
    feedback: object = None
    model: str | None = None
    root: Generated | None = None
    children: dict[str, Generated] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)
    scopes: dict[str, str] = Field(default_factory=dict)
    repair: Validation | None = None
    error: str | None = None

    def consumed(self):
        return {
            str(path.parents[1].relative_to(self.directory)): GenerationState.model_validate_json(path.read_text())
            for path in Path(self.directory).glob("*/progress/generation.json")
        }

    def totals(self):
        return {name: sum(getattr(state, name) for state in self.consumed().values()) for name in COUNTERS}

    def limits_for(self, scope, limits):
        spent = self.totals()
        own = self.consumed().get(scope)
        remaining = {name: getattr(limits, f"max_{name}") - spent[name] for name in COUNTERS}
        if any(value < 0 for value in remaining.values()):
            raise ValueError("limits cannot be below the already consumed composite budget")
        if remaining["calls"] == 0:
            raise self.paused(own.stage if own else "select")
        return Limits(
            **{
                **asdict(limits),
                **{f"max_{name}": remaining[name] + (getattr(own, name) if own else 0) for name in COUNTERS},
            }
        )

    def paused(self, stage="select"):
        return GenerationPausedError(GenerationState(input_id=self.input_id, stage=stage, **self.totals()))
