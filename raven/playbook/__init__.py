"""Playbook — reusable Harnesses and Workflows.

A playbook is one directory under the scan root holding one ``playbook.md``:
two-field frontmatter, a human-readable body, and one fenced
``yaml playbook-spec`` block. Schema v2 stores an optional durable Harness,
an optional concrete DAG Workflow, or both. New artifacts never use prompt
mode. Schema-v1 DAG/prompt files remain readable and executable for backwards
compatibility. Whether an artifact is offered on this machine is config
(``playbooks.disabled``), never file content.

Package layout:

- ``unified``    — schema-v2 Harness + Workflow contract
- ``types``      — legacy schema-v1 contract
- ``agent_profiles`` — what each sub-agent on the table can be asked to do
- ``llm_result`` — the shapes a generation call comes back in
- ``params``     — parameter values and ``${params.x}`` / ``{{ params.x }}`` refs
- ``mcp``        — the servers a run may reach, and the MCP pre-flight
- ``triggers``   — offline vocabulary expansion + guards (L1 material)
- ``matcher``    — the trigger vocabulary index (a retrieval hint)
- ``router``     — which playbooks this turn describes in full
- ``validate``   — the field definition's rule table
- ``prompt``     — generation / repair / revise / compose prompt assembly
- ``store``      — playbook.md persistence
- ``generator``  — :class:`PlaybookGenerator` (generate / revise)
- ``executor``   — matched spec + params -> a running graph
- ``runtime``    — the library plus ``load``, the one execution entry
"""

from raven.playbook.agent_profiles import agent_profiles_from_registry
from raven.playbook.executor import ExecutionPlan, PlaybookExecutor
from raven.playbook.generator import (
    CapabilityInventory,
    GeneratedPlaybook,
    PlaybookGenerationError,
    PlaybookGenerator,
    PlaybookProtocolError,
    PlaybookProviderError,
    StaticInventory,
    live_inventory,
)
from raven.playbook.matcher import TriggerIndex
from raven.playbook.router import RouterSizes, select_playbooks
from raven.playbook.runtime import MAX_GAP_ROUNDS, PlaybookRuntime
from raven.playbook.store import BUILTIN_ROOT, PlaybookExistsError, PlaybookOrigin, PlaybookStore
from raven.playbook.triggers import find_collisions, normalize
from raven.playbook.types import (
    NodeSpec,
    ParamSpec,
    PlaybookSpec,
    Triggers,
)
from raven.playbook.unified import (
    InputSchema,
    PlaybookMatch,
    PlaybookMetadata,
    StoredPlaybook,
    UnifiedPlaybookSpec,
    WorkflowSpec,
)

__all__ = [
    "BUILTIN_ROOT",
    "CapabilityInventory",
    "ExecutionPlan",
    "GeneratedPlaybook",
    "InputSchema",
    "NodeSpec",
    "ParamSpec",
    "PlaybookExecutor",
    "PlaybookExistsError",
    "PlaybookGenerationError",
    "PlaybookGenerator",
    "PlaybookProviderError",
    "PlaybookProtocolError",
    "PlaybookOrigin",
    "PlaybookMatch",
    "PlaybookMetadata",
    "StoredPlaybook",
    "MAX_GAP_ROUNDS",
    "PlaybookRuntime",
    "RouterSizes",
    "select_playbooks",
    "PlaybookSpec",
    "PlaybookStore",
    "StaticInventory",
    "TriggerIndex",
    "Triggers",
    "UnifiedPlaybookSpec",
    "WorkflowSpec",
    "agent_profiles_from_registry",
    "find_collisions",
    "live_inventory",
    "normalize",
]
