"""Which Mirasim conversation each ACP session continues, kept on disk.

The two ids are minted at different moments. ACP needs a session id when the
session opens, while Mirasim names its conversation only when the host takes
its first prompt (``accepted``). So the adapter mints the ACP id and records the
Mirasim key against it once the host reports one. A conversation the user began
in Mirasim itself goes by its own key, which :meth:`BindingStore.by_key` is how
``session/list`` tells apart from one this adapter recorded.

On disk rather than in memory, because Raven resumes an instance with
``session/load`` on whatever adapter process it reaches next: a restarted
adapter, or a second one Raven launched for another binding, has to find the
same conversation. One file per Mirasim agent, written under the cross-process
lock in :mod:`raven.utils.atomic_io`, since those processes write it
concurrently.

Only what continuing needs is kept: the conversation and the directory it
works in. The model and effort a session was last put on are not, because
Raven's client re-asserts its own choice on every route into a session and
keeps none across its own restart -- an adapter restoring that choice would
leave the session answering on a model every Raven surface reports as cleared.
Nor is its route: every turn names the route its model choice runs on, and the
host keeps it.

A record is dropped once it has gone :data:`RETENTION_DAYS` without a turn,
and at once on ``session/delete``, which Raven sends when a person forgets the
instance and which deletes the Mirasim conversation too. A record that merely
expired leaves the conversation in Mirasim's own history.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from raven.home import raven_home
from raven.utils.atomic_io import atomic_update

STORE_VERSION = 1
RETENTION_DAYS = 90
_RETENTION_MS = RETENTION_DAYS * 24 * 3600 * 1000


def store_path(agent: str) -> Path:
    """Where ``agent``'s records live, under Raven's own home."""
    return store_dir() / f"{agent}.json"


def store_dir() -> Path:
    """The directory the records of every Mirasim agent live in."""
    return raven_home() / "subagent_sessions" / "mirasim"


@dataclass
class Binding:
    """One ACP session, as much of it as a later process needs to continue it."""

    session_id: str
    cwd: str
    mirasim_key: str | None = None
    used_at_ms: int = 0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode(raw: str | None, path: Path) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        logger.warning("Mirasim session store {} is not JSON ({}); treating it as empty", path, exc)
        return {}
    sessions = doc.get("sessions") if isinstance(doc, dict) else None
    return {key: row for key, row in sessions.items() if isinstance(row, dict)} if isinstance(sessions, dict) else {}


def _encode(sessions: dict[str, dict[str, Any]]) -> str:
    return json.dumps({"version": STORE_VERSION, "sessions": sessions}, ensure_ascii=False, indent=1)


def _binding(session_id: str, row: dict[str, Any]) -> Binding | None:
    cwd = row.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    key = row.get("mirasimKey")
    used = row.get("usedAtMs")
    return Binding(
        session_id=session_id,
        cwd=cwd,
        mirasim_key=key if isinstance(key, str) and key else None,
        used_at_ms=used if isinstance(used, int) else 0,
    )


class BindingStore:
    """Read, write and forget :class:`Binding` records in one agent's file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, session_id: str) -> Binding | None:
        """The live record for ``session_id``, or ``None`` when absent or past retention."""
        return self._live().get(session_id)

    def by_key(self) -> dict[str, str]:
        """Each live record's Mirasim conversation, mapped to the ACP session that continues it."""
        return {binding.mirasim_key: sid for sid, binding in self._live().items() if binding.mirasim_key}

    def _live(self) -> dict[str, Binding]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        horizon = _now_ms() - _RETENTION_MS
        live: dict[str, Binding] = {}
        for session_id, row in _decode(raw, self.path).items():
            binding = _binding(session_id, row)
            if binding is not None and binding.used_at_ms >= horizon:
                live[session_id] = binding
        return live

    def put(self, binding: Binding) -> None:
        """Write ``binding``, stamped as used now, and drop every record past retention."""
        binding.used_at_ms = _now_ms()
        horizon = binding.used_at_ms - _RETENTION_MS

        def update(raw: str | None) -> tuple[str, None]:
            kept = {
                key: row
                for key, row in _decode(raw, self.path).items()
                if isinstance(row.get("usedAtMs"), int) and row["usedAtMs"] >= horizon
            }
            kept[binding.session_id] = {
                "cwd": binding.cwd,
                "mirasimKey": binding.mirasim_key,
                "usedAtMs": binding.used_at_ms,
            }
            return _encode(kept), None

        atomic_update(self.path, update)

    def delete(self, session_id: str) -> bool:
        """Forget ``session_id``; whether there was a record to forget."""
        if not self.path.exists():
            return False

        def update(raw: str | None) -> tuple[str | None, bool]:
            sessions = _decode(raw, self.path)
            if sessions.pop(session_id, None) is None:
                return None, False
            return _encode(sessions), True

        return atomic_update(self.path, update)


__all__ = ["RETENTION_DAYS", "STORE_VERSION", "Binding", "BindingStore", "store_dir", "store_path"]
