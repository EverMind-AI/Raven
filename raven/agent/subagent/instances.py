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
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

_FILENAME = "subagent_instances.json"

_Key = tuple[str, str, str]

_SLUG_SEPARATORS_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_CHARS = 32


def _slug(seed: str) -> str:
    return _SLUG_SEPARATORS_RE.sub("-", seed.lower()).strip("-")[:_MAX_SLUG_CHARS].strip("-")


def mint_handle(seed: str, *, fallback: str = "agent") -> str:
    """A fresh handle for a stateful call that named no instance.

    The six hex characters are what make it addressable rather than merely
    unique: a handle repeated across two DAG runs would otherwise share one
    session between graphs that asked for nothing of the sort, and a handle
    equal to a name the model might also choose would shadow it. Reading the
    slug is the only reason the seed is carried at all -- nothing keys on it.
    ``fallback`` keeps the handle readable when the seed itself slugifies to
    nothing, which a non-Latin label does routinely rather than as an edge
    case; it is slugified the same way, and ``"agent"`` remains the last
    resort when the fallback reduces to nothing too.
    """
    slug = _slug(seed) or _slug(fallback) or "agent"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def default_registry_path() -> Path:
    from raven.config.loader import get_config_path

    return get_config_path().parent / _FILENAME


def _dag_origin(existing: dict[str, Any]) -> dict[str, Any]:
    """The DAG node this handle belongs to, carried forward across a rewrite.

    Every writer below rebuilds its record wholesale, so a status write or a
    re-``commit`` would drop the link ``link_dag_node`` stamped -- and the link
    is the only thing that can pair a minted handle back to its node:
    ``mint_handle`` says outright that nothing keys on the slug it derives.
    """
    origin = {k: existing[k] for k in ("runId", "nodeId") if existing.get(k)}
    return origin


class InstanceRegistry:
    """Handle-to-session-id store backed by one JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_registry_path()
        self._lock = asyncio.Lock()
        self._records: dict[_Key, dict[str, Any]] | None = None
        self._stamp: tuple[int, int] | None = None

    def _file_stamp(self) -> tuple[int, int] | None:
        """What the file looked like when we last read or wrote it."""
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _load(self) -> dict[_Key, dict[str, Any]]:
        """Records as they are on disk, re-read whenever the file has moved on.

        This store is shared by processes, not owned by one: a sub-agent spawn
        is its own ``cli`` process and registers itself there, while the gateway
        serving the UI is a different process entirely. Caching the file for the
        lifetime of a process meant the gateway answered every later question
        from the snapshot it happened to load at startup -- so a session's own
        sub-agents were invisible to the panel that exists to list them, and
        stayed invisible until the gateway was restarted.

        It also bounds the damage a concurrent writer can do: ``_flush`` writes
        the whole cached map back, so flushing from a stale cache would drop
        every record another process had added in the meantime.
        """
        stamp = self._file_stamp()
        if self._records is not None and stamp == self._stamp:
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
        # Stamped from the stat taken BEFORE the read: a writer that lands
        # between the two is then seen as a change on the next call rather than
        # being stamped as already-loaded and skipped forever.
        self._stamp = stamp
        return records

    def _flush(self) -> None:
        assert self._records is not None  # noqa: S101
        data = {"version": 1, "instances": list(self._records.values())}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)
        # Our own write is not a reason to re-read on the next call.
        self._stamp = self._file_stamp()

    async def lookup(self, session_key: str, agent: str, handle: str, *, kind: str = "cli") -> str | None:
        """The transport session id bound to this handle, or ``None``.

        ``kind`` is matched, not ignored: the same key space also holds status
        rows (``dag-node``), and a handle bound over one transport is meaningless
        over another -- a cli session id cannot be resumed by ``session/load``.
        Defaults to ``cli`` so every existing caller keeps its behaviour.
        """
        async with self._lock:
            rec = self._load().get((session_key, agent, handle))
        if not rec or rec.get("kind") != kind:
            return None
        return rec.get("agentId")

    async def commit(self, session_key: str, agent: str, handle: str, agent_id: str, *, kind: str = "cli") -> None:
        async with self._lock:
            records = self._load()
            key = (session_key, agent, handle)
            now = int(time.time() * 1000)
            existing = records.get(key) or {}
            records[key] = {
                "kind": kind,
                "sessionKey": session_key,
                "agent": agent,
                "handle": handle,
                "agentId": agent_id,
                "createdAtMs": existing.get("createdAtMs", now),
                "updatedAtMs": now,
                **_dag_origin(existing),
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

        A row's status is one of ``idle`` | ``running`` | ``completed`` |
        ``failed`` | ``cancelled``. ``idle`` is an instance the user created and
        has not addressed yet, and it is deliberately not one of the two
        ``reconcile_instance_rows`` rewrites: it is true across a restart, where
        an unfinished ``running`` is not. ``agent_id`` carries forward from any existing record
        when not given, so a status write after a successful create does not
        erase the session id ``commit`` already stored for that handle.
        """
        async with self._lock:
            records = self._load()
            key = (session_key, agent, handle)
            now = int(time.time() * 1000)
            existing = records.get(key) or {}
            records[key] = {
                # Carried forward, not hardcoded: a status write shares this key
                # with the binding `commit` made, so stamping "cli" here would
                # retype an acp binding and `lookup` would then refuse to find it.
                "kind": existing.get("kind") or "cli",
                "sessionKey": session_key,
                "agent": agent,
                "handle": handle,
                "status": status,
                "agentId": agent_id or existing.get("agentId"),
                "createdAtMs": existing.get("createdAtMs", now),
                "updatedAtMs": now,
                **_dag_origin(existing),
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

    async def link_dag_node(self, session_key: str, agent: str, handle: str, run_id: str, node_id: str) -> None:
        """Mark the instance a DAG node is running on as belonging to that node.

        A node's own status lives on its ``dag-node`` row, keyed by
        ``<run_id>/<node_id>``; when its sub-agent is stateful the node *also*
        runs on an ordinary handle, which the backend commits under separately.
        Nothing in either row says they are the same piece of work, and the
        handle cannot supply it -- ``mint_handle`` derives a readable slug from
        the node id but promises nothing keys on it. So the pairing is recorded
        here, by the one caller that holds both halves.

        Written before the node dispatches, so the row exists even if the node
        never reaches ``commit``: an instance that failed on its first turn is
        still the node's, and a reader that saw only the ``dag-node`` row would
        report the graph twice.
        """
        async with self._lock:
            records = self._load()
            key = (session_key, agent, handle)
            now = int(time.time() * 1000)
            existing = records.get(key) or {}
            if existing.get("runId") == run_id and existing.get("nodeId") == node_id:
                return
            records[key] = {
                # `kind` is carried, never stamped: this may run before the
                # backend has committed its binding, and typing the row here
                # would make `lookup` refuse the acp id that arrives later.
                **existing,
                "kind": existing.get("kind") or "cli",
                "sessionKey": session_key,
                "agent": agent,
                "handle": handle,
                "runId": run_id,
                "nodeId": node_id,
                "createdAtMs": existing.get("createdAtMs", now),
                "updatedAtMs": now,
            }
            try:
                self._flush()
            except OSError as e:  # noqa: BLE001 - persistence is best-effort
                logger.warning(
                    "subagent instance registry write failed (dag node link live in-process, "
                    "will not survive restart): {}",
                    e,
                )

    async def unbind(self, session_key: str, agent: str, handle: str) -> bool:
        """Drop only the transport session id, keeping the instance itself.

        What a failed resume actually learned is that this ``agentId`` is stale,
        not that the instance stopped existing -- and the instance is what the
        chip strip and ``/instance`` list are drawn from. Deleting the row
        instead took the instance off screen for the length of the turn that was
        recovering it, and permanently if that turn then failed, since only a
        *successful* turn commits the replacement id.
        """
        async with self._lock:
            records = self._load()
            key = (session_key, agent, handle)
            rec = records.get(key)
            if rec is None or rec.get("agentId") is None:
                return False
            records[key] = {**rec, "agentId": None, "updatedAtMs": int(time.time() * 1000)}
            try:
                self._flush()
            except OSError as e:  # noqa: BLE001 - persistence is best-effort
                logger.warning(
                    "subagent instance registry write failed (binding dropped in-process, remains on disk): {}", e
                )
            return True

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


def reconcile_instance_rows(
    rows: list[dict[str, Any]],
    *,
    live_handles: "Callable[[str], set[tuple[str, str]]]",
    active_run_ids: "Callable[[], set[str]]",
) -> list[dict[str, Any]]:
    """Rewrite rows the process index says are not actually running.

    The registry is durable and the task index is not, so a gateway that was
    killed mid-spawn leaves rows reading ``running`` forever. Whether one is
    genuinely live is answered by a different index per kind: a spawn (``cli``,
    which is also what a built-in or HTTP instance is recorded as) by the
    manager's in-flight handles, a ``dag-node`` by its run still being active.
    A kind with neither is left alone rather than guessed at.

    Shared by the TUI and the web RPC rather than derived twice: two surfaces
    disagreeing about which instances are live is the hardest kind of bug to
    find later. The two indexes arrive as callables so this module stays free of
    the manager and the DAG tool -- neither of which the registry knows about.

    Never mutates a row: the registry hands out its cached records, and a
    rewrite in place would be read back on the next call as if it had come from
    disk.
    """
    live_by_session: dict[str, set[tuple[str, str]]] = {}
    runs: set[str] | None = None
    out: list[dict[str, Any]] = []
    for row in rows:
        kind = row.get("kind")
        if kind == "dag-node" and row.get("status") in ("pending", "running"):
            if runs is None:
                runs = active_run_ids()
            if row.get("runId") not in runs:
                out.append({**row, "status": "interrupted"})
                continue
        elif kind in ("cli", "acp") and row.get("status") in ("pending", "running"):
            # An acp row answers to the same in-flight index as a cli spawn:
            # a bound server whose process died leaves no live handle, and
            # without this branch such a row read "running" forever.
            session_key = row.get("sessionKey", "")
            if session_key not in live_by_session:
                live_by_session[session_key] = live_handles(session_key)
            if (row.get("agent"), row.get("handle")) not in live_by_session[session_key]:
                out.append({**row, "status": "interrupted"})
                continue
        out.append(row)
    return out


_registry: InstanceRegistry | None = None


def get_registry() -> InstanceRegistry:
    """Process-wide registry, so the gateway's RPC reads what the backend wrote."""
    global _registry
    if _registry is None:
        _registry = InstanceRegistry()
    return _registry


@dataclass
class _HeldLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_handle_locks: dict[_Key, _HeldLock] = {}

# Which task currently holds each key, so a nested acquire from that same task
# passes straight through. Without this, a caller that takes the lock and then
# calls a backend that takes it again deadlocks on a plain asyncio.Lock: it is
# waiting for itself, with no timeout and nothing logged.
_handle_owners: dict[_Key, "asyncio.Task[Any]"] = {}


@asynccontextmanager
async def hold_handle(session_key: str, agent: str, handle: str) -> AsyncIterator[None]:
    """Serialize everything that resumes one stateful instance handle.

    A handle maps to a single session inside the CLI's own store; two runs
    resuming it at once interleave that session's transcript or corrupt it.
    Process-wide and keyed by the handle rather than held on a backend object,
    because the DAG tool and the sub-agent manager build *separate* backends
    from the same config -- a per-backend lock would let a spawn and a DAG node
    resume the same session side by side.

    The refcount is taken before the lock is awaited, so a waiter keeps the
    entry alive and the map never strands a lock two callers disagree about.

    Re-entrant within one task, and it has to be: the direct-chat path takes
    this around a whole turn, and the cli backend takes it again around its own
    lookup-run-commit sequence. Both are right to -- each is the outermost hold
    on some other path -- and the two together are still one resumption, which
    is what the lock protects. Non-reentrant, that pair is a deadlock with no
    error and no timeout: the turn's record simply stays ``running`` forever.
    """
    key = (session_key, agent, handle)
    current = asyncio.current_task()

    if current is not None and _handle_owners.get(key) is current:
        yield
        return

    entry = _handle_locks.get(key)
    if entry is None:
        entry = _handle_locks[key] = _HeldLock()
    entry.users += 1
    try:
        async with entry.lock:
            _handle_owners[key] = current  # type: ignore[assignment]
            try:
                yield
            finally:
                _handle_owners.pop(key, None)
    finally:
        entry.users -= 1
        if entry.users == 0:
            _handle_locks.pop(key, None)


__all__ = ["InstanceRegistry", "get_registry", "default_registry_path", "hold_handle"]
