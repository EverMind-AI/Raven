"""Read agent state from a workspace directory.

After a sequence of ``raven`` subprocess invocations, the persistent
artifacts the eval cares about live on disk:

- ``<workspace>/MEMORY.md`` — agent's working memory
- ``<workspace>/HISTORY.md`` — conversation/action log
- ``<workspace>/sessions/*.jsonl`` — per-session message history
- ``~/.raven/sentinel/feedback.jsonl`` — every dispatch + engagement event
  (was ``<workspace>/sentinel_feedback.jsonl`` before the colocation move)
- ``~/.raven/sentinel/state.json`` — cross-process Sentinel state
  (NudgePolicy quotas, dedup, dismissals); GLOBAL not per-workspace today

This module is read-only. The driver writes to these via subprocess;
the eval reads them through ``AgentState.from_workspace()``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentState:
    """Snapshot of agent-on-disk state at a moment in time."""

    workspace: Path
    memory_md: str | None
    history_md: str | None
    sentinel_feedback: list[dict] = field(default_factory=list)
    sessions: dict[str, list[dict]] = field(default_factory=dict)
    sentinel_state: dict | None = None
    pending_decisions: dict | None = None
    routines: dict | None = None

    @classmethod
    def from_workspace(
        cls,
        workspace: Path,
        sentinel_state_dir: Path | None = None,
    ) -> "AgentState":
        """Read every artifact present under ``workspace`` and (optionally)
        ``sentinel_state_dir``. Missing files map to ``None`` / empty
        collections — never raises.

        ``sentinel_state_dir`` defaults to ``~/.raven/sentinel/``.
        Pass an explicit path when the driver overrides it via the
        ``RAVEN_RUNTIME_DIR`` env var.
        """
        ws = Path(workspace)
        memory = _read_text(ws / "MEMORY.md")
        history = _read_text(ws / "HISTORY.md")

        sessions: dict[str, list[dict]] = {}
        sessions_dir = ws / "sessions"
        if sessions_dir.is_dir():
            for f in sorted(sessions_dir.glob("*.jsonl")):
                sessions[f.stem] = _read_jsonl(f)

        if sentinel_state_dir is None:
            sentinel_state_dir = Path.home() / ".raven" / "sentinel"
        sd = Path(sentinel_state_dir)
        feedback = _read_jsonl(sd / "feedback.jsonl")
        if not feedback:
            # Legacy fallback for workspaces written by pre-migration sentinel.
            feedback = _read_jsonl(ws / "sentinel_feedback.jsonl")
        sentinel_state = _read_json(sd / "state.json")
        pending = _read_json(sd / "pending_decisions.json")
        routines = _read_json(sd / "routines.json")

        return cls(
            workspace=ws,
            memory_md=memory,
            history_md=history,
            sentinel_feedback=feedback,
            sessions=sessions,
            sentinel_state=sentinel_state,
            pending_decisions=pending,
            routines=routines,
        )


def _read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
