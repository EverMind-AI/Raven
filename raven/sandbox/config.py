"""Pydantic configuration model for the sandbox package."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class SandboxDebugConfig(BaseModel):
    """Debug socket server configuration (nested under sandbox.debug)."""

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    enabled: bool = False
    socket: str = "sandbox/debug.sock"
    max_message_bytes: int = 1048576  # 1 MiB

    @field_validator("max_message_bytes")
    @classmethod
    def _validate_max_message_bytes(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("maxMessageBytes must be > 0")
        return v


class SandboxConfig(BaseModel):
    """Sandbox execution configuration (boxlite microVM)."""

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    # "none"    → DirectExecutor: runs directly on host with no isolation (default)
    # "auto"    → auto-detect: currently the only supported backend is boxlite;
    #             raises an error on startup if detection fails
    # "boxlite" → force boxlite; also runs availability probe, errors if unavailable
    backend: Literal["none", "auto", "boxlite"] = "none"
    image: str = "ubuntu:22.04"
    cpus: int = 2
    memory_mib: int = 2048
    disk_size_gb: int | None = None  # None = ephemeral (boxlite default)
    # Network: True=fully open; False=no network; list=domain allowlist
    allow_net: bool | list[str] = True
    # Extra volume mounts: each entry is [host_path, vm_path, "ro"|"rw"]
    extra_volumes: list[list[str]] = Field(default_factory=list)
    # Default timeout (seconds) for a single exec call inside the sandbox
    default_timeout: int = 120
    # Timeout (seconds) for the startup echo-ok probe
    verify_timeout: int = 30
    # Timeout (seconds) for image pull + VM creation
    create_timeout: int = 300
    # Debug socket server (nested object)
    debug: SandboxDebugConfig = Field(default_factory=SandboxDebugConfig)

    @field_validator("allow_net")
    @classmethod
    def _validate_allow_net(cls, v: bool | list[str]) -> bool | list[str]:
        if isinstance(v, list) and len(v) == 0:
            raise ValueError(
                "allow_net: [] is ambiguous — an empty allowlist may mean 'allow all' or "
                "'allow none' depending on the boxlite runtime. "
                "Use allow_net: false to disable networking entirely."
            )
        return v

    @field_validator("extra_volumes")
    @classmethod
    def _validate_volumes(cls, v: list[list[str]]) -> list[list[str]]:
        for entry in v:
            if len(entry) != 3 or entry[2] not in ("ro", "rw"):
                raise ValueError(
                    f"Invalid volume entry {entry!r}; "
                    "each entry must be [host_path, vm_path, 'ro'|'rw']"
                )
            if not Path(entry[0]).is_absolute():
                raise ValueError(
                    f"Volume host path must be absolute: {entry[0]!r}"
                )
            if not Path(entry[1]).is_absolute():
                raise ValueError(
                    f"Volume VM path must be absolute: {entry[1]!r}"
                )
        return v
