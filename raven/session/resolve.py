"""Resolve the SessionManager a server-side method should use.

One rule, two callers' worth of history: prefer the loop's shared manager --
the one the turn path files into -- and only fall back to building a fresh
one for the configured workspace when no loop is in the room (a CLI listing
sessions, a method invoked before the loop exists). Both dialects (rpc and
acp) resolve through here, which is why it lives beside the manager rather
than on either surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.session.manager import SessionManager

if TYPE_CHECKING:
    from raven.config.schema import Config


def build_manager(config: "Config") -> SessionManager:
    """Return a ``SessionManager`` for the configured workspace.

    Module-level so tests can monkeypatch it to inject a pre-populated manager
    without touching the filesystem (same seam as ``load_config``).
    """
    return SessionManager(config.workspace_path)


def manager_for(agent_loop: Any, config: "Config") -> SessionManager:
    """Prefer the loop's shared manager when available; fall back to a fresh one.

    The loop is duck-typed on purpose: this module must not import the loop,
    and a host that hands anything with a ``sessions`` manager gets the same
    answer.
    """
    if agent_loop is not None:
        mgr = getattr(agent_loop, "sessions", None)
        if isinstance(mgr, SessionManager):
            return mgr
    return build_manager(config)
