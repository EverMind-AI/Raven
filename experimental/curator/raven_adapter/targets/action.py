"""Action authoring entries for model behavior, judgments and execution control."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from raven.config.schema import AgentDefaults
from raven.contracts.harness import ActionModule
from raven.contracts.participant import AgentParticipant, StepView
from raven.contracts.tool_gate import ToolGate
from raven.plugins.manifest import HookContribution, ServiceContribution, ToolGateContribution

from ...harness.declaration import Target
from ...harness.prompts import Prompt
from ...harness.state import StateUse, Task
from ...harness.strategies import ActionStrategy
from ..action.contracts import TARGET, ActionBinding
from ..inference import Inference
from ..planning.contracts import PlanReader
from ..strategy import TaskBinding
from . import PARTICIPANT_STATE, PLUGIN_STATE, EntryPoint

CONTRACT = ActionModule


class ReviewResult(BaseModel):
    """The mapping consumed by Raven's read_verdict, before native composition."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["accept", "resample", "end"]
    reason: str | None = None
    inject: list[dict[str, Any]] | None = None
    overrides: dict[str, Any] | None = None
    reply: Any = None
    note: str | None = None


TARGETS = (
    Target(
        roles=("action",),
        name=TARGET,
        contract=ActionStrategy,
        binding=TARGET,
        payload=ActionBinding,
        channels=("model_decision", "tool_interaction", "execution_control"),
        effect="Generate optional pre-decision guidance plus assessment and recovery decisions, translated to native addenda, review and terminal salvage. Native composition and bounded retries remain in control.",
        state=(
            StateUse(
                resource="Action checkpoint",
                scope="session",
                access="Factory-injected mutable JSON mapping, one per session, owned by this strategy.",
                lifecycle="Copied for validation; successful calls persist; failed installation restores it.",
            ),
        ),
        knowledge=(
            Prompt,
            Inference,
            Path(__file__).resolve().parents[2] / "harness/reference/prompt-resources.md",
            ActionStrategy,
            ActionBinding,
            TaskBinding,
            Task,
            StepView,
            PlanReader,
            ReviewResult,
            Path(__file__).resolve().parents[2] / "harness/reference/action.md",
            Path(__file__).resolve().parents[2] / "harness/reference/strategy-prompts.md",
            Path(__file__).resolve().parents[1] / "reference/action.md",
        ),
    ),
    Target(
        roles=("action",),
        name="action.config",
        contract=AgentDefaults,
        binding="config.agents.defaults",
        payload=AgentDefaults,
        channels=("model_input", "model_decision", "execution_control"),
        fields=tuple(
            name
            for name in AgentDefaults.model_fields
            if name
            not in {
                "workspace",
                "max_concurrent_subagents",
                "max_subagent_spawns_per_hour",
            }
        ),
        effect="Configure worker behavior; agent-home identity and collaboration limits remain host-owned.",
    ),
    Target(
        roles=("action",),
        name="action.review",
        contract=AgentParticipant.review,
        binding="HostWiring.hooks",
        payload=EntryPoint,
        channels=("model_decision", "tool_interaction", "execution_control"),
        phases=("execute_tools", "after_iteration"),
        result=ReviewResult | None,
        state=(PARTICIPANT_STATE,),
        knowledge=(StepView, PlanReader),
        effect="Accept, resample or end through native composition. Rollback is bounded and never reverses tool side effects.",
    ),
    Target(
        roles=("action",),
        name="action.salvage",
        contract=AgentParticipant.salvage,
        binding="HostWiring.hooks",
        payload=EntryPoint,
        channels=("model_decision", "execution_control"),
        phases=("answerless",),
        result=str | None,
        state=(PARTICIPANT_STATE,),
        knowledge=(StepView, PlanReader),
        effect="Offer a terminal reply after an error or missing final answer, unless Raven has budgeted a rerun. Exhaustion synthesis can already supply an answer; the first composed salvage wins.",
    ),
    Target(
        roles=("action",),
        name="action.hooks",
        contract=HookContribution,
        binding="plugin.contributes.hooks",
        payload=list[HookContribution],
        channels=("model_input", "tool_interaction", "execution_control"),
        state=(PLUGIN_STATE,),
        knowledge=(PlanReader,),
        effect="Contribute native AgentHook factories; phase ordering and HookDecision application remain host-controlled.",
    ),
    Target(
        roles=("action",),
        name="action.tool_gates",
        contract=ToolGateContribution,
        binding="plugin.contributes.tool_gates",
        payload=list[ToolGateContribution],
        channels=("tool_interaction", "execution_control"),
        state=(PLUGIN_STATE,),
        knowledge=(ToolGate, PlanReader),
        effect="Adjudicate after parameter validation and before dispatch; a non-None result or an error refuses this call.",
    ),
    Target(
        roles=("action",),
        name="action.services",
        contract=ServiceContribution,
        binding="plugin.contributes.services",
        payload=list[ServiceContribution],
        channels=("tool_interaction", "execution_control"),
        state=(PLUGIN_STATE,),
        effect="Contribute generation-scoped background services; a resident host starts and stops them.",
    ),
)
