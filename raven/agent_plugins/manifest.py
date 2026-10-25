"""Agent Plugins 1.0.0 manifest reading.

The portable manifest is a closed object. Its authoritative shape is the
official JSON schema at
``https://agent-plugins.org/schemas/1.0.0/plugin.schema.json``; the
specification text is authoritative where the two disagree.

Two rules of the specification do not fit a data model and are coded here
instead:

- an unknown top-level field is reported and ignored, and the plugin still
  loads (spec §5.2);
- a manifest that declares an Agent Plugins version this build does not
  implement is rejected rather than partially loaded (spec §5.2) -- and the
  schema is never retrieved while loading a plugin.

Every other schema violation is fatal for the package: the caller rejects
it and takes no component from it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

AGENT_PLUGINS_SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
"""The canonical manifest schema identifier of Agent Plugins 1.0.0."""

SUPPORTED_SCHEMA_URLS = frozenset({AGENT_PLUGINS_SCHEMA_URL})
"""The Agent Plugins versions this build implements."""

MANIFEST_FILENAME = "plugin.json"
"""The manifest's fixed location at the plugin root (spec §5.1)."""

_NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")

_PERMITTED_KEYS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)
"""The closed set of top-level manifest fields (spec §5.2)."""


class AgentPluginManifestError(ValueError):
    """The manifest violates the Agent Plugins manifest schema.

    Fatal for the package: none of its components are loaded.
    """


class AgentPluginAuthor(BaseModel):
    """The manifest's ``author`` object.

    The official schema closes it, so a field other than ``name``,
    ``email`` or ``url`` makes the manifest invalid.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    email: str | None = None
    url: str | None = None


class AgentPluginManifest(BaseModel):
    """A portable plugin manifest."""

    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)

    schema_uri: str = Field(alias="$schema")
    name: str
    version: str | None = None
    description: str | None = None
    author: AgentPluginAuthor | None = None
    homepage: str | None = None
    repository: str | None = None
    license: str | None = None
    keywords: list[str] = Field(default_factory=list)
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_follows_the_specification(cls, value: str) -> str:
        if not 1 <= len(value) <= 64:
            raise ValueError(f"name must be 1-64 characters, got {len(value)}")
        if not _NAME_RE.match(value):
            raise ValueError(
                f"name {value!r} must be lowercase letters, digits, '-' or '.', start "
                "and end alphanumeric, and carry no '--' or '..'"
            )
        return value

    @field_validator("extensions", mode="before")
    @classmethod
    def _non_object_extensions_are_ignored(cls, value: Any) -> Any:
        # §8.1: a non-object extensions field is reported and ignored, and
        # the rest of the manifest still loads. The caller reports it from
        # the raw document, because the value is gone by now.
        if value is None:
            return {}
        if not isinstance(value, dict):
            return {}
        return value

    @property
    def unknown_fields(self) -> tuple[str, ...]:
        """Top-level fields the manifest schema does not define."""
        return tuple(sorted((self.model_extra or {}).keys()))


def read_manifest(path: Path) -> tuple[AgentPluginManifest, tuple[str, ...]]:
    """Read and validate a manifest file.

    Returns the manifest and the non-fatal notes worth reporting. Raises
    :class:`AgentPluginManifestError` for a fatal violation.
    """
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentPluginManifestError(f"the manifest is unreadable: {exc}") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise AgentPluginManifestError(f"the manifest is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AgentPluginManifestError("the manifest must contain a JSON object")

    schema_uri = raw.get("$schema")
    if not isinstance(schema_uri, str) or schema_uri not in SUPPORTED_SCHEMA_URLS:
        raise AgentPluginManifestError(
            f"the manifest declares Agent Plugins version {schema_uri!r}, which this "
            f"build does not implement (supported: {sorted(SUPPORTED_SCHEMA_URLS)})"
        )

    notes: list[str] = []
    unknown = sorted(set(raw) - _PERMITTED_KEYS)
    if unknown:
        notes.append("unknown manifest field(s) ignored: " + ", ".join(unknown))
    if "extensions" in raw and not isinstance(raw["extensions"], dict):
        notes.append("the manifest's extensions field is not an object; ignored")

    try:
        manifest = AgentPluginManifest.model_validate(raw)
    except ValidationError as exc:
        raise AgentPluginManifestError(
            f"the manifest does not satisfy the Agent Plugins manifest schema: {exc}"
        ) from exc
    return manifest, tuple(notes)


__all__ = [
    "AGENT_PLUGINS_SCHEMA_URL",
    "MANIFEST_FILENAME",
    "SUPPORTED_SCHEMA_URLS",
    "AgentPluginAuthor",
    "AgentPluginManifest",
    "AgentPluginManifestError",
    "read_manifest",
]
