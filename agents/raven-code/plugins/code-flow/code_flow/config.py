"""FlowConfig: the code flow's knobs, validated from the plugin's config slice.

The slice reaches the plugin as a plain dict with camelCase keys exactly as
the launcher renders them (``plugins.config["code-flow"]``, spelled by
agents/raven-code/run.py), so the model accepts both camelCase and snake_case
(``alias_generator=to_camel`` + ``populate_by_name``) and ignores unknown keys
instead of forbidding them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _Base(BaseModel):
    """Accepts both camelCase and snake_case keys; unknown keys are ignored."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class FlowConfig(_Base):
    """The code flow's product gate.

    ``enabled`` defaults False the D6 way: an absent slice casts no surface
    at all.
    """

    enabled: bool = False

    @classmethod
    def from_slice(cls, raw: dict[str, Any] | None) -> "FlowConfig":
        return cls.model_validate(dict(raw or {}))


__all__ = ["FlowConfig"]
