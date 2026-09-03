"""The session-retirement notification: the store says a session is gone.

A plugin that keeps per-session state outside the session store (an
allocation ledger, a provisioned directory) contributes an observer the way
it contributes a tool: a manifest entry, a factory taking ``PluginContext``.
Three disciplines are the paper, not a convention:

- **The store notifies; it never waits.** ``on_session_deleted`` is called
  synchronously after the store has acted on a delete request, from
  whichever host surface asked. Return promptly; schedule slow work
  yourself. An observer that raises is logged and skipped -- the deletion's
  outcome is already decided and no observer can veto or repair it.
- **The request, and its outcome.** The observer fires for every delete
  request the store handles; ``removed`` reports whether a file was
  actually removed (False when none existed -- a session can live in cache
  alone -- or when the removal failed), so a consumer can filter. Holding
  nothing under the key is the observer's own no-op.
- **In-process only.** A deletion performed by another process (a CLI
  invocation, a stray ``rm``) is out of the store's sight; an observer
  whose state must survive that keeps its own out-of-band probe.

Only a resident host attaches observers (a one-shot turn never does); the
host owns the attachment lifecycle alongside the generation's services.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SessionObserver(Protocol):
    """What a ``[[plugin.contributes.session_observers]]`` factory returns."""

    def on_session_deleted(self, session_key: str, removed: bool) -> None: ...


__tier__ = "contract"
__all__ = ["SessionObserver"]
