"""Authoring and observation contracts shared by planning generation and binding."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ..targets import EntryPoint

TARGET = "planning.strategy"
TOOL_NAME = "curator_planning"


class PlanReader(Protocol):
    """Read this conversation's plan from any other Harness component; declare it as a keyword-only `plan`.

    Strategy factories, participant entry points and plugin component factories (tool gates, hooks, tools)
    may declare `plan`; the host supplies a reader bound to the planning strategy. Calling it returns a copy
    of the plan view (the planning strategy's view type, as JSON) that planning last produced for the
    conversation now running, or None when no planning strategy is bound or this conversation has no plan
    yet. It is read-only: changing the returned value changes nothing. Planning updates its view when it
    initializes, renders context, runs its tool or observes a completed iteration, so the plan reflects
    everything observed up to the last completed iteration; the model output being judged right now is in
    the component's own arguments (for example a StepView or the tool call), not yet in the plan.
    """

    def __call__(self) -> dict | None: ...


class PlanningObservation(BaseModel):
    """Completed iteration evidence supplied by the host, never by tool arguments.

    messages contains this turn's transcript up to this moment, including tool
    results. Tool content retains Raven's untrusted-data boundary markers and
    may differ from the raw tool return. response is the observed model proposal,
    not proof of execution.
    Inspect tool-call IDs and actual results before drawing conclusions. Native
    retries can repeat an observation; translation and revision should tolerate it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    iteration: int
    messages: list[dict[str, JsonValue]]
    response: dict[str, JsonValue] | None


class PlanningBinding(BaseModel):
    """Construct one planning strategy per session and select its Raven interaction paths.

    Each session's plan is initialized from the task on first use. Supporting
    entry points are synchronous translations. They receive detached data and
    do not own planning state. All business decisions belong to the strategy's
    async initialize/view/revise methods or their explicit delegates.
    """

    model_config = ConfigDict(extra="forbid")

    factory: EntryPoint = Field(
        description="create(state: dict[str, JsonValue]) returns a concrete instance explicitly inheriting "
        "PlanningStrategy, with concrete method annotations. Structural method matching alone is not accepted. "
        "The host supplies one mutable JSON checkpoint per session, kept across that session's turns and Harness "
        "revisions; a new session starts empty and its plan is initialized from the task. Put all durable plan "
        "state in this mapping. The factory may explicitly migrate an older representation. initialize must "
        "resume restored state; view must not change it. Factories construct inert objects. An optional keyword-only infer dependency supplies host-bounded text inference."
    )
    tool: EntryPoint | None = Field(
        default=None,
        description="translate(request: ConcreteCommand) returns ChangeT or None to read the current view. "
        "The input annotation supplies the curator_planning tool schema. Tool arguments cannot invoke the "
        "observation callback. Registration does not bypass native tool permissions.",
    )
    context: EntryPoint | None = Field(
        default=None,
        description="render(view: ViewT) returns str or None for the current model-input addendum. "
        "Raven replaces this participant's previous addendum before each model call.",
    )
    observe: EntryPoint | None = Field(
        default=None,
        description="translate(view: ViewT, observation: PlanningObservation) returns ChangeT or None. "
        "Called after an iteration with real host evidence. None requests no revision. "
        "A revision changes planning state, not the loop's accept/retry decision.",
    )
