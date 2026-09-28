"""A proposal guard and inert test tools for checking refusal and permitted execution."""

from typing import Literal

from pydantic import BaseModel

from experimental.curator.harness.strategies import ActionStrategy
from experimental.curator.raven_adapter.targets.action import ReviewResult
from raven.contracts.tool import Tool


class Proposal(BaseModel):
    calls: list[str]


class Decision(BaseModel):
    verdict: Literal["accept", "resample"]


class Guard(ActionStrategy[Proposal, Proposal, Decision]):
    async def assess(self, proposal: Proposal) -> Decision:
        return Decision(verdict="resample" if "blocked_probe" in proposal.calls else "accept")

    async def recover(self, failure: Proposal) -> Decision:
        return Decision(verdict="accept")


def create(state, task):
    return Guard()


def proposal(step) -> Proposal:
    calls = step.response.tool_calls if step.response is not None else []
    return Proposal(calls=[call.name for call in calls])


def decision(value: Decision) -> ReviewResult:
    return ReviewResult(
        verdict=value.verdict,
        inject=[{"role": "user", "content": "Use safe_probe instead."}] if value.verdict == "resample" else None,
    )


class Probe(Tool):
    description = "Record that this permitted test tool actually ran."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    async def execute(self, **kwargs):
        return self.name + "_EXECUTED"


def blocked(context):
    return Probe("blocked_probe")


def safe(context):
    return Probe("safe_probe")
