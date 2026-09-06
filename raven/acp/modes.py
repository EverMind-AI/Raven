"""Session modes: the operating profiles a client may switch a session to.

The ACP spec's session-mode surface (``modes`` on a session response,
``session/set_mode`` to change it). A named profile a session runs in is a
first-class protocol concept, so it is served as one rather than re-spelled
as a configuration option.

Two decisions visible from the wire:

* **A mode is session state, not transcript state.** It is not persisted
  with the conversation: a stored session reopened later starts at the
  connection's default again, because the effort a question deserves is the
  caller's judgement at the time of asking.
* **Nothing is refused and nothing is interrupted.** A switch during a turn
  leaves that turn on the profile it started with and takes effect on the
  session's next turn -- the loop reads its per-session policy once, at the
  turn's start.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from raven.config.mode_catalogue import ModeProfile, build_mode_catalogue

# The profile shape itself is inner: the loop needs the default tier and the RPC
# surface serves the catalogue, and neither may import this surface to reach it.
# The ACP spelling stays as the name this package and its tests already use.
AcpModeProfile = ModeProfile


class SessionModes:
    """The declared profiles, the default, and each session's current choice."""

    def __init__(self, profiles: dict[str, AcpModeProfile], *, default: str | None) -> None:
        self._profiles = dict(profiles)
        self._default = default if default in self._profiles else next(iter(self._profiles), "")
        self._current: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._profiles)

    @property
    def default(self) -> str:
        return self._default

    def ids(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def profiles(self) -> tuple[AcpModeProfile, ...]:
        return tuple(self._profiles.values())

    def current(self, session_id: str) -> str:
        """Answers for a session it has never seen: the default."""
        return self._current.get(session_id, self._default)

    def profile(self, session_id: str) -> AcpModeProfile | None:
        return self._profiles.get(self.current(session_id))

    def set(self, session_id: str, mode_id: str) -> None:
        if mode_id not in self._profiles:
            raise KeyError(mode_id)
        previous = self.current(session_id)
        self._current[session_id] = mode_id
        if previous != mode_id:
            logger.info("acp: session {} switched from mode {} to {}", session_id, previous, mode_id)

    def state(self, session_id: str) -> dict[str, Any] | None:
        """The ``SessionModeState`` a session response carries; ``None`` when
        nothing is declared -- an empty catalogue is a picker onto nothing."""
        if not self._profiles:
            return None
        return {
            "currentModeId": self.current(session_id),
            "availableModes": [
                {"id": p.id, "name": p.name, "description": p.description} for p in self._profiles.values()
            ],
        }


def build_session_modes(config: Any) -> SessionModes:
    """The declared catalogue, wrapped in the per-session choice tracker."""
    catalogue = build_mode_catalogue(config)
    return SessionModes(catalogue.profiles, default=catalogue.default)


__all__ = ["AcpModeProfile", "SessionModes", "build_session_modes"]
