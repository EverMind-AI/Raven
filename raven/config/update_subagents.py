"""Atomic write path for ``subagents.agents`` config (req5 / P4).

The only supported write path for agent config. Every entry point (CLI, the
WebUI "configure Raven" page) must go through here. Entries are validated against
the schema (discriminated on ``kind``) before anything is written, and the write
is atomic (temp file + os.replace).

Written under the ``agents`` key; a config still holding the old ``thirdParty``
one is read and migrated on the next write, so the two never coexist.

The built-in rows are *not* written here. They are package seeds
(``raven.agent.subagent.builtin_agents``), and a caller may store an override row
for one -- but not change its ``kind``, and not remove it, since "absent" is what
"use the package's row" means. Both are refused below.

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

# The key written. ``SubagentsConfig`` reads the two older spellings as well, so
# a stored config keeps loading; writing only this one is what retires them.
_ALIAS = "agents"
_LEGACY_ALIASES = ("thirdParty", "third_party")


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

    Deliberately not called from ``set_agents``. That function is
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

    Deliberately not called from ``set_agents``, again matching the
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
    for key in (_ALIAS, *_LEGACY_ALIASES):
        if sub.get(key):
            return sub[key]
    return []


def reject_builtin_transport_changes(entries: list[dict]) -> None:
    """Raise when an incoming entry would rewrite a built-in row's transport.

    A built-in agent is an in-process raven loop; its row exists whether or not
    config mentions one, and an override may retune what it can reach (``skills``,
    ``tools``, ``model``) or take it off the roster (``enabled: false``). What it
    may not do is claim the name for another transport: that leaves the built-in
    agent unreachable under a name every stored playbook and instance record
    already points at.

    A *removal* needs no check here -- writing no row for a seed is exactly how
    "use the package's default" is spelled, so it is not a delete.
    """
    from raven.agent.subagent.builtin_agents import BUILTIN_AGENT_NAMES

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if name in BUILTIN_AGENT_NAMES and entry.get("kind") not in (None, "builtin"):
            raise ValueError(
                f"sub-agent {name!r} is a built-in agent, so it cannot be redeclared as kind "
                f"{entry.get('kind')!r}: the built-in one would become unreachable under a name "
                "playbooks and instance records already use. Choose another name for this entry"
            )


def get_agents(*, config_path: Path | None = None) -> list[dict]:
    """Return the configured agents as validated dicts (by alias).

    Config rows only -- the package's built-in seed rows are not in config and do
    not appear here. A caller that wants the whole table reads
    ``AgentRegistry.rows()``; this is the *editable* set, which is what a config
    surface must show so that "delete" means something.

    Raises ``ValidationError`` if the on-disk section is malformed.
    """
    path = config_path or get_config_path()
    validated = SubagentsConfig(agents=_raw_entries(path))
    return [cfg.model_dump(by_alias=True) for cfg in validated.agents]


def set_agents(entries: list[dict], *, config_path: Path | None = None) -> None:
    """Replace the whole ``subagents.agents`` list (validate-then-write).

    Raises ``ValidationError`` if any entry violates the schema, or ``ValueError``
    on duplicate names or a built-in row redeclared under another transport --
    nothing is written in any of those cases.

    Duplicates are a hard error here while the load path only warns and keeps the
    first: on load, refusing would take the whole ``Config`` down and raven would
    stop starting behind the very config a user needs the UI to fix. Here the
    caller is holding the value and can be told.
    """
    reject_builtin_transport_changes([e for e in entries if isinstance(e, dict)])
    # Counted on the incoming entries, not on the validated model: the schema
    # deduplicates on load (keeping the first, so a hand-edited config still
    # starts), which means by then there is only one of each and this check would
    # never fire. The whole point of it is that a caller writing two gets told.
    names = [e.get("name") for e in entries if isinstance(e, dict)]
    dupes = {n for n in names if n is not None and names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate sub-agent name(s): {sorted(dupes)}")
    validated = SubagentsConfig(agents=entries)
    dumped = validated.model_dump(by_alias=True)[_ALIAS]

    path = config_path or get_config_path()
    data = _raw_config(path)
    data.setdefault("subagents", {})
    # Drop the older spellings so a config never carries two competing lists.
    for legacy in _LEGACY_ALIASES:
        data["subagents"].pop(legacy, None)
    data["subagents"][_ALIAS] = dumped
    _write_atomic(path, data)
    logger.info("update_subagents: wrote {} sub-agent(s)", len(dumped))


def add_agent(entry: dict, *, config_path: Path | None = None) -> None:
    """Add or replace (by name) one agent."""
    path = config_path or get_config_path()
    name = entry.get("name")
    kept = [e for e in _raw_entries(path) if not (isinstance(e, dict) and e.get("name") == name)]
    kept.append(entry)
    set_agents(kept, config_path=path)


def remove_agent(name: str, *, config_path: Path | None = None) -> bool:
    """Remove an agent's config row by name. Returns True if one was removed.

    Removing an *override* of a built-in row is allowed and means "go back to the
    package's row" -- the agent itself stays on the table, which is why nothing
    here guards the built-in names.
    """
    path = config_path or get_config_path()
    current = _raw_entries(path)
    kept = [e for e in current if not (isinstance(e, dict) and e.get("name") == name)]
    if len(kept) == len(current):
        return False
    set_agents(kept, config_path=path)
    return True


# Pre-``agents`` spellings, kept as names only.
get_third_party_subagents = get_agents
set_third_party_subagents = set_agents
add_third_party_subagent = add_agent
remove_third_party_subagent = remove_agent


__all__ = [
    "add_agent",
    "add_third_party_subagent",
    "get_agents",
    "get_third_party_subagents",
    "reject_builtin_transport_changes",
    "reject_unsupported_acp_fields",
    "reject_unsupported_openai_fields",
    "remove_agent",
    "remove_third_party_subagent",
    "set_agents",
    "set_third_party_subagents",
]
