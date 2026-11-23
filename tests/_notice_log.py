"""A session log for tool-notice tests, filed and read back the way a turn does.

The registry's standing notices are read from the log at every assembly
(``AgentLoop._bind_session_log``), so a test that walks notice transitions has
to keep a real log: user entries whose runtime-context block carries the lines
a turn sent, read back with ``told_notices``. ``NoticeLog`` keeps one, either
on its own or on a real ``Session`` so a clear, a save and a reload act on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from raven.context_engine.runtime_context import RUNTIME_CONTEXT_FIELD, notice_changes, notice_key, told_notices
from raven.context_engine.segments.render import build_runtime_context
from raven.session.manager import Session


def filed(lines: list[str]) -> dict[str, Any]:
    """A user entry carrying ``lines`` in its runtime-context block, as a turn files it."""
    block = build_runtime_context(lambda: datetime(2026, 11, 23, 12, 0), "cli", "c1", tool_notices=lines)
    return {"role": "user", "content": "go on", RUNTIME_CONTEXT_FIELD: block}


class NoticeLog:
    """One session's log; each turn binds to it, takes the notices and files what was sent."""

    def __init__(self, log_id: str = "log-a", *, session: Session | None = None, key: str = "s1") -> None:
        self.key = key
        self.session = session
        self._log_id = log_id
        self._messages: list[dict[str, Any]] = []
        self.current: dict[tuple[str, str], str] = {}
        self.sent: list[str] = []

    @property
    def log_id(self) -> str:
        return self.session.revision.log_id if self.session is not None else self._log_id

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.session.messages if self.session is not None else self._messages

    @property
    def told(self) -> dict[tuple[str, str], str]:
        return told_notices(self.messages)

    def bind(self, loop: Any) -> None:
        loop._bind_session_log(self.key, self.log_id, self.messages)

    def clear(self, log_id: str = "log-cleared") -> None:
        """What ``Session.clear`` does to the log: a new id and no messages."""
        if self.session is not None:
            self.session.clear()
        else:
            self._log_id, self._messages = log_id, []

    def turn(self, loop: Any) -> dict[tuple[str, str], str]:
        self.bind(loop)
        with loop.tools.session_scope_for(self.key):
            notes = loop._tool_notices()
        current = {key: note for note in notes if (key := notice_key(note)) is not None}
        sent = notice_changes(notes, self.told)
        if sent:
            self.messages.append(filed(sent))
        self.current, self.sent = current, sent
        return current

    def cleared(self) -> list[str]:
        """The lines that say a keyed note no longer applies."""
        return [line for line in self.sent if (key := notice_key(line)) is not None and key not in self.current]
