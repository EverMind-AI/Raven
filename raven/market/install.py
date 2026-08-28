"""Atomic install / uninstall / toggle for market plugins.

An install interprets a catalog entry's ``contributes`` list, lands each
piece, then writes the ledger. Any piece failing rolls back the pieces
already landed — no half-installed plugins. Uninstall replays the ledger
in reverse. Neither touches live MCP connections; the RPC layer drives the
connection manager after the disk transaction commits.

Config writes go to the same ``config.json`` the loader reads, using the
camelCase key shape the file already uses (``tools.mcpServers``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import atomic_update


class PlugInstallError(Exception):
    """A user-visible install/uninstall failure (already installed, bad form
    input, name collision with a manual server, …)."""


# ── config.json plumbing ───────────────────────────────────────────


def _config_path() -> Path:
    from raven.config.loader import get_config_path

    return get_config_path()


def _parse_config(current: str | None) -> dict:
    if current is None:
        return {}
    try:
        return json.loads(current)
    except json.JSONDecodeError as e:
        # Refuse the read-modify-write: continuing would clobber whatever the
        # user has in the malformed file.
        raise PlugInstallError(f"config.json is not valid JSON ({e}); fix it before installing plugins") from e


def _read_config_raw() -> dict:
    try:
        current = _config_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    return _parse_config(current)


def _dump_config(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def _servers(payload: dict) -> dict:
    return payload.setdefault("tools", {}).setdefault("mcpServers", {})


# ── piece: mcp ─────────────────────────────────────────────────────


def _render_field(cfg: dict, field: dict, form: dict) -> None:
    key = str(field.get("key") or "")
    value = str(form.get(key) or "").strip()
    if not value:
        if field.get("optional"):
            return
        raise PlugInstallError(f"missing required field '{key}'")
    template = str(field.get("template") or "{value}")
    rendered = template.replace("{value}", value)
    into = str(field.get("into") or "")
    if into.startswith("headers."):
        cfg.setdefault("headers", {})[into[len("headers.") :]] = rendered
    elif into.startswith("env."):
        cfg.setdefault("env", {})[into[len("env.") :]] = rendered
    else:
        raise PlugInstallError(f"catalog field '{key}' has unsupported target '{into}'")


def _build_mcp_config(contrib: dict, form: dict) -> dict:
    """Catalog connection template + form secrets -> a camelCase config
    stanza, validated through MCPServerConfig before it ever hits disk."""
    from raven.config.schema import MCPServerConfig
    from raven.market.trust import HubTrustError, validate_mcp_connection

    cfg: dict[str, Any] = dict(contrib.get("connection") or {})
    cfg["auth"] = ((contrib.get("auth") or {}).get("mode")) or "none"
    endpoints = (contrib.get("auth") or {}).get("endpoints")
    if cfg["auth"] == "oauth" and isinstance(endpoints, dict):
        cfg["oauth"] = dict(endpoints)
    for field in (contrib.get("auth") or {}).get("fields") or []:
        _render_field(cfg, field, form)
    # After rendering, not before: a field's `into` target lands in the same
    # env/headers maps, so one check covers both what the entry declared and what
    # its form wrote.
    try:
        validate_mcp_connection(cfg)
    except HubTrustError as e:
        raise PlugInstallError(str(e)) from e
    validated = MCPServerConfig.model_validate(cfg)
    dumped = validated.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    if validated.auth != "none":
        dumped["auth"] = validated.auth
    return dumped


def _install_mcp_piece(entry_id: str, contrib: dict, form: dict) -> tuple[dict, Any]:
    # The collision check and the write share one locked transaction: checked
    # against a config another installer already changed, the check answers for
    # a file that no longer exists.
    def _add(current: str | None) -> tuple[str | None, None]:
        payload = _parse_config(current)
        servers = _servers(payload)
        if entry_id in servers:
            raise PlugInstallError(
                f"an MCP server named '{entry_id}' already exists in tools.mcpServers; remove it first"
            )
        servers[entry_id] = _build_mcp_config(contrib, form)
        return _dump_config(payload), None

    atomic_update(_config_path(), _add)
    piece = {"kind": "mcp", "server": entry_id}
    return piece, None


def _undo_mcp_piece(piece: dict) -> None:
    from raven.mcp.oauth import delete_credentials

    server = piece["server"]

    def _drop(current: str | None) -> tuple[str | None, None]:
        payload = _parse_config(current)
        servers = _servers(payload)
        if server not in servers:
            return None, None
        del servers[server]
        return _dump_config(payload), None

    atomic_update(_config_path(), _drop)
    delete_credentials(server)


# ── piece: skill (delegates to the SkillHub machinery) ─────────────


async def _install_skill_piece(contrib: dict) -> tuple[dict, Any]:
    hub_id = str(contrib.get("skillhub_id") or "")
    if not hub_id:
        raise PlugInstallError("catalog skill contribution carries no skillhub_id")
    from raven.rpc.methods.skillhub import skillhub_install

    # if_absent: undoing this piece deletes the skill directory, so the
    # transaction must only ever delete a directory it created. Without it, an
    # entry naming a skill the user already installed turns a failed install into
    # deletion of their copy.
    result = await skillhub_install({"id": hub_id}, if_absent=True)
    name = str(result.get("name") or "")
    return {"kind": "skill", "name": name, "skillhub_id": hub_id}, None


async def _undo_skill_piece(piece: dict) -> None:
    name = str(piece.get("name") or "")
    if not name:
        return
    from raven.rpc.methods.skillhub import skillhub_remove

    try:
        await skillhub_remove({"name": name})
    except Exception as e:  # noqa: BLE001 — best-effort rollback; report, don't mask the original error
        logger.warning("plughub: skill rollback for '{}' failed: {}", name, e)


# ── transaction ────────────────────────────────────────────────────


async def install_plugin(entry: dict, form: dict | None = None) -> dict:
    """Land every contribution of ``entry``; roll back on any failure.

    Returns the written ledger. Does not connect anything.
    """
    from raven.market.ledger import read_ledger, write_ledger

    entry_id = str(entry.get("id") or "")
    if not entry_id:
        raise PlugInstallError("catalog entry has no id")
    if read_ledger(entry_id) is not None:
        raise PlugInstallError(f"plugin '{entry_id}' is already installed")

    contributes = entry.get("contributes") or []
    if not contributes:
        raise PlugInstallError(f"catalog entry '{entry_id}' contributes nothing")

    form = form or {}
    done: list[dict] = []
    try:
        for contrib in contributes:
            kind = contrib.get("kind")
            if kind == "mcp":
                piece, _ = _install_mcp_piece(entry_id, contrib, form)
            elif kind == "skill":
                piece, _ = await _install_skill_piece(contrib)
            elif kind == "python":
                raise PlugInstallError("python plugins install via `uv tool install raven --with <pkg>` for now")
            else:
                raise PlugInstallError(f"unknown contribution kind '{kind}'")
            done.append(piece)
        # Inside the transaction: a plugin whose pieces landed but whose ledger
        # did not is worse than no install at all -- uninstall would read it as a
        # hand-written server, and a reinstall would refuse on the name it left
        # behind. Roll the pieces back and report the failure.
        write_ledger(entry_id, str(entry.get("version") or ""), done)
    except BaseException:
        for piece in reversed(done):
            try:
                if piece["kind"] == "mcp":
                    _undo_mcp_piece(piece)
                elif piece["kind"] == "skill":
                    await _undo_skill_piece(piece)
            except Exception as e:  # noqa: BLE001
                logger.warning("plughub: rollback of {} failed: {}", piece, e)
        raise

    return {"catalog_id": entry_id, "pieces": done}


async def uninstall_plugin(name: str) -> dict:
    """Remove a plugin: ledger replay for market installs, config+credential
    removal for manual servers. Does not touch live connections."""
    from raven.market.ledger import delete_ledger, read_ledger

    led = read_ledger(name)
    if led is not None:
        for piece in reversed(led.get("pieces") or []):
            if piece.get("kind") == "mcp":
                _undo_mcp_piece(piece)
            elif piece.get("kind") == "skill":
                await _undo_skill_piece(piece)
        delete_ledger(name)
        return {"removed": True, "origin": "market"}

    payload = _read_config_raw()
    servers = _servers(payload)
    if name not in servers:
        raise PlugInstallError(f"no installed plugin or MCP server named '{name}'")
    _undo_mcp_piece({"kind": "mcp", "server": name})
    return {"removed": True, "origin": "manual"}


def toggle_server(name: str, enabled: bool) -> None:
    """Flip the enabled flag of one configured MCP server (config only)."""

    def _set(current: str | None) -> tuple[str | None, None]:
        payload = _parse_config(current)
        servers = _servers(payload)
        if name not in servers:
            raise PlugInstallError(f"no MCP server named '{name}'")
        servers[name]["enabled"] = bool(enabled)
        return _dump_config(payload), None

    atomic_update(_config_path(), _set)


__all__ = ["PlugInstallError", "install_plugin", "toggle_server", "uninstall_plugin"]
