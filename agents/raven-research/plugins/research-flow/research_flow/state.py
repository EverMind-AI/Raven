"""Per-session state the research flow keeps outside the transcript.

The fork stored cross-turn research state (the memo, an open clarify round) on
``session.metadata``; hooks in the trunk see a ``session_key`` and never the
Session, so the plugin owns a store instead: one JSON file per session under
``<root>/sessions/<slug>.json``, and the per-turn ledger files under
``<root>/ledger``. The root comes from the plugin config (``stateRoot``),
defaulting to ``<workspace>/research_flow``.

The session ContextVar that scopes tool-side state lives with the tools
(``research_flow.tools.web``); this module is only the disk half.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.paths import mint_slug


def session_slug(session_key: str) -> str:
    """Filesystem name for one session: the minted slug, or a sha1 when nothing survives.

    ``mint_slug`` returns "" for a key with no alphanumerics at all; the hash
    fallback keeps such keys addressable rather than folding them into one file.
    """
    slug = mint_slug(session_key, max_chars=80)
    if slug:
        return slug
    return hashlib.sha1(session_key.encode("utf-8")).hexdigest()  # noqa: S324 - a filename, not a credential


@dataclass
class SessionRecord:
    """What one session carries across turns.

    ``research_memo`` and ``pending_clarify`` hold the same dict shapes the fork
    persisted on ``session.metadata`` (``ResearchMemo.to_metadata`` /
    ``PendingClarify.to_metadata``); ``chain_round`` mirrors the open clarify
    chain's count (0 when no round is open); ``mode`` is the session's last-seen
    mode name, kept so a phase that fires before the loop reports one (the
    inbound rewrite) resolves the same chain as the rest of the turn.
    ``observers`` is the latest turn's read-only counters (final shape,
    conversation gate, process appendix) - the fork attached these to the last
    assistant message, which a plugin cannot reach.
    """

    research_memo: dict[str, Any] | None = None
    pending_clarify: dict[str, Any] | None = None
    chain_round: int = 0
    mode: str = ""
    observers: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> "SessionRecord":
        if not isinstance(raw, dict):
            return cls()
        memo = raw.get("research_memo")
        pending = raw.get("pending_clarify")
        observers = raw.get("observers")
        try:
            chain = int(raw.get("chain_round") or 0)
        except (TypeError, ValueError):
            chain = 0
        return cls(
            research_memo=memo if isinstance(memo, dict) else None,
            pending_clarify=pending if isinstance(pending, dict) else None,
            chain_round=chain,
            mode=str(raw.get("mode") or ""),
            observers=observers if isinstance(observers, dict) else {},
        )


class SessionStore:
    """One JSON file per session, written atomically.

    Failure is degradation, never an exception: a corrupt or unreadable file
    yields a fresh record with a warning, and a failed write is logged and
    dropped - cross-turn niceties must not be able to kill a turn.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.ledger_dir = self.root / "ledger"

    def path_for(self, session_key: str) -> Path:
        return self.sessions_dir / f"{session_slug(session_key)}.json"

    def load(self, session_key: str) -> SessionRecord:
        path = self.path_for(session_key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return SessionRecord()
        except (OSError, ValueError) as e:
            logger.warning("research-flow session state unreadable ({}): {}", path, e)
            return SessionRecord()
        return SessionRecord.from_dict(raw)

    def save(self, session_key: str, record: SessionRecord) -> None:
        path = self.path_for(session_key)
        tmp = path.with_name(path.name + ".tmp")
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(asdict(record), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("research-flow session state not saved ({}): {}", path, e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "SessionRecord",
    "SessionStore",
    "session_slug",
]
