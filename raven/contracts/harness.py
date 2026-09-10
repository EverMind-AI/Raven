"""The four harness strategy roles the agent loop delegates to.

Factory-loop tier: Versioned with the factory loop, not frozen for every
loop — a replacement loop may ship its own strategy vocabulary and version
this paper with it. Only the ``contract`` tier is a cross-loop promise.

The four roles name the strategy *decisions* a turn makes, not four layers
and not the loop itself. Memory assembles the window the model sees, Planning
may prepare turn guidance, Capability picks the tool definitions one iteration
exposes, and Action produces one usable model response. Everything else the
turn does -- iteration accounting, hook phases, tool execution and approval,
the three in-loop recoveries, persistence and event order -- stays with the
L2 shell, which is what makes these four replaceable at all.

_Avoid_: reading ``ActionModule`` as "the loop". The shell owns retries, tool
execution and events; Action owns one model decision. And reading Memory as
the memory engine: Memory here is the turn's *window*, which the context
engine owns; long-term recall is a different organ behind its own paper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from raven.contracts.assembled import AssembledContext, TokenBudget
    from raven.contracts.context import TurnContext
    from raven.contracts.llm_provider import LLMProvider, LLMResponse

__tier__ = "factory_loop"


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
    """One model decision, from an assembled request to a usable response."""

    async def decide(self, request: ActionRequest) -> "LLMResponse": ...


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
]
