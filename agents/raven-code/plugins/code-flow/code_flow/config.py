"""FlowConfig: the code flow's knobs, validated from the plugin's config slice.

The slice reaches the plugin as a plain dict with camelCase keys exactly as
the launcher renders them (``plugins.config["code-flow"]``, spelled by
agents/raven-code/run.py), so the model accepts both camelCase and snake_case
(``alias_generator=to_camel`` + ``populate_by_name``) and ignores unknown keys
instead of forbidding them.
"""

from __future__ import annotations

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


class ExecFaceConfig(_Base):
    """The shell tool's knobs, rendered in by the launcher from the host config.

    ``timeout`` and ``path_append`` mirror the host's own ``tools.exec``;
    ``max_timeout`` is the clamp ceiling the fork had and trunk does not;
    ``sandbox_backend`` mirrors ``tools.sandbox.backend`` so the factory can
    refuse to replace a sandboxed exec with a host-running one.
    """

    timeout: int = 60
    max_timeout: int = 1200
    path_append: str = ""
    sandbox_backend: str = "none"


class ToolsConfig(_Base):
    """The product's own tool face, and the two conduct knobs it carries.

    ``enabled`` defaults False the D6 way, like the flow itself: an absent
    section hands the tools back to the host, and the model is served the
    built-ins unchanged.

    ``require_read_before_edit`` is the fork's "editing unread content is
    rejected" rule; ``python_syntax_note`` appends the post-write syntax
    verdict to a ``.py`` write or edit; ``restrict_to_workspace`` mirrors the
    host's own ``tools.restrictToWorkspace`` -- a plugin factory cannot read
    that field (``ServiceLocator`` grants five things and this is not one), so
    the launcher renders the product's answer in here. Without it a product
    that asked for the fence got replacements with no fence at all.
    """

    enabled: bool = False
    require_read_before_edit: bool = True
    python_syntax_note: bool = True
    restrict_to_workspace: bool = False
    exec: ExecFaceConfig = Field(default_factory=ExecFaceConfig)


class FlowConfig(_Base):
    """The code flow's product gate, plus the tool face's own section.

    ``enabled`` controls the flow's notices and workspace reports. The tool
    face keeps its checklist lifecycle while ``tools.enabled`` is True.
    Both default False, so an absent slice casts no surface at all.

    ``project_files`` names the working directory's own instruction files
    (``AGENTS.md`` and its equivalents) contributed to the system message
    during context assembly. Empty reads none; the launcher renders the coding set into the
    slice, and its ``CODE_PROJECT_FILES`` setting empties it.
    """

    enabled: bool = False
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    project_files: list[str] = Field(default_factory=list)

    @classmethod
    def from_slice(cls, raw: dict[str, Any] | None) -> "FlowConfig":
        return cls.model_validate(dict(raw or {}))


__all__ = ["ExecFaceConfig", "FlowConfig", "ToolsConfig"]
