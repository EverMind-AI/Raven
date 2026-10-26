"""The reasoning effort one session picked for its own model calls.

A conversation can ask for more or less thinking than the configured default
(``agents.defaults.reasoningEffort``) without moving that default: a quick
question is not worth the time a deep one is, and the person asking is the one
who knows which is which. The pick rides the session's metadata beside
``model``, and for the same reason -- it is this conversation's own, it has to
outlive the gateway (a policy held only in memory goes back to the default on
every restart, which is what the tier does), and the turn reads it where it
reads the model.

Where it ranks: the configured default underneath, a Session Mode's own
``reasoningEffort`` over that, this pick over both, and a hook's rollback
override over the lot (``_run_agent_loop``'s ``gen_overrides``). Raven's own
calls only: a sub-agent is told the configured default, as before.

The words are ``REASONING_EFFORTS`` (``config/schema.py``); the provider maps
each to its own wire, and a model with no thinking to move ignores it.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from raven.config.schema import REASONING_EFFORTS

#: Where the pick is kept on a session: the metadata dict ``model`` lives in.
#: Null, not absent, once cleared -- a metadata patch can set a key and not
#: remove one, and both spellings read as "follows the default" here.
METADATA_KEY = "reasoning_effort"


def of(metadata: Any) -> str | None:
    """The pick a session record carries, or None while it follows the default.

    A word outside ``REASONING_EFFORTS`` reads as none: a record written by a
    later raven is not a request this one can send, and the turn is better off
    at the default than refused by the provider.
    """
    if not isinstance(metadata, dict):
        return None
    stored = metadata.get(METADATA_KEY)
    return stored if isinstance(stored, str) and stored in REASONING_EFFORTS else None


def read(sessions: Any, session_key: str | None) -> str | None:
    """What one session picked, read off its record; None for one following the default."""
    if sessions is None or not session_key:
        return None
    try:
        # `peek`, not `get_or_create`, for the reason knowledge_scope.read
        # gives: a read must not cache a fresh empty session under a key the
        # manager has never seen.
        held = sessions.peek(session_key)
    except Exception:  # noqa: BLE001 - a session that cannot be read has no pick
        return None
    return of(getattr(held, "metadata", None)) if held is not None else None


def write(sessions: Any, session_key: str, effort: str | None) -> bool:
    """Record the pick (None clears it), and answer whether it reached the disk.

    A session that has no record yet -- a draft the first message has not
    filed -- holds it on its in-memory copy, which the first turn's save then
    writes; the caller reports that as pending, the way ``session.pin`` does.
    """
    persisted = False
    try:
        persisted = bool(sessions.append_metadata_patch(session_key, {METADATA_KEY: effort}))
    except Exception:  # noqa: BLE001 - the in-memory copy below still carries it
        logger.warning("session {}: failed to persist the reasoning effort", session_key)
    if not persisted:
        sessions.get_or_create(session_key).metadata[METADATA_KEY] = effort
    return persisted
