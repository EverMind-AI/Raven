"""The Raven-owned conversation state for one sub-agent instance.

A ``cli`` instance's session lives inside the CLI's own store and the registry
keeps only its id. The built-in in-process sub-agent and an OpenAI-compatible
HTTP agent have no such store, so Raven keeps their message list here and
replays it on the next turn -- which is what makes those two kinds resumable.

Sits beside the audit record (``raven/agent/subagent/history.py``) but is a
different thing: this file is mutable resume state and gets overwritten, while
a record directory is append-only evidence. Keeping them in one file would let
a single interrupted write destroy both.

Lifetime: deleting a chat session (``SessionManager.delete``) only unlinks its
transcript and never touches the session's metadata directory, so a file this
module writes is not reclaimed when its session is -- the same as the
pre-existing ``spawn/`` and ``mas_dag/`` audit trees beside it. There is no TTL
and no count cap. Reclaiming this directory would mean teaching session
deletion to recurse into that metadata tree, which would also start deleting
those pre-existing audit trails; that is a separate change, deferred for now.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.subagent.direct_chat import direct_root

_FILENAME = "messages.json"


def instance_state_path(session_dir: Path, agent: str, handle: str) -> Path:
    """Where one instance's message list lives. Not created here.

    Shares ``direct_root`` with the turn-record directory
    (``raven/agent/subagent/direct_chat.py``), which is what puts
    ``safe_path_segment`` on both ``agent`` and ``handle``: a handle is
    free-form text chosen by the model, so it is the one component here that
    could otherwise escape the directory.
    """
    return direct_root(session_dir, agent, handle) / _FILENAME


class InstanceState:
    """One instance's message list, persisted as a single JSON array."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        """The stored messages, or an empty list.

        Every failure degrades to empty rather than raising: a missing file is
        the normal first-turn case, and a corrupt one must cost the user this
        instance's memory, not the turn they are trying to send.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            logger.warning("Instance state at {} is not a list; ignoring it", self._path)
            return []
        return [m for m in raw if isinstance(m, dict)]

    def save(self, messages: list[dict[str, Any]]) -> None:
        """Replace the stored messages atomically."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("Instance state at {} could not be written: {}", self._path, exc)


__all__ = ["InstanceState", "instance_state_path"]
