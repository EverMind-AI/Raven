"""Example generated carriers delegating to a semantic planning strategy."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from experimental.curator.raven_adapter.planning.contracts import PlanningObservation
from raven.contracts.tool import Tool

from .task_planning import Change, Complete, Evidence, View


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["view", "complete"]
    item: str | None = None


def command(request: Command) -> Change | None:
    """View the task plan or request completion of a named item."""
    return None if request.operation == "view" else Complete(item=request.item)


def context(view: View) -> str:
    return "CURRENT_TASK_PLAN: " + view.model_dump_json()


def observe(view: View, observation: PlanningObservation) -> Change | None:
    for message in reversed(observation.messages):
        content = message.get("content")
        if message.get("role") == "tool" and message.get("name") == "planning_probe" and isinstance(content, str):
            evidence = [line for line in content.splitlines() if line.startswith("EVIDENCE:")]
            if len(evidence) == 1:
                return Evidence(
                    item=next(iter(view.items)),
                    call_id=message["tool_call_id"],
                    succeeded=evidence[0] == "EVIDENCE:passed",
                )
    return None


class Probe(Tool):
    name = "planning_probe"
    description = "Return an actual controlled verification result."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "EVIDENCE:failed"


def probe(context):
    return Probe()
