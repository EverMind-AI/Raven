"""Atomic write path for the ``tools.mcpServers`` config block.

Mirrors :mod:`raven.config.update_skills`: reads the raw block, validates each
entry against :class:`MCPServerConfig`, and writes the RAW patched dict back so
no unrelated key under ``tools`` is reset to a default.

``set_mcp_servers`` replaces the whole server map, the same contract
``raven.subagents.set`` uses -- the caller sends the list it wants to end up
with, so a removal is expressible.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.config.schema import MCPServerConfig

_FIELDS = ("type", "command", "args", "env", "url", "headers", "toolTimeout", "enabled", "auth")
# Both spellings of every field. ``MCPServerConfig`` accepts snake_case and
# camelCase alike (``populate_by_name``), so a filter that knows only one of
# them would drop a caller's value without saying so.
_KNOWN_KEYS = frozenset(MCPServerConfig.model_fields) | frozenset(_FIELDS) | {"name", "error"}

#: Either spelling of a field -> the one field it names. The carry-through
#: compares what the caller mentioned against what is on disk, and those two
#: sides can spell the same field differently: plughub writes ``by_alias``
#: (``toolTimeout``) while a caller may send ``tool_timeout``, which
#: ``_KNOWN_KEYS`` deliberately admits. Comparing raw keys made the two look
#: like different fields, so the stored one was carried alongside the caller's,
#: pydantic resolved the alias first, and the caller's value lost.
_CANONICAL_KEY = {
    spelling: field for field, info in MCPServerConfig.model_fields.items() for spelling in (field, info.alias or field)
}

#: Keys ``raven.mcp.list`` merges into each server from the live runtime. They
#: ride back in on every save because the panel round-trips that response, but
#: they describe one gateway run rather than configuration, so the write path
#: drops them -- otherwise the carry-through that exists to protect unknown
#: *future* keys is what persists a connection state true for a single instant.
#: ``raven.mcp.list`` is tested against this set, so a live key added there
#: without being listed here fails rather than quietly starting to leak.
MCP_RUNTIME_KEYS = frozenset({"connected", "tools"})
_NEVER_PERSISTED = MCP_RUNTIME_KEYS | {"name", "error"}


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _raw_config(path: Path) -> dict[str, Any]:
    return read_raw_or_raise(path) if path.exists() else {}


def _raw_block(cfg: dict) -> dict:
    tools = cfg.get("tools") or {}
    return dict(tools.get("mcpServers") or tools.get("mcp_servers") or {})


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err.get("loc") or ()) or "entry"
    return f"{where}: {err.get('msg') or 'invalid'}"


def _entry(name: str, raw: dict) -> dict:
    """One server as the web UI sees it: camelCase, defaults filled in.

    A server the schema rejects is reported with an ``error`` rather than
    raised: one hand-broken entry must not blank the whole panel, whose entire
    job is to show what is configured.
    """
    try:
        d = MCPServerConfig.model_validate(raw or {}).model_dump(by_alias=True)
    except ValidationError as exc:
        return {"name": name, **dict.fromkeys(_FIELDS), "error": _first_error(exc)}
    return {"name": name, **{k: d.get(k) for k in _FIELDS}}


def get_mcp_servers(*, config_path: Path | None = None) -> list[dict]:
    """Every configured MCP server. A malformed entry carries ``error``."""
    path = config_path or get_config_path()
    block = _raw_block(_raw_config(path))
    return [_entry(name, raw) for name, raw in block.items()]


def set_mcp_servers(servers: list[dict], *, config_path: Path | None = None) -> None:
    """Replace ``tools.mcpServers`` with ``servers``.

    Each entry needs a ``name`` plus either a ``command`` (stdio) or a ``url``
    (http/sse) -- a server with neither is silently skipped at connect time,
    which would look like a server that exists but never works. An entry the
    schema rejects fails the whole write, so a config hand-edited into an
    invalid state has to be fixed before the panel can save over it; that is
    deliberate, since the alternative is quietly dropping it.

    A key the caller does not mention is carried through from the entry already
    on disk, so a save cannot erase a field the client that sent it never knew
    about -- neither a key this build does not understand nor one it simply
    does not surface. :data:`MCP_RUNTIME_KEYS` is exempt from that
    carry-through: it is live state, not configuration.
    """
    path = config_path or get_config_path()
    data = _raw_config(path)
    if not isinstance(servers or [], list):
        raise ValueError("servers must be a list")
    existing = _raw_block(data)

    block: dict[str, Any] = {}
    for item in servers or []:
        if not isinstance(item, dict):
            raise ValueError(f"each MCP server must be an object, got {type(item).__name__}")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("every MCP server needs a name")
        if name in block:
            raise ValueError(f"duplicate MCP server name: {name!r}")
        # A stored value the caller did not mention AT ALL is carried through,
        # whether or not the schema knows the key. Restricting this to unknown
        # keys left a gap that a no-op save fell into: the panel round-trips the
        # response it was given, that response never carried ``auth`` or
        # ``enabled``, and neither did the carry-through -- so saving anything
        # dropped ``auth: oauth`` (orphaning a plugin's stored tokens, with no
        # way to re-authorize) and dropped ``enabled: false`` (silently turning
        # a shelved server back on).
        stored = existing.get(name) or {}
        mentioned = {_CANONICAL_KEY.get(k, k) for k in item}
        carried = {
            k: v for k, v in stored.items() if _CANONICAL_KEY.get(k, k) not in mentioned and k not in _NEVER_PERSISTED
        }
        sent = {k: v for k, v in item.items() if k in _KNOWN_KEYS and k not in _NEVER_PERSISTED and v is not None}
        raw = {**{k: v for k, v in carried.items() if k in _KNOWN_KEYS}, **sent}
        cfg = MCPServerConfig.model_validate(raw)  # raises on a bad type / field
        if not cfg.command and not cfg.url:
            raise ValueError(f"MCP server {name!r} needs a command (stdio) or a url (http/sse)")
        # Scrubbed on the way out of the file too, not just on the way in: a
        # config an earlier build already polluted heals on its next save
        # instead of carrying the stale state forward forever.
        unknown = {k: v for k, v in carried.items() if k not in _KNOWN_KEYS}
        unknown.update({k: v for k, v in item.items() if k not in _KNOWN_KEYS and k not in _NEVER_PERSISTED})
        block[name] = {**unknown, **cfg.model_dump(by_alias=True, exclude_defaults=True)}

    tools = dict(data.get("tools") or {})
    # Write under whichever spelling the file already uses, so a config written
    # in snake_case does not silently gain a second, shadowing camelCase block.
    key = "mcp_servers" if "mcp_servers" in tools and "mcpServers" not in tools else "mcpServers"
    tools[key] = block
    # Drop the other spelling: leaving it behind means the servers just deleted
    # here come back the moment someone removes the block that shadows them.
    tools.pop("mcp_servers" if key == "mcpServers" else "mcpServers", None)
    data["tools"] = tools
    _write_atomic(path, data)


__all__ = ["MCP_RUNTIME_KEYS", "get_mcp_servers", "set_mcp_servers"]
