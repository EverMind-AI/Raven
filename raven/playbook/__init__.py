"""Playbook — reusable task templates: generation, matching, execution.

A playbook is one directory under the playbooks scan root holding a
``playbook.md`` (two-field frontmatter, human body, one fenced
``yaml playbook-spec`` block) — one file, no sidecar. The two modes differ
only in where the graph comes from: ``dag`` ships it, ``prompt`` ships
assembly guidance a model turns into a graph at run time — same validation,
same execution chain. Whether a playbook is matchable on this machine is
config (``playbooks.disabled``), never file content.

Package layout:

- ``types``      — the pydantic contract (:class:`PlaybookSpec` et al.)
- ``role_pool``  — the four capability bases behind the v1 agent roster
- ``triggers``   — offline vocabulary expansion + guards (L1 material)
- ``matcher``    — L1 index and the LLM gate (L2)
- ``validate``   — the field definition's rule table
- ``prompt``     — generation / repair / revise / compose prompt assembly
- ``store``      — playbook.md persistence
- ``generator``  — :class:`PlaybookGenerator` (generate / revise)
- ``executor``   — matched spec + params -> a running graph
- ``runtime``    — the per-message funnel packaged for the agent loop
"""

from raven.playbook.executor import ExecutionPlan, PlaybookExecutor, RoleBuildSpec
from raven.playbook.generator import (
    CapabilityInventory,
    GeneratedPlaybook,
    PlaybookGenerationError,
    PlaybookGenerator,
    StaticInventory,
)
from raven.playbook.matcher import GateVerdict, MatchCandidate, TriggerIndex, gate
from raven.playbook.role_pool import RoleBase, RolePoolError, agent_roster, base_of, load_role_pool
from raven.playbook.runtime import PlaybookRuntime
from raven.playbook.store import BUILTIN_ROOT, PlaybookExistsError, PlaybookOrigin, PlaybookStore
from raven.playbook.triggers import expand_triggers, find_collisions, normalize
from raven.playbook.types import (
    BUILTIN_AGENTS,
    NodeSpec,
    ParamSpec,
    PlaybookSpec,
    Triggers,
)

__all__ = [
    "BUILTIN_AGENTS",
    "BUILTIN_ROOT",
    "CapabilityInventory",
    "ExecutionPlan",
    "GateVerdict",
    "GeneratedPlaybook",
    "MatchCandidate",
    "NodeSpec",
    "ParamSpec",
    "PlaybookExecutor",
    "PlaybookExistsError",
    "PlaybookGenerationError",
    "PlaybookGenerator",
    "PlaybookOrigin",
    "PlaybookRuntime",
    "PlaybookSpec",
    "PlaybookStore",
    "RoleBase",
    "RoleBuildSpec",
    "RolePoolError",
    "StaticInventory",
    "TriggerIndex",
    "Triggers",
    "agent_roster",
    "base_of",
    "expand_triggers",
    "find_collisions",
    "gate",
    "load_role_pool",
    "normalize",
]
