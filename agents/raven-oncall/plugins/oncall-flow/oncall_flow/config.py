"""FlowConfig: the oncall flow's knobs, validated from the plugin's config slice.

The slice reaches the plugin as a plain dict with camelCase keys exactly as
the product writes them in ``config.json`` (``plugins.config["oncall-flow"]``),
so every model here accepts both camelCase and snake_case
(``alias_generator=to_camel`` + ``populate_by_name``) and ignores unknown keys
instead of forbidding them -- the slice also carries the plugin-only key
``stateRoot`` (launcher-injected) that the flow models do not own.

Part one carries only the gate and the watch-loop knob; the fork's other
tunables (interruption contract, retry policy, budgets) are per-campaign
``meta.json`` facts written by ``ops_declare``, not config -- they arrive with
the tools in part two and never belonged in this file.
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


class WatcherConfig(_Base):
    """The resident event watcher's pace: the fork's 20s no-LLM poll."""

    poll_interval_seconds: float = 20.0


class FlowConfig(_Base):
    """The oncall flow: the product gate and the watch loop.

    ``enabled`` defaults False the way the fork's ``tools.oncall.enabled``
    did: an absent slice casts no surface at all.
    """

    enabled: bool = False
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)

    @classmethod
    def from_slice(cls, d: dict[str, Any] | None) -> "FlowConfig":
        """Validate the plugin's config slice as the product wrote it."""
        return cls.model_validate(d or {})


def state_root(raw: dict[str, Any] | None, workspace: Path | str) -> Path:
    """The campaign-state root: the slice's ``stateRoot``, else a workspace default.

    ``stateRoot`` is a plugin-only key outside the flow model (mirroring
    research-flow); the launcher injects it at render time, and the fallback
    covers a host that activates the plugin without the launcher.
    """
    value = (raw or {}).get("stateRoot")
    return Path(value) if value else Path(workspace) / "oncall_flow"


__all__ = [
    "FlowConfig",
    "WatcherConfig",
    "state_root",
]
