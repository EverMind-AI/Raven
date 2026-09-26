"""Complete shared observation knowledge from native definitions, without copying their schemas."""

from pathlib import Path

from raven.contracts.llm_provider import (
    CallRecord,
    ErrorClassification,
    LLMResponse,
    RunMeta,
    ToolCallRequest,
    TruncationInfo,
)
from raven.contracts.participant import StepView
from raven.providers.tool_calls import openai_tool_call

from ..observe import plain
from ..planning.contracts import PlanningObservation

_REFERENCE = Path(__file__).resolve().parents[1] / "reference/observations.md"
_DEPENDENCIES = {
    StepView: (LLMResponse, openai_tool_call, _REFERENCE),
    PlanningObservation: (LLMResponse, plain, openai_tool_call, _REFERENCE),
    LLMResponse: (ToolCallRequest, ErrorClassification, CallRecord),
    ToolCallRequest: (RunMeta,),
    RunMeta: (TruncationInfo,),
}


def complete(items):
    """Include the known runtime meaning of opaque fields as well as their nested types."""
    found = {}

    def include(item):
        if item in found:
            return
        found[item] = None
        for dependency in _DEPENDENCIES.get(item, ()):
            include(dependency)

    for item in items:
        include(item)
    return tuple(found)
