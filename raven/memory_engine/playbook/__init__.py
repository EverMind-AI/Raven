"""Playbook — reusable task templates: generation, matching, execution.

A playbook is one directory under the playbooks scan root holding a
``playbook.md`` (two-field frontmatter, human body, one fenced
``yaml playbook-spec`` block) plus a generator-side ``.provenance.json``
sidecar. The two modes differ only in where the graph comes from: ``dag``
ships it, ``prompt`` ships assembly guidance a model turns into a graph at
run time — same validation, same execution chain.

Package layout:

- ``types``      — the pydantic contract (:class:`PlaybookSpec` et al.)
- ``role_pool``  — the four capability bases behind the v1 agent roster
- ``triggers``   — offline vocabulary expansion + guards (L1 material)
- ``matcher``    — L1 index and the LLM gate (L2)
- ``validate``   — the field definition's rule table
- ``prompt``     — generation / repair / revise / compose prompt assembly
- ``store``      — playbook.md + sidecar persistence
- ``generator``  — :class:`PlaybookGenerator` (generate / revise)
- ``executor``   — matched spec + params -> a running graph
- ``runtime``    — the per-message funnel packaged for the agent loop
"""

from raven.memory_engine.playbook.executor import ExecutionPlan, PlaybookExecutor, RoleBuildSpec
from raven.memory_engine.playbook.generator import (
    CapabilityInventory,
    PlaybookGenerationError,
    PlaybookGenerator,
    StaticInventory,
)
from raven.memory_engine.playbook.matcher import GateVerdict, MatchCandidate, TriggerIndex, gate
from raven.memory_engine.playbook.role_pool import RoleBase, RolePoolError, agent_roster, base_of, load_role_pool
from raven.memory_engine.playbook.runtime import PlaybookRuntime
from raven.memory_engine.playbook.store import PlaybookExistsError, PlaybookStore
from raven.memory_engine.playbook.triggers import expand_triggers, find_collisions, normalize
from raven.memory_engine.playbook.types import (
    BUILTIN_AGENTS,
    NodeSpec,
    ParamSpec,
    PlaybookSpec,
    Provenance,
    Triggers,
)

__all__ = [
    "BUILTIN_AGENTS",
    "CapabilityInventory",
    "ExecutionPlan",
    "GateVerdict",
    "MatchCandidate",
    "NodeSpec",
    "ParamSpec",
    "PlaybookExecutor",
    "PlaybookExistsError",
    "PlaybookGenerationError",
    "PlaybookGenerator",
    "PlaybookRuntime",
    "PlaybookSpec",
    "PlaybookStore",
    "Provenance",
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
