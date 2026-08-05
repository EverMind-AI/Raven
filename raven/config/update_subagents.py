"""Atomic write path for ``subagents.third_party`` config (req5 / P4).

The only supported write path for third-party sub-agent config. Every entry
point (CLI, future WebUI "configure Raven" page) must go through here. Entries
are validated against the schema (discriminated ``kind: cli|openai``) before
anything is written, and the write is atomic (temp file + os.replace).

Redaction of secrets (``api_key``) is a display concern left to the caller — this
primitive round-trips raw values so a read→edit→write cycle never clobbers a key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.config.schema import SubagentsConfig

# camelCase alias of SubagentsConfig.third_party (Base uses a camel alias generator).
_ALIAS = "thirdParty"


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _raw_config(path: Path) -> dict[str, Any]:
    return read_raw_or_raise(path) if path.exists() else {}


def _raw_entries(path: Path) -> list[dict]:
    sub = _raw_config(path).get("subagents") or {}
    return sub.get(_ALIAS) or sub.get("third_party") or []


def get_third_party_subagents(*, config_path: Path | None = None) -> list[dict]:
    """Return the configured third-party sub-agents as validated dicts (by alias).

    Raises ``ValidationError`` if the on-disk section is malformed.
    """
    path = config_path or get_config_path()
    validated = SubagentsConfig(third_party=_raw_entries(path))
    return [cfg.model_dump(by_alias=True) for cfg in validated.third_party]


def set_third_party_subagents(entries: list[dict], *, config_path: Path | None = None) -> None:
    """Replace the whole ``subagents.third_party`` list (validate-then-write).

    Raises ``ValidationError`` if any entry violates the schema, or ``ValueError``
    on duplicate names — nothing is written in either case.
    """
    validated = SubagentsConfig(third_party=entries)
    names = [c.name for c in validated.third_party]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate third-party sub-agent name(s): {sorted(dupes)}")
    dumped = validated.model_dump(by_alias=True)[_ALIAS]

    path = config_path or get_config_path()
    data = _raw_config(path)
    data.setdefault("subagents", {})
    # Drop a possible snake_case key so we don't leave two competing entries.
    data["subagents"].pop("third_party", None)
    data["subagents"][_ALIAS] = dumped
    _write_atomic(path, data)
    logger.info("update_subagents: wrote {} third-party sub-agent(s)", len(dumped))


def add_third_party_subagent(entry: dict, *, config_path: Path | None = None) -> None:
    """Add or replace (by name) one third-party sub-agent."""
    path = config_path or get_config_path()
    name = entry.get("name")
    kept = [e for e in _raw_entries(path) if not (isinstance(e, dict) and e.get("name") == name)]
    kept.append(entry)
    set_third_party_subagents(kept, config_path=path)


def remove_third_party_subagent(name: str, *, config_path: Path | None = None) -> bool:
    """Remove a third-party sub-agent by name. Returns True if one was removed."""
    path = config_path or get_config_path()
    current = _raw_entries(path)
    kept = [e for e in current if not (isinstance(e, dict) and e.get("name") == name)]
    if len(kept) == len(current):
        return False
    set_third_party_subagents(kept, config_path=path)
    return True


__all__ = [
    "get_third_party_subagents",
    "set_third_party_subagents",
    "add_third_party_subagent",
    "remove_third_party_subagent",
]
