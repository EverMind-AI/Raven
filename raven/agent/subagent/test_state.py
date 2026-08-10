"""Remembered outcomes of explicit subagent availability tests.

Worth keeping, because for a cli agent the free probe can only answer
"installed" while the failures that matter -- a missing provider credential, a
hard runtime-version gate -- are observable only by running the thing.

Worth invalidating, because a remembered verdict becomes a lie the moment the
configuration it measured changes. Every record therefore carries a digest of the
fields that decide how the agent runs, and a verdict whose digest no longer
matches is treated as absent rather than shown as current. Doing it by digest
rather than by hooking the write path is what also covers a hand-edited
``config.json``, an edit no UI hook would ever see.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

_FILENAME = "subagent_test_state.json"

# Only the fields that decide how the agent runs. `name`, `description`, `preset`
# and `enabled` are deliberately absent: renaming an agent or switching it off and
# on does not change whether it works, so neither may discard a verdict that holds.
_CLI_FIELDS = (
    "command",
    "resume_command",
    "id_source",
    "session_id_pattern",
    "output_pattern",
    "transcript_format",
    "cwd",
    "env",
    "timeout",
)
_OPENAI_FIELDS = ("base_url", "model", "api_key")


def default_state_path() -> Path:
    from raven.config.loader import get_config_path

    return get_config_path().parent / _FILENAME


@dataclass(frozen=True)
class LastTest:
    """One remembered verdict, already validated against the current config."""

    ok: bool
    detail: str
    tested_at_ms: int


def fingerprint(cfg: Any) -> str:
    """A digest of the fields that decide how this agent runs.

    Truncated because this is a change detector, not a security boundary: a
    collision would at worst surface one stale verdict. ``api_key`` is included
    so that fixing a rejected key clears the old failure, and only the digest is
    ever written, never the key.
    """
    fields = _OPENAI_FIELDS if getattr(cfg, "kind", None) == "openai" else _CLI_FIELDS
    payload = {name: getattr(cfg, name, None) for name in fields}
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class TestStateStore:
    """One JSON file of remembered verdicts, keyed by source and name.

    Stored as a list rather than a keyed object for the reason
    :mod:`raven.agent.subagent.instances` gives for the same choice: a subagent
    name is an arbitrary user string, and a list needs no key escaping.
    """

    # The name starts with pytest's default `python_classes = Test*` prefix, so
    # importing it into a test module makes the collector try to collect it and warn
    # about the constructor. The name is right for the domain -- it stores test
    # state -- so opt the class out of collection rather than rename it.
    __test__ = False

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_state_path()

    def _read(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = raw.get("verdicts") if isinstance(raw, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "verdicts": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def record(self, cfg: Any, source: str, *, ok: bool, detail: str, tested_at_ms: int) -> None:
        """Remember one verdict, replacing any previous one for the same agent.

        Takes the config object rather than a precomputed digest so ``record`` and
        ``load`` cannot drift onto different field sets -- the one failure that
        would make invalidation silently stop working. ``tested_at_ms`` is passed
        in rather than read from the clock here so a test can pin it.
        """
        name = getattr(cfg, "name", "") or ""
        rows = [r for r in self._read() if not (r.get("source") == source and r.get("name") == name)]
        rows.append(
            {
                "source": source,
                "name": name,
                "ok": bool(ok),
                "detail": detail,
                "fingerprint": fingerprint(cfg),
                "testedAtMs": int(tested_at_ms),
            }
        )
        try:
            self._write(rows)
        except OSError as e:  # a remembered verdict is a convenience, never a dependency
            logger.warning("subagent test state write failed (verdict not remembered): {}", e)

    def load(self, entries: Sequence[tuple[Any, str]]) -> dict[str, LastTest]:
        """Verdicts still valid for these ``(config, source)`` pairs, keyed ``"source:name"``.

        A verdict whose fingerprint no longer matches its config is skipped, which
        is the whole invalidation mechanism -- nothing has to delete it.
        """
        rows = {(row.get("source"), row.get("name")): row for row in self._read()}
        found: dict[str, LastTest] = {}
        for cfg, source in entries:
            name = getattr(cfg, "name", "") or ""
            row = rows.get((source, name))
            if row is None or row.get("fingerprint") != fingerprint(cfg):
                continue
            found[f"{source}:{name}"] = LastTest(
                ok=bool(row.get("ok")),
                detail=str(row.get("detail") or ""),
                tested_at_ms=int(row.get("testedAtMs") or 0),
            )
        return found


__all__ = ["LastTest", "TestStateStore", "default_state_path", "fingerprint"]
