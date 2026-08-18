"""The role pool — capability bases the generator casts roles from.

Built-in defaults live in ``roles_default.yaml`` next to this module. A caller
may pass ``overrides`` to merge on top:

- same base name -> field-level override (only the fields given);
- new base name  -> added to the pool.

No config key feeds that parameter today -- every caller loads the defaults --
so the merge is the mechanism a future config or registry uses, not a promise
that editing config changes the pool right now.

The generator never hardcodes base names — it renders whatever the merged
pool holds, and contract validation checks ``RoleSpec.base`` against the
same pool. Adding a fifth base is therefore a config change, not a code
change.

Bad config is rejected at load time rather than surfacing as degraded
generation quality: a base whose description is empty or trivially short
gives the casting LLM nothing to decide with, so it fails fast here.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

_DEFAULTS_FILE = Path(__file__).parent / "roles_default.yaml"

_KNOWN_TAGS = {"stateful", "stateless", "local-files", "no-local-files"}
_MIN_DESCRIPTION_CHARS = 20


class RoleBase(BaseModel):
    """One capability base: what the casting LLM knows about it."""

    model_config = ConfigDict(extra="forbid")

    description: str
    tags: list[str] = Field(default_factory=lambda: ["stateful", "local-files"])
    # No model / effort here on purpose: nothing on the execution path reads a
    # per-base model, so declaring one would be a knob that silently does
    # nothing. Per-agent model, credentials and connection details belong to the
    # agent registry, which is where a node's ``agent`` name resolves.


class RolePoolError(ValueError):
    """Raised for a pool that would degrade generation silently."""


def _check(name: str, base: RoleBase) -> None:
    if len(base.description.strip()) < _MIN_DESCRIPTION_CHARS:
        raise RolePoolError(
            f"role base {name!r}: description too short to cast against (need >= {_MIN_DESCRIPTION_CHARS} chars)"
        )
    unknown = set(base.tags) - _KNOWN_TAGS
    if unknown:
        raise RolePoolError(f"role base {name!r}: unknown tags {sorted(unknown)} (known: {sorted(_KNOWN_TAGS)})")


def agent_roster(pool: dict[str, "RoleBase"]) -> dict[str, str]:
    """v1 agent names for the generator/executor: ``<base>-raven``.

    When the unified agent registry grows configurable builtin rows, the
    roster comes from there and this helper retires."""
    return {f"{base}-raven": rb.description.strip() for base, rb in pool.items()}


def base_of(agent_name: str) -> str:
    """The capability base behind a v1 agent name."""
    return agent_name.removesuffix("-raven")


def load_role_pool(overrides: dict[str, dict] | None = None) -> dict[str, RoleBase]:
    """Load built-in bases and merge config overrides/additions on top.

    Args:
        overrides (`dict[str, dict] | None`):
            ``playbook.roles`` from config: base name -> partial or full
            :class:`RoleBase` fields.

    Returns:
        `dict[str, RoleBase]`:
            The merged, validated pool.

    Raises:
        `RolePoolError`:
            Empty description, unknown tags, or an empty resulting pool.
    """
    raw: dict[str, dict] = yaml.safe_load(_DEFAULTS_FILE.read_text(encoding="utf-8"))
    for name, patch in (overrides or {}).items():
        if not isinstance(patch, dict):
            raise RolePoolError(f"role base {name!r}: override must be a mapping, got {type(patch).__name__}")
        raw[name] = {**raw.get(name, {}), **patch}

    pool: dict[str, RoleBase] = {}
    for name, fields in raw.items():
        base = RoleBase.model_validate(fields)
        _check(name, base)
        pool[name] = base
    if not pool:
        raise RolePoolError("role pool is empty")
    return pool
