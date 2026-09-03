"""FlowConfig: the code flow's knobs, validated from the plugin's config slice.

The slice reaches the plugin as a plain dict with camelCase keys exactly as
the launcher renders them (``plugins.config["code-flow"]``, spelled by
agents/raven-code/run.py), so every model here accepts both camelCase and
snake_case (``alias_generator=to_camel`` + ``populate_by_name``) and ignores
unknown keys instead of forbidding them.

The ``workspaceGate`` block replaces the fork's ``RAVEN_WORKSPACE_ALLOC_*``
environment arming (fork workspace_gate.py:74-77, build_workspace_gate
:1241-1253): the launcher used to export the env, now it renders the same
facts into this slice (verdict D6), one layout per hosting. The "unarmed
means no gate at all" contract survives the move: an absent or incomplete
block makes ``armed()`` answer None and the factories decline.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class GateArming:
    """A parsed arming: which layout the launcher rendered, made explicit.

    Exactly one of ``alloc_dir`` (with ``instance``) and ``alloc_base`` is
    set; ``repos_root`` names the repo-level records in both layouts.
    """

    repos_root: Path
    alloc_base: Path | None = None
    alloc_dir: Path | None = None
    instance: str = ""


class WorkspaceGateSlice(_Base):
    """The launcher-rendered gate arming: the scaffold owns these spellings.

    Two layouts, the fork's own pair spelled as config instead of env (fork
    ALLOC_DIR/INSTANCE/ALLOC_BASE/REPOS env quartet, workspace_gate.py:74-77):

    - ``allocDir`` + ``instance``: one process serves one conversation (the
      CLI adjudication hosting); the record is ``<dir>/allocation.json``.
    - ``allocBase``: one process serves many sessions (the ACP server
      multiplexes them on one connection); each session's record lives under
      ``<base>/allocations/``.

    ``reposRoot`` names the repo-level records (locks, owners, worktrees) in
    both layouts, and no layout arms without it. ``stateBucket`` (rendered on
    the ACP layout) is accepted and carried but not consumed by the gate: it
    names the session-state partition the fork's config.paths read, and
    boards with the state-partition wave.
    """

    alloc_base: str = ""
    alloc_dir: str = ""
    instance: str = ""
    repos_root: str = ""
    state_bucket: str = ""

    def armed(self) -> GateArming | None:
        """The arming, or None when the launcher armed nothing.

        The fork's own decision order (build_workspace_gate :1244-1253):
        repos first and always, then the one-conversation pair, then the
        multiplexed base.
        """
        repos = self.repos_root.strip()
        if not repos:
            return None
        alloc_dir = self.alloc_dir.strip()
        instance = self.instance.strip()
        if alloc_dir and instance:
            return GateArming(repos_root=Path(repos), alloc_dir=Path(alloc_dir), instance=instance)
        base = self.alloc_base.strip()
        if base:
            return GateArming(repos_root=Path(repos), alloc_base=Path(base))
        return None


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


__all__ = ["FlowConfig", "GateArming", "WorkspaceGateSlice"]
