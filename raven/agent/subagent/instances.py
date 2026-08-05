"""Persistent instance registry for stateful third-party CLI subagents.

Maps ``(session_key, agent, handle)`` to the CLI's own session id so a later
spawn with the same handle resumes instead of starting fresh. Stored as a list
rather than a keyed object so an arbitrary handle needs no key escaping.

A record's lifetime is its chat session's: there is no time-based expiry, and
``delete_session`` (called when the session is deleted) is what bounds the file.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

_FILENAME = "subagent_instances.json"

_Key = tuple[str, str, str]


def default_registry_path() -> Path:
    from raven.config.loader import get_config_path

    return get_config_path().parent / _FILENAME


class InstanceRegistry:
    """Handle-to-session-id store backed by one JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_registry_path()
        self._lock = asyncio.Lock()
        self._records: dict[_Key, dict[str, Any]] | None = None

    def _load(self) -> dict[_Key, dict[str, Any]]:
        if self._records is not None:
            return self._records
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        records: dict[_Key, dict[str, Any]] = {}
        for rec in (raw.get("instances") if isinstance(raw, dict) else None) or []:
            if not isinstance(rec, dict):
                continue
            key = (rec.get("sessionKey"), rec.get("agent"), rec.get("handle"))
            if not all(isinstance(part, str) and part for part in key):
                continue
            if "kind" not in rec:
                rec["kind"] = "cli"
            records[key] = rec  # type: ignore[index]
        self._records = records
        return records

    def _flush(self) -> None:
        assert self._records is not None  # noqa: S101
        data = {"version": 1, "instances": list(self._records.values())}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    async def lookup(self, session_key: str, agent: str, handle: str) -> str | None:
        async with self._lock:
            rec = self._load().get((session_key, agent, handle))
        if not rec or rec.get("kind") != "cli":
            return None
        return rec.get("agentId")

    async def commit(self, session_key: str, agent: str, handle: str, agent_id: str) -> None:
        async with self._lock:
            records = self._load()
            key = (session_key, agent, handle)
            now = int(time.time() * 1000)
            records[key] = {
                "kind": "cli",
                "sessionKey": session_key,
                "agent": agent,
                "handle": handle,
                "agentId": agent_id,
                "createdAtMs": (records.get(key) or {}).get("createdAtMs", now),
                "updatedAtMs": now,
            }
            try:
                self._flush()
            except OSError as e:  # noqa: BLE001 - persistence is best-effort
                logger.warning(
                    "subagent instance registry write failed (mapping live in-process, will not survive restart): {}", e
                )

    async def upsert_spawn(
        self, session_key: str, agent: str, handle: str, status: str, agent_id: str | None = None
    ) -> None:
        """Record or update one third-party CLI spawn's status.

        A row's status is one of ``running`` | ``completed`` | ``failed`` |
        ``cancelled``. ``agent_id`` carries forward from any existing record
        when not given, so a status write after a successful create does not
        erase the session id ``commit`` already stored for that handle.
        """
        async with self._lock:
            records = self._load()
            key = (session_key, agent, handle)
            now = int(time.time() * 1000)
            existing = records.get(key) or {}
            records[key] = {
                "kind": "cli",
                "sessionKey": session_key,
                "agent": agent,
                "handle": handle,
                "status": status,
                "agentId": agent_id or existing.get("agentId"),
                "createdAtMs": existing.get("createdAtMs", now),
                "updatedAtMs": now,
            }
            try:
                self._flush()
            except OSError as e:  # noqa: BLE001 - persistence is best-effort
                logger.warning(
                    "subagent instance registry write failed (spawn status live in-process, "
                    "will not survive restart): {}",
                    e,
                )

    async def upsert_dag_node(self, session_key: str, run_id: str, node_id: str, agent: str, status: str) -> None:
        """Record or update one DAG node's status.

        The handle is namespaced by run id so a node can never collide with a
        user-chosen CLI instance handle.
        """
        handle = f"{run_id}/{node_id}"
        async with self._lock:
            records = self._load()
            key = (session_key, agent, handle)
            now = int(time.time() * 1000)
            existing = records.get(key) or {}
            records[key] = {
                "kind": "dag-node",
                "sessionKey": session_key,
                "agent": agent,
                "handle": handle,
                "runId": run_id,
                "nodeId": node_id,
                "status": status,
                "createdAtMs": existing.get("createdAtMs", now),
                "updatedAtMs": now,
            }
            try:
                self._flush()
            except OSError as e:  # noqa: BLE001 - persistence is best-effort
                logger.warning(
                    "subagent instance registry write failed (dag node state live in-process, "
                    "will not survive restart): {}",
                    e,
                )

    async def forget(self, session_key: str, agent: str, handle: str) -> bool:
        """Drop one record (e.g. after its CLI-side session was pruned). Returns
        whether a record was actually removed."""
        async with self._lock:
            records = self._load()
            key = (session_key, agent, handle)
            if key not in records:
                return False
            del records[key]
            try:
                self._flush()
            except OSError as e:  # noqa: BLE001 - persistence is best-effort
                logger.warning(
                    "subagent instance registry write failed (record dropped in-process, remains on disk): {}", e
                )
            return True

    async def delete_session(self, session_key: str) -> int:
        """Drop every record for a deleted chat session. Returns the count removed."""
        async with self._lock:
            records = self._load()
            doomed = [k for k in records if k[0] == session_key]
            for key in doomed:
                del records[key]
            if doomed:
                try:
                    self._flush()
                except OSError as e:  # noqa: BLE001 - persistence is best-effort
                    logger.warning(
                        "subagent instance registry write failed (records dropped in-process, remain on disk): {}", e
                    )
            return len(doomed)

    def list_instances(self, session_key: str | None = None) -> list[dict[str, Any]]:
        """Most-recently-used first, optionally scoped to one session."""
        records = list(self._load().values())
        if session_key is not None:
            records = [r for r in records if r.get("sessionKey") == session_key]
        return sorted(records, key=lambda r: r.get("updatedAtMs") or 0, reverse=True)


_registry: InstanceRegistry | None = None


def get_registry() -> InstanceRegistry:
    """Process-wide registry, so the gateway's RPC reads what the backend wrote."""
    global _registry
    if _registry is None:
        _registry = InstanceRegistry()
    return _registry


__all__ = ["InstanceRegistry", "get_registry", "default_registry_path"]
