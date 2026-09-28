"""Planning authoring entries for guidance and reusable procedural knowledge."""

from pathlib import Path

from raven.agent.subagent.dag_graph import DagNodeSpec
from raven.config.raven import SkillForgeConfig
from raven.contracts.harness import PlanningModule
from raven.contracts.participant import AgentParticipant, StepView
from raven.memory_engine.skill_local.registry import SkillRegistry
from raven.playbook.types import PlaybookSpec

from ....requirements import Requirement
from ...harness.artifact import ArtifactPath
from ...harness.declaration import Target
from ...harness.prompts import Prompt
from ...harness.state import StateUse
from ...harness.strategies import PlanningStrategy
from ..inference import Inference
from ..planning.contracts import TARGET, PlanningBinding, PlanningObservation
from . import PARTICIPANT_STATE, EntryPoint

CONTRACT = PlanningModule

TARGETS = (
    Target(
        roles=("planning",),
        name=TARGET,
        contract=PlanningStrategy,
        binding="planning.strategy",
        payload=PlanningBinding,
        channels=("model_input", "tool_interaction", "execution_control"),
        effect="Generate semantic initialize/view/revise behavior, with optional tool, model-input and "
        "completed-iteration translations. Each session (conversation) has its own plan, initialized from the task "
        "on first use and checkpointed across its turns and Harness revisions. "
        "The selected worker must have a host-owned task binding. Native tool permissions still apply.",
        state=(
            StateUse(
                resource="Planning checkpoint",
                scope="session",
                access="Factory-injected mutable JSON mapping, one per session; the strategy owns its meaning and mutations.",
                lifecycle="Copied for validation, saved after "
                "successful operations, restored on failed installation; factory migrations are explicit.",
            ),
        ),
        knowledge=(
            Prompt,
            Inference,
            Path(__file__).resolve().parents[2] / "harness/reference/prompt-resources.md",
            PlanningStrategy,
            PlanningBinding,
            PlanningObservation,
            Path(__file__).resolve().parents[2] / "harness/reference/planning.md",
            Path(__file__).resolve().parents[2] / "harness/reference/strategy-prompts.md",
            Path(__file__).resolve().parents[1] / "reference/planning.md",
        ),
    ),
    Target(
        roles=("planning",),
        name="planning.advise",
        contract=AgentParticipant.advise,
        binding="HostWiring.hooks",
        payload=EntryPoint,
        channels=("model_input", "execution_control"),
        phases=("iteration", "after_iteration"),
        result=str | None,
        state=(PARTICIPANT_STATE,),
        knowledge=(StepView,),
        effect="Advice is composed in participant order and passed to the host for subsequent model input.",
    ),
    Target(
        roles=("capability",),
        name="planning.skills",
        contract=SkillRegistry,
        binding="skill_files",
        payload=dict[ArtifactPath, str],
        channels=("model_input", "tool_interaction"),
        effect="Files are relative to a local skill root; each package needs SKILL.md. Discovery alone does not mean a body was read.",
    ),
    Target(
        roles=("planning",),
        name="planning.playbooks",
        contract=PlaybookSpec,
        binding="playbook_files",
        payload=dict[ArtifactPath, str],
        channels=("model_input", "tool_interaction", "execution_control"),
        effect="Files are relative to the agent home's playbooks folder, one <name>/playbook.md each, with optional nonempty <name>/nodes/<id>/requirements.json files of Requirement arrays. The main agent "
        "sees offered playbooks in its tool table and runs one with load_playbook; a dag-mode playbook runs its "
        "nodes as a background sub-agent graph whose result returns to the conversation as a new turn. Node skills "
        "reach only built-in Raven sub-agents that are not managed children; an external agent or a managed child "
        "takes only its node's prompt.",
        knowledge=(
            PlaybookSpec,
            DagNodeSpec,
            Requirement,
            Path(__file__).resolve().parents[1] / "reference/playbooks.md",
        ),
    ),
    Target(
        roles=("capability",),
        name="planning.skill_config",
        contract=SkillForgeConfig,
        binding="raven_config.skill_forge",
        payload=SkillForgeConfig,
        channels=("model_input", "tool_interaction"),
        effect="Configure native skill sources and pull/push delivery. Legacy placeholder fields do not establish implemented behavior.",
    ),
)
