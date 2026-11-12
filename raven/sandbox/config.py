"""Pydantic configuration model for the sandbox package."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

_RETIRED_BACKENDS = ("auto", "boxlite")
_RETIRED_KEYS = ("debug", "diskSizeGb", "disk_size_gb")
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    # The config is re-read on every reload; one warning per process is enough.
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)


class SandboxConfig(BaseModel):
    """Sandbox execution configuration (an OpenShell sandbox on a local gateway)."""

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    # "none"      -> DirectExecutor: commands run on the host with no isolation (default)
    # "openshell" -> one OpenShell sandbox per executor, on the registered gateway
    backend: Literal["none", "openshell"] = "none"
    # Name of an OpenShell gateway registered with the CLI; None uses the active one.
    gateway: str | None = None
    image: str = "ubuntu:22.04"
    cpus: int = 2
    memory_mib: int = 2048
    # True: every host of three or more labels plus common two-label domains;
    # False: no network; a list: those domains only, or the open set plus them
    # when the list contains "*".
    allow_net: bool | list[str] = True
    # Extra volume mounts: each entry is [host_path, sandbox_path, "ro"|"rw"]
    extra_volumes: list[list[str]] = Field(default_factory=list)
    # Default timeout (seconds) for a single exec call inside the sandbox
    default_timeout: int = 120
    # Timeout (seconds) for the startup probe
    verify_timeout: int = 30
    # Timeout (seconds) for image pull and sandbox creation
    create_timeout: int = 300

    @model_validator(mode="before")
    @classmethod
    def _migrate_retired(cls, data: Any) -> Any:
        """Read configs written for the retired boxlite backend instead of rejecting them."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if data.get("backend") in _RETIRED_BACKENDS:
            _warn_once(
                "backend",
                f"tools.sandbox.backend={data['backend']!r} is retired; using 'openshell'. "
                "Update the config to backend 'openshell'.",
            )
            data["backend"] = "openshell"
        for key in _RETIRED_KEYS:
            if key in data:
                data.pop(key)
                _warn_once(key, f"tools.sandbox.{key} applied to the retired boxlite backend and is ignored.")
        return data

    @field_validator("allow_net")
    @classmethod
    def _validate_allow_net(cls, v: bool | list[str]) -> bool | list[str]:
        if isinstance(v, list) and len(v) == 0:
            raise ValueError(
                "allow_net: [] is ambiguous: an empty allowlist may read as 'allow all' or "
                "'allow none'. Use allow_net: false to disable networking entirely."
            )
        return v

    @field_validator("extra_volumes")
    @classmethod
    def _validate_volumes(cls, v: list[list[str]]) -> list[list[str]]:
        for entry in v:
            if len(entry) != 3 or entry[2] not in ("ro", "rw"):
                raise ValueError(
                    f"Invalid volume entry {entry!r}; each entry must be [host_path, sandbox_path, 'ro'|'rw']"
                )
            if not Path(entry[0]).is_absolute():
                raise ValueError(f"Volume host path must be absolute: {entry[0]!r}")
            if not Path(entry[1]).is_absolute():
                raise ValueError(f"Volume sandbox path must be absolute: {entry[1]!r}")
        return v
