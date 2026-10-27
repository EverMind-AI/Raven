"""The reasoning effort one session picked for its own model calls.

A conversation can ask for more or less thinking than the configured default
(``agents.defaults.reasoningEffort``) without moving that default: a quick
question is not worth the time a deep one is, and the person asking is the one
who knows which is which. The pick rides the session's metadata beside
``model``, and for the same reason -- it is this conversation's own, it has to
outlive the gateway (a policy held only in memory goes back to the default on
every restart, which is what the tier does), and the turn reads it where it
reads the model.

Where it ranks (``in_force``, the one rule the turn runs at and the RPC
reports): the configured default underneath, a Session Mode's own
``reasoningEffort`` over that, this pick over both, and a hook's rollback
override over the lot (``_run_agent_loop``'s ``gen_overrides``). Raven's own
calls only: a sub-agent is told the configured default, as before.

The words a session may pick are its model's provider's
(``LLMProvider.reasoning_efforts``: the four every wire takes, ``xhigh`` and
``max`` over them where the wire takes them), checked when the pick is set.
The record may carry any rung this raven knows (``REASONING_EFFORT_RUNGS``),
because the model can move after the pick -- ``/model``, the picker -- and the
turn asks its own provider again (:func:`fit`): a word the new wire does not
take runs at the highest rung it does take, not at a request it would refuse.
The provider maps each word to its own wire, and a model with no thinking to
move ignores it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from loguru import logger

from raven.config.schema import REASONING_EFFORT_RUNGS

#: Where the pick is kept on a session: the metadata dict ``model`` lives in.
#: Null, not absent, once cleared -- a metadata patch can set a key and not
#: remove one, and both spellings read as "follows the default" here.
METADATA_KEY = "reasoning_effort"


def of(metadata: Any) -> str | None:
    """The pick a session record carries, or None while it follows the default.

    A word outside ``REASONING_EFFORT_RUNGS`` reads as none: a record written
    by a later raven is not a request this one can send, and the turn is
    better off at the default than refused by the provider. Whether the
    session's own wire takes the word is :func:`fit`'s question, not this
    one's.
    """
    if not isinstance(metadata, dict):
        return None
    stored = metadata.get(METADATA_KEY)
    return stored if isinstance(stored, str) and stored in REASONING_EFFORT_RUNGS else None


def fit(effort: str | None, words: Iterable[str] | None) -> str | None:
    """The rung a wire that takes ``words`` runs ``effort`` at.

    The word itself when the wire takes it; else the highest of ``words``
    below it; else None, the default. For a pick made on one wire and run on
    another: ``max`` picked on a Claude, then the session moved to a model
    whose wire takes the four, runs at ``high`` -- as much thinking as that
    wire takes, which is what the person asked for -- rather than at a
    request the wire would refuse, or at the default they moved away from.

    ``words`` None is a provider that cannot say, and the word stands as
    named, the way the empty-retry descent reads labels on their own without
    ``wire_keys``. A word this raven does not rank stands as named too: a
    mode's catalogue may say what this file cannot place, and the wire is the
    one to answer it.
    """
    if not effort or words is None:
        return effort or None
    taken = tuple(words)
    if effort in taken or effort not in REASONING_EFFORT_RUNGS:
        return effort
    below = [word for word in REASONING_EFFORT_RUNGS[: REASONING_EFFORT_RUNGS.index(effort)] if word in taken]
    return below[-1] if below else None


def in_force(pick: str | None, mode_effort: str | None, configured: str | None) -> str | None:
    """The effort a session's own calls run at, short of a hook's rollback override.

    One rule with one home. The turn freezes it at its start (``_run_agent_loop``)
    and ``session.info`` / ``session.set_effort`` report it, and the two used to
    spell the ranking out apart -- and to disagree on what no word meant. The
    turn left the key out of the call, which let the provider fill it from its
    own copy, frozen at boot from the same config key; so a default cleared on
    a running gateway kept every call at the word it booted with, while the RPC
    reported the clear as in force. Here None is one answer with one meaning,
    the provider's own default, and the turn sends it as such: an explicit None,
    nothing on the wire. The provider's boot copy is never consulted.

    The pick is the record's (``of`` / ``read``), the mode's word is the
    session's policy's, and the configured default is the file's as it is now
    (the loop's live reader, the RPC's fresh load): each caller reads its own
    inputs and this is the one place they are ranked.
    """
    return pick or mode_effort or configured or None


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
