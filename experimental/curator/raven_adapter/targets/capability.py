"""Capability authoring entries for tool exposure, implementations and resources."""

from pathlib import Path
from typing import Any

from raven.agent.tools.registry import admit_tool
from raven.config.raven import PluginsConfig
from raven.config.schema import MCPServerConfig, ToolsConfig
from raven.contracts.harness import CapabilityModule
from raven.contracts.participant import AgentParticipant, StepView
from raven.contracts.tool import Tool
from raven.plugins.manifest import ToolContribution

from ...harness.declaration import Target
from ...harness.prompts import Prompt
from ...harness.state import StateUse, Task
from ...harness.strategies import CapabilityStrategy
from ..capability.contracts import TARGET, CapabilityBinding, CapabilityResources
from ..inference import Inference
from ..planning.contracts import PlanReader
from ..strategy import TaskBinding
from . import PARTICIPANT_STATE, PLUGIN_STATE, EntryPoint

CONTRACT = CapabilityModule

TARGETS = (
    Target(
        roles=("capability",),
        name=TARGET,
        contract=CapabilityStrategy,
        binding=TARGET,
        payload=CapabilityBinding,
        channels=("model_input", "tool_interaction"),
        effect="Generate inert tool/skill resources and semantic capability selection. Native permissions still apply. Selection state is kept per session and survives its turns and revisions.",
        state=(
            StateUse(
                resource="Capability checkpoint",
                scope="session",
                access="Factory-injected mutable JSON mapping, one per session, owned by this strategy.",
                lifecycle="Copied for validation; successful calls persist; failed installation restores it.",
            ),
        ),
        knowledge=(
            Prompt,
            Inference,
            Path(__file__).resolve().parents[2] / "harness/reference/prompt-resources.md",
            CapabilityStrategy,
            CapabilityBinding,
            TaskBinding,
            PlanReader,
            Task,
            StepView,
            CapabilityResources,
            Tool,
            admit_tool,
            Path(__file__).resolve().parents[2] / "harness/reference/capability.md",
            Path(__file__).resolve().parents[2] / "harness/reference/strategy-prompts.md",
            Path(__file__).resolve().parents[1] / "reference/capability.md",
        ),
    ),
    Target(
        roles=("capability",),
        name="capability.select_tools",
        contract=AgentParticipant.select_tools,
        binding="HostWiring.hooks",
        payload=EntryPoint,
        channels=("model_input", "tool_interaction"),
        phases=("iteration",),
        result=list[dict[str, Any]] | None,
        state=(PARTICIPANT_STATE,),
        knowledge=(StepView, PlanReader),
        effect="Change offered definitions, including contributed definitions; execution remains subject to registry authorization.",
    ),
    Target(
        roles=("capability",),
        name="capability.tools",
        contract=ToolContribution,
        binding="plugin.contributes.tools",
        payload=list[ToolContribution],
        channels=("model_input", "tool_interaction"),
        state=(PLUGIN_STATE,),
        effect="Register tools built by native PluginContext factories; a factory reference can use supplied or existing code.",
    ),
    Target(
        roles=("capability",),
        name="capability.tool_config",
        contract=ToolsConfig,
        binding="config.tools",
        payload=ToolsConfig,
        channels=("model_input", "tool_interaction"),
        fields=tuple(
            name
            for name in ToolsConfig.model_fields
            if name
            not in {
                "mcp_servers",
                "sandbox",
                "restrict_to_workspace",
            }
        ),
        effect="Configure worker tools. Sandbox and workspace grants remain host-owned, and so do the tools the host "
        "disables: an authored disabled_tools list adds to them. MCP configuration has its own target.",
    ),
    Target(
        roles=("capability",),
        name="capability.mcp",
        contract=MCPServerConfig,
        binding="config.tools.mcp_servers",
        payload=dict[str, MCPServerConfig],
        channels=("model_input", "tool_interaction"),
        effect="Assemble configured MCP connections and their discovered tools; connection status must be observed.",
    ),
    Target(
        roles=("capability",),
        name="capability.plugins",
        contract=PluginsConfig,
        binding="raven_config.plugins",
        payload=PluginsConfig,
        channels=("model_input", "tool_interaction", "execution_control"),
        effect="Select existing plugin roots and native config slices; every resulting contribution still needs host admission.",
    ),
)
