"""The four harness strategy roles the agent loop delegates to.

Factory-loop tier: Versioned with the factory loop, not frozen for every
loop — a replacement loop may ship its own strategy vocabulary and version
this paper with it. Only the ``contract`` tier is a cross-loop promise.

The four roles name the strategy *decisions* a turn makes, not four layers and
not the loop itself. Memory assembles the window the model sees and decides how
a transcript is made to fit again mid-turn, Planning may prepare turn guidance,
Capability picks the tool definitions one iteration exposes, and Action produces
one usable model response and judges a call against the dispatch's Charter.
Everything else the turn does -- iteration accounting, hook phases, tool
execution and approval, persistence and event order -- stays with the L2 shell,
which is what makes these four replaceable at all.

The in-loop recoveries are split along that line rather than sitting on one
side of it. Memory answers *what to give up* (see ``shrink``); the shell owns
the *mechanism* -- noticing the refusal, re-entering the iteration, and
bounding how many times a turn may pay for it. A replacement that answers
``shrink`` with an unchanged transcript therefore does not merely decline a
policy: the shell's ``changed`` test never fires, and an overflow the loop
could have recovered from ends the turn.

_Avoid_: reading ``ActionModule`` as "the loop". The shell owns retries, tool
execution and events; Action owns one model decision and the Charter's verdict
on a call. And reading Memory as the memory engine: Memory here is the turn's
*window*, which the context engine owns; long-term recall is a different organ
behind its own paper.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from raven.contracts.assembled import AssembledContext, TokenBudget
    from raven.contracts.context import TurnContext
    from raven.contracts.llm_provider import LLMProvider, LLMResponse

__tier__ = "factory_loop"


class WindowPressure(str, Enum):
    """Why the window is being asked to get smaller: the shell's side of the seam.

    The two ``before`` pressures are asked every iteration; the three
    ``refused`` ones only when the endpoint answered a call with the matching
    classification, and a ``changed`` answer to one of those is the shell's
    cue to retry the iteration.
    """

    PROACTIVE = "proactive"
    """Before a call: the last billed context size crossed the trigger line."""
    STANDING = "standing"
    """Before a call: the picture window and its byte budget, applied in place."""
    OVERFLOW = "overflow"
    """After a call: the endpoint refused the request as too long."""
    TOOL_IMAGES_REFUSED = "tool_images_refused"
    """After a call: this endpoint takes no picture inside a tool result."""
    IMAGES_TOO_LARGE = "images_too_large"
    """After a call: the request's pictures were refused for their size."""


@dataclass
class WindowState:
    """One turn's window bookkeeping: the readings and retry budgets every
    shrink draws on.

    Mutable, and held by the shell rather than the role: the counts move every
    iteration and belong to the turn, while Memory is a generation's role and
    outlives it. The shell creates one per turn and hands the same object to
    every ``shrink`` call; Memory reads and advances it and never keeps it.

    ``image_window`` has no default on purpose: 0 is a real value meaning "no
    picture stays", so a state built without saying how many image-bearing
    messages keep theirs would silently withdraw them all.
    """

    image_window: int
    """Image-bearing messages that keep their pictures; a refusal closes it a notch."""
    last_context_used: int = 0
    """Billed size of the last successful call; 0 until the first usage report
    and after any compaction, so the proactive trigger fires on fresh data only."""
    compress_retries: int = 0
    """Elisions and head summaries paid for this turn, against one shared cap."""
    head_summary_failures: int = 0
    """Summaries paid for that freed nothing; read only to say why the elisions
    that follow are all the turn has left."""
    reactive_summary_tried: bool = False
    """The overflow-path summary is a last resort, once per turn."""
    image_budget: int | None = None
    """Bytes the standing pictures may occupy on the wire, or None for no standing pass."""
    image_demote_retries: int = 0
    image_strip_retries: int = 0


@dataclass(frozen=True)
class ShrinkResult:
    """What Memory hands back from one ``shrink``: the list to go on with, and
    whether it is smaller. ``changed`` false means the role had no move left
    for this pressure, and ``messages`` is then the list it was given."""

    messages: list[dict[str, Any]]
    changed: bool


@runtime_checkable
class MemoryModule(Protocol):
    """The turn's window: what this turn's model calls get to see.

    Three of the five members are the context engine's own turn-facing
    surface, which the shipped ``ContextAssembler`` answers by construction.
    The other two were the shell's and are seated here because the decisions
    are the window's: which slice of history is even a candidate turns on
    whether the engine archives for itself, and how much of the window a
    prompt may occupy is that window's own arithmetic. So an engine alone
    does not satisfy this protocol -- the default implementation wraps one
    and adds those two.

    The engine still holds the Curator's working state and each segment
    builder's provider binding, so a generation's Memory *is* its context
    engine -- swapping it is a new generation, not a per-turn choice.
    """

    @property
    def owns_compaction(self) -> bool:
        """True when the engine compacts history itself, so the loop does not
        call the consolidator for token pressure."""
        ...

    def candidate_messages(self, session: Any) -> list[dict[str, Any]]:
        """The history this turn may draw on, which only Memory can decide:
        an engine that archives for itself wants the whole append-only log,
        one that does not wants the post-consolidation slice."""
        ...

    def token_budget(self, selected_skills: list[Any] | None = None) -> "TokenBudget":
        """How much of the window this turn's prompt may occupy."""
        ...

    async def assemble(
        self,
        session_key: str,
        session_messages: list[dict[str, Any]],
        budget: "TokenBudget",
        *,
        turn: "TurnContext",
    ) -> "AssembledContext":
        """Build the exact message list this turn's first model call receives."""
        ...

    async def shrink(
        self,
        messages: list[dict[str, Any]],
        *,
        pressure: "WindowPressure",
        state: "WindowState",
        model: str | None,
    ) -> "ShrinkResult":
        """Make the window smaller under ``pressure``, if this role has a move
        left for it; otherwise hand the list back unchanged.

        The window's mid-turn decisions, in one seat: what to give up when the
        transcript nears the line, when a summary is worth paying for, which
        pictures stay. The shell keeps the mechanism -- it re-enters the
        iteration on a ``changed`` reactive answer, so the hooks and the standing
        passes see a retry exactly as they saw the attempt -- and this role keeps
        the policy, which is the half a generation may replace.
        """
        ...

    async def after_turn(self, session_key: str, outcome: dict[str, Any]) -> None:
        """Post-turn hook: the engine updates its manifest or archives here."""
        ...


@dataclass(frozen=True)
class PlanningRequest:
    """What Planning is told about the turn about to start."""

    task: str
    session_key: str
    messages: list[dict[str, Any]]


@dataclass(frozen=True)
class PlanningResult:
    """What Planning hands back: the message list the iterations run on.

    A pass-through result returns ``request.messages`` unchanged, which is
    what the default does and what Raven has always done -- planning is the
    model's own, in its own text, and the position ahead of the turn was
    measured to be the wrong one for it (the retired playbook funnel judged a
    message with no history and replaced the whole turn on a hit).
    """

    messages: list[dict[str, Any]]


@runtime_checkable
class PlanningModule(Protocol):
    """Optional turn guidance, ahead of the iterations."""

    async def prepare(self, request: PlanningRequest) -> PlanningResult: ...


@dataclass(frozen=True)
class CapabilityRequest:
    """What Capability is told about the iteration about to run."""

    messages: list[dict[str, Any]]
    iteration: int


@dataclass(frozen=True)
class CapabilitySelection:
    """The tool definitions this iteration shows the model.

    A *view*, never a permission: Raven's progressive disclosure rests on a
    tool absent from the array still being reachable by name, so narrowing
    this narrows what the model is shown and nothing else. The enforcement
    point is ``ToolRegistry.execute``.
    """

    tools: list[dict[str, Any]]


@runtime_checkable
class CapabilityModule(Protocol):
    """Which tool definitions one iteration exposes."""

    async def select(self, request: CapabilityRequest) -> CapabilitySelection: ...


@dataclass(frozen=True)
class ActionRequest:
    """Everything one model decision needs.

    ``stream_call`` is the loop's own streaming path, handed in rather than
    reimplemented: it fans deltas out to the turn's sinks, which is the
    shell's business. An implementation chooses between it and the provider's
    retry ladder the way the loop used to, and may call either more than once
    -- ``decide`` is one *decision*, not one HTTP request, so best-of-n or a
    critic pass is expressible without the shell knowing.
    """

    provider: "LLMProvider"
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    model: str
    fallback_models: list[str]
    stream_call: Any
    on_token_delta: Any | None = None
    on_reasoning_delta: Any | None = None
    generation_overrides: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ActionModule(Protocol):
    """What the agent does next: decide it, and judge it before it runs.

    Two methods because there are two moments and two callers. ``decide`` is
    asked by the loop and answers with a response. ``judge`` is asked by
    ``ToolRegistry.execute`` once per call the response proposed, the way the
    permission gate beside it is asked -- so the module supplies the judgement
    and the shell still decides what to do with it. A role that answers ``[]``
    withholds nothing, which is what having no playbook already means: the
    contract narrows and never grants.
    """

    async def decide(self, request: ActionRequest) -> "LLMResponse": ...

    def judge(
        self,
        name: str,
        params: Mapping[str, Any],
        prior: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> list[str]:
        """Why this call must not run, or an empty list.

        Sentences, not exceptions: a refusal reaches the model as the call's
        own result, so its next attempt can be right. Synchronous and cheap by
        contract -- this runs before every dispatch and ahead of the permission
        gate, where anything that blocked on the network would cost the turn
        rather than the call.
        """
        ...


@dataclass(frozen=True)
class HarnessModules:
    """The four roles one generation runs on, bound together.

    Frozen because a generation's strategy set is decided when it is
    assembled: a mid-turn swap would let two model calls of one turn run on
    different strategies, and the tool array is the prompt-cache prefix.
    """

    memory: MemoryModule
    planning: PlanningModule
    capability: CapabilityModule
    action: ActionModule


__all__ = [
    "ActionModule",
    "ActionRequest",
    "CapabilityModule",
    "CapabilityRequest",
    "CapabilitySelection",
    "HarnessModules",
    "MemoryModule",
    "PlanningModule",
    "PlanningRequest",
    "PlanningResult",
    "ShrinkResult",
    "WindowPressure",
    "WindowState",
]
