"""Memory authoring entries for model inputs, context assembly and retained records."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, RootModel, model_validator

from raven.config.raven import ContextConfig, MemoryConfig, TokenWiseConfig
from raven.context_engine.segments.render import BOOTSTRAP_FILES, load_bootstrap_files
from raven.contracts.context import ContextEngine
from raven.contracts.harness import MemoryModule
from raven.contracts.participant import AgentParticipant, StepView
from raven.plugins.manifest import MemoryBackendContribution, SessionObserverContribution

from ...harness.declaration import Target
from ...harness.prompts import Prompt
from ...harness.state import StateUse, Task
from ...harness.strategies import MemoryStrategy
from ...harness.strategies.memory import ContextRequest, ContextView
from ..inference import Inference
from ..memory.contracts import TARGET, MemoryBinding
from ..planning.contracts import PlanReader
from ..strategy import TaskBinding
from . import PARTICIPANT_STATE, PLUGIN_STATE, EntryPoint

CONTRACT = MemoryModule


class BootstrapFiles(RootModel[dict[str, str]]):
    """Content for the bootstrap paths read by the native context builder."""

    model_config = ConfigDict(json_schema_extra={"propertyNames": {"enum": BOOTSTRAP_FILES}})

    @model_validator(mode="after")
    def native_paths(self) -> "BootstrapFiles":
        unknown = self.root.keys() - set(BOOTSTRAP_FILES)
        if unknown:
            raise ValueError(f"paths are not native bootstrap inputs: {sorted(unknown)}")
        return self


class IntakeResult(BaseModel):
    """The native intake/addendum mapping, including an optional early reply."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    reply: Any = None
    note: str | None = None


TARGETS = (
    Target(
        roles=("memory",),
        name=TARGET,
        contract=MemoryStrategy,
        binding=TARGET,
        payload=MemoryBinding,
        channels=("model_input", "execution_control"),
        effect="Generate recall/retain and optional compose/compact behavior through native context assembly, or use model-context and completed-iteration paths. Task-owned state survives turns and Harness revisions.",
        state=(
            StateUse(
                resource="Memory checkpoint",
                scope="task",
                access="Factory-injected mutable JSON mapping, owned by this strategy.",
                lifecycle="Copied for validation; successful calls persist; failed installation restores it.",
            ),
        ),
        knowledge=(
            Prompt,
            Inference,
            Path(__file__).resolve().parents[2] / "harness/reference/prompt-resources.md",
            MemoryStrategy,
            ContextRequest,
            ContextView,
            MemoryBinding,
            TaskBinding,
            PlanReader,
            Task,
            StepView,
            Path(__file__).resolve().parents[2] / "harness/reference/memory.md",
            Path(__file__).resolve().parents[2] / "harness/reference/strategy-prompts.md",
            Path(__file__).resolve().parents[1] / "reference/memory.md",
        ),
    ),
    Target(
        roles=("memory",),
        name="memory.prompt",
        contract=load_bootstrap_files,
        binding="bootstrap_files",
        payload=BootstrapFiles,
        channels=("model_input",),
        effect="Content is loaded from the native bootstrap paths in agent home when the context is built.",
    ),
    Target(
        roles=("memory",),
        name="memory.context_config",
        contract=ContextConfig,
        binding="raven_config.context",
        payload=ContextConfig,
        channels=("model_input",),
        effect="Configure native context assembly and its context-window Curator.",
    ),
    Target(
        roles=("memory",),
        name="memory.token_config",
        contract=TokenWiseConfig,
        binding="raven_config.token_wise",
        payload=TokenWiseConfig,
        channels=("model_input", "model_decision"),
        effect="Configure the native token/cache strategy stack; effectiveness depends on the provider and implemented readers.",
    ),
    Target(
        roles=("memory",),
        name="memory.backend_config",
        contract=MemoryConfig,
        binding="raven_config.memory",
        payload=MemoryConfig,
        channels=("model_input",),
        effect="Select and configure the native memory backend and its identity wiring.",
    ),
    Target(
        roles=("memory",),
        name="memory.context_engine",
        contract=ContextEngine,
        binding="build_runtime.context_engine",
        payload=EntryPoint,
        channels=("model_input", "model_decision"),
        effect="Supply a ContextEngine through the native instance socket; respect its context and after-turn lifecycle.",
    ),
    Target(
        roles=("memory",),
        name="memory.backends",
        contract=MemoryBackendContribution,
        binding="plugin.contributes.memory_backends",
        payload=list[MemoryBackendContribution],
        channels=("model_input",),
        effect="Register native memory backend factories; selection, credentials and lifecycle belong to the host.",
        state=(PLUGIN_STATE,),
    ),
    Target(
        roles=("memory",),
        name="memory.session_observers",
        contract=SessionObserverContribution,
        binding="plugin.contributes.session_observers",
        payload=list[SessionObserverContribution],
        channels=("execution_control",),
        effect="Receive session-retirement notifications; only a resident host attaches observers.",
        state=(PLUGIN_STATE,),
    ),
    Target(
        roles=("memory",),
        name="memory.intake",
        contract=AgentParticipant.intake,
        binding="HostWiring.hooks",
        payload=EntryPoint,
        channels=("model_input", "execution_control"),
        phases=("user_inbound",),
        result=IntakeResult | None,
        state=(PARTICIPANT_STATE,),
        effect="Reshape input or provide an early reply; generic inbound hooks are skipped for subagent/sentinel origins.",
    ),
    Target(
        roles=("memory",),
        name="memory.system_addendum",
        contract=AgentParticipant.system_addendum,
        binding="HostWiring.hooks",
        payload=EntryPoint,
        channels=("model_input", "execution_control"),
        phases=("iteration",),
        result=IntakeResult | None,
        state=(PARTICIPANT_STATE,),
        knowledge=(StepView, PlanReader),
        effect="Replace this participant's preceding addendum before the model call; a reply may end the turn.",
    ),
    Target(
        roles=("memory",),
        name="memory.archive",
        contract=AgentParticipant.archive,
        binding="HostWiring.hooks",
        payload=EntryPoint,
        channels=("model_input", "execution_control"),
        phases=("sent",),
        result=dict[str, Any] | None,
        state=(PARTICIPANT_STATE,),
        effect="Stamp the native turn record after sending; reachability depends on the origin's after_send path.",
    ),
)
