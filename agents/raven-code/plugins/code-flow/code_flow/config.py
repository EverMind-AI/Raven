"""FlowConfig: the code flow's knobs, validated from the plugin's config slice.

The slice reaches the plugin as a plain dict with camelCase keys exactly as
the launcher renders them (``plugins.config["code-flow"]``, spelled by
agents/raven-code/run.py), so every model here accepts both camelCase and
snake_case (``alias_generator=to_camel`` + ``populate_by_name``) and ignores
unknown keys instead of forbidding them.

The ``workspaceGate`` block replaces the fork's ``RAVEN_WORKSPACE_ALLOC_*``
environment arming (fork workspace_gate.py:76-79, build_workspace_gate
:1241-1253): the launcher used to export the env, now it renders the same
three facts into this slice (verdict D6). The "unarmed means no gate at all"
contract survives the move: an absent or incomplete block makes ``armed()``
answer None and the factories decline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Base(BaseModel):
    """Accepts both camelCase and snake_case keys; unknown keys are ignored."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class WorkspaceGateSlice(_Base):
    """The launcher-rendered gate arming: the scaffold owns these spellings.

    ``state_bucket`` (rendered as ``stateBucket``) is accepted and carried but
    not consumed by the gate: it names the session-state partition the fork's
    config.paths reads, and boards with the state-partition wave.
    """

    alloc_base: str = ""
    repos_root: str = ""
    state_bucket: str = ""

    def armed(self) -> tuple[Path, Path] | None:
        """(allocation base, repos root), or None when the launcher armed nothing."""
        base = self.alloc_base.strip()
        repos = self.repos_root.strip()
        if not (base and repos):
            return None
        return Path(base), Path(repos)


class FlowConfig(_Base):
    """The code flow: the product gate and the workspace-gate arming.

    ``enabled`` defaults False the D6 way: an absent slice casts no surface
    at all.
    """

    enabled: bool = False
    workspace_gate: WorkspaceGateSlice = Field(default_factory=WorkspaceGateSlice)

    @classmethod
    def from_slice(cls, raw: dict[str, Any] | None) -> "FlowConfig":
        return cls.model_validate(dict(raw or {}))


__all__ = ["FlowConfig", "WorkspaceGateSlice"]
