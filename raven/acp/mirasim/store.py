"""Which mirasim conversation each ACP session continues, kept on disk.

The two ids are minted at different moments. ACP needs a session id when the
session opens, while mirasim names its conversation only when the first turn
settles, and refuses a key the caller made up ("not one of this server's
sessions", measured). So the adapter mints the ACP id and records the mirasim
key against it once a turn reports one.

On disk rather than in memory, because Raven resumes an instance with
``session/load`` on whatever adapter process it reaches next: a restarted
adapter, or a second one Raven launched for another binding, has to find the
same conversation. One file per mirasim agent, written under the cross-process
lock in :mod:`raven.utils.atomic_io`, since those processes write it
concurrently.

Only what continuing needs is kept: the conversation and the directory it
works in. The model and effort a session was last put on are not, because
Raven's client re-asserts its own choice on every route into a session and
keeps none across its own restart -- an adapter restoring that choice would
leave the session answering on a model every Raven surface reports as cleared.

A record is dropped once it has gone :data:`RETENTION_DAYS` without a turn,
and at once on ``session/delete``, which Raven sends when a person forgets the
instance. Raven then finds no session to load and opens a fresh one; the
conversation itself stays in mirasim's own history either way.
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
    return raven_home() / "subagent_sessions" / "mirasim" / f"{agent}.json"


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
        logger.warning("mirasim session store {} is not JSON ({}); treating it as empty", path, exc)
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
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        row = _decode(raw, self.path).get(session_id)
        binding = _binding(session_id, row) if row is not None else None
        if binding is None or _now_ms() - binding.used_at_ms > _RETENTION_MS:
            return None
        return binding

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


__all__ = ["RETENTION_DAYS", "STORE_VERSION", "Binding", "BindingStore", "store_path"]
