"""The permission mode one conversation runs in, when it differs from the default.

Set from a settings surface (``/perm smart`` in the TUI, the composer chip on
the web) through ``config.set`` with a ``session_id``; read by the gate on every
tool call through the turn's conversation id, so a switch holds from the next
call, and a sub-agent's task -- which inherits the turn -- reads the same one.

Kept the way a conversation's model is kept: in memory here, and on the
conversation's own record (``session.metadata["permissions_mode"]``, written
by the RPC handler) so a restart does not undo it. The loop registers the
reader for that record at construction; a conversation this process has not
heard of is looked up once, and the answer -- either way -- is remembered.
``permissions.mode`` in config stays the default a new conversation starts on.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

_MODES: dict[str, str] = {}
_LOOKED_UP: set[str] = set()
_RESTORE: Callable[[str], str | None] | None = None


def set_session_mode_restorer(restore: Callable[[str], str | None] | None) -> None:
    """Register how a conversation's stored mode is read back; ``None`` forgets it."""
    global _RESTORE
    _RESTORE = restore
    _LOOKED_UP.clear()


def set_session_mode(conversation_id: str, mode: str | None) -> str | None:
    """Give a conversation its own mode, or with ``None`` return it to the default."""
    previous = session_mode(conversation_id)
    _LOOKED_UP.add(conversation_id)
    if mode is None:
        _MODES.pop(conversation_id, None)
    else:
        _MODES[conversation_id] = mode
    return previous


def session_mode(conversation_id: str) -> str | None:
    mode = _MODES.get(conversation_id)
    if mode is not None or not conversation_id or conversation_id in _LOOKED_UP or _RESTORE is None:
        return mode
    _LOOKED_UP.add(conversation_id)
    try:
        stored = _RESTORE(conversation_id)
    except Exception as exc:  # noqa: BLE001 - a record that cannot be read means the default
        logger.debug("cannot read the stored permission mode of {!r}: {}", conversation_id, exc)
        return None
    if stored:
        _MODES[conversation_id] = stored
    return stored or None


__all__ = ["session_mode", "set_session_mode", "set_session_mode_restorer"]
