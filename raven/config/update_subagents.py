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
from pydantic.alias_generators import to_camel

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.config.schema import SubagentsConfig

# camelCase alias of SubagentsConfig.third_party (Base uses a camel alias generator).
_ALIAS = "thirdParty"


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def reject_unsupported_openai_fields(entries: list[dict]) -> None:
    """Raise on an incoming openai entry that declares ``readsLocalFiles``.

    The schema coerces this field away on load, because rejecting there would
    stop raven starting for anyone whose stored config the older web form wrote
    (see ``_drop_declared_local_file_access``). Coercing silently on a *write* is
    a different matter: the caller is holding the value and can be told.

    Deliberately not called from ``set_third_party_subagents``. That function is
    also how ``add_`` / ``remove_third_party_subagent`` re-write entries they
    read back raw from disk, so a legacy ``true`` sitting in an unrelated entry
    would make every later add or remove fail -- the same trap, one layer down.
    Callers that own an incoming payload (the RPC handlers) call this first.
    """
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "openai":
            continue
        # Either spelling reaches here: the schema accepts both, so a payload
        # that skipped the camel alias must not skip the check with it.
        if entry.get("readsLocalFiles") or entry.get("reads_local_files"):
            name = entry.get("name") or "<unnamed>"
            raise ValueError(
                f"readsLocalFiles is not supported for kind 'openai' (sub-agent {name!r}): the "
                "backend has no tools and no filesystem access, so nothing can open a path on "
                "this machine; remove the field or set it false"
            )


def reject_unsupported_acp_fields(entries: list[dict]) -> None:
    """Raise on an incoming acp entry that declares a cli-only field.

    Same split as ``reject_unsupported_openai_fields`` and for the same reason: the
    schema only warns on load, because raising there would stop raven starting for
    anyone whose stored config carries the field, and the UI that could fix it sits
    behind the config that no longer loads. On a *write* the caller is holding the
    value and can be told.

    Each of these is a cli *declaration* about behaviour whose acp counterpart is
    negotiated in the ``initialize`` handshake. Accepting both would make every one
    a second source of truth, and nothing in the code would know which to believe
    when they disagreed.

    Deliberately not called from ``set_third_party_subagents``, again matching the
    openai rejector: that function is also how ``add_`` / ``remove_`` re-write
    entries they read back raw from disk, so one legacy field in an unrelated entry
    would make every later add or remove fail.
    """
    from raven.config.schema import ACP_PROMPT_PLACEHOLDERS, ACP_UNSUPPORTED_FIELDS

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "acp":
            continue
        name = entry.get("name") or "<unnamed>"
        # Both spellings reach here: the schema accepts either, so a payload that
        # skipped the camel alias must not skip the check with it.
        declared = sorted(
            {spelling for field in ACP_UNSUPPORTED_FIELDS for spelling in (field, to_camel(field)) if spelling in entry}
        )
        if declared:
            raise ValueError(
                f"{declared} not supported for kind 'acp' (sub-agent {name!r}): an acp agent reports "
                "these in its initialize handshake, so a declared value here would be a second, "
                "unverifiable source of truth; remove the field(s)"
            )
        command = entry.get("command")
        found = [p for p in ACP_PROMPT_PLACEHOLDERS if isinstance(command, str) and p in command]
        if found:
            raise ValueError(
                f"command for acp sub-agent {name!r} contains {found}: an acp command starts a "
                "server, not one task -- the task is delivered as a session/prompt request, so the "
                "placeholder would be passed through literally and the handshake would fail"
            )


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
    "reject_unsupported_acp_fields",
    "reject_unsupported_openai_fields",
]
