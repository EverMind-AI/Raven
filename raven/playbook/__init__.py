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

from raven.playbook.executor import ExecutionPlan, PlaybookExecutor
from raven.playbook.generator import (
    CapabilityInventory,
    GeneratedPlaybook,
    PlaybookGenerationError,
    PlaybookGenerator,
    StaticInventory,
    live_inventory,
)
from raven.playbook.matcher import TriggerIndex
from raven.playbook.router import RouterSizes, select_playbooks
from raven.playbook.runtime import MAX_GAP_ROUNDS, PlaybookRuntime
from raven.playbook.store import BUILTIN_ROOT, PlaybookExistsError, PlaybookOrigin, PlaybookStore
from raven.playbook.triggers import expand_triggers, find_collisions, normalize
from raven.playbook.types import (
    NodeSpec,
    ParamSpec,
    PlaybookSpec,
    Triggers,
)

__all__ = [
    "BUILTIN_ROOT",
    "CapabilityInventory",
    "ExecutionPlan",
    "GeneratedPlaybook",
    "NodeSpec",
    "ParamSpec",
    "PlaybookExecutor",
    "PlaybookExistsError",
    "PlaybookGenerationError",
    "PlaybookGenerator",
    "PlaybookOrigin",
    "MAX_GAP_ROUNDS",
    "PlaybookRuntime",
    "RouterSizes",
    "select_playbooks",
    "PlaybookSpec",
    "PlaybookStore",
    "StaticInventory",
    "TriggerIndex",
    "Triggers",
    "expand_triggers",
    "find_collisions",
    "live_inventory",
    "normalize",
]
