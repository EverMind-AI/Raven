"""Action example requiring execution evidence before accepting a final claim."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from experimental.curator.harness.strategies import ActionStrategy

REQUIRE_EVIDENCE = True


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    has_evidence: bool
    pending_tools: bool


class Failure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["approve", "retry", "finish"]
    text: str = ""


class Action(ActionStrategy[Proposal, Failure, Decision]):
    def __init__(self, state):
        self.state = state

    async def assess(self, proposal: Proposal) -> Decision:
        if REQUIRE_EVIDENCE and not proposal.has_evidence and not proposal.pending_tools:
            self.state["retries"] = self.state.get("retries", 0) + 1
            return Decision(kind="retry", text="Call evidence_probe before finishing.")
        return Decision(kind="approve")

    async def recover(self, failure: Failure) -> Decision:
        self.state["recoveries"] = self.state.get("recoveries", 0) + 1
        return Decision(kind="finish", text="Evidence was not obtained; the task remains incomplete.")


def create(state, task):
    return Action(state)
