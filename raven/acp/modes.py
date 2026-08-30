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

from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class AcpModeProfile:
    """One resolved mode: what the loop enforces and what the hooks read."""

    id: str
    name: str
    description: str
    max_iterations: int | None
    overlay: dict[str, Any] = field(default_factory=dict)


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
    """Resolve ``config.acp.modes`` once at startup."""
    acp = getattr(config, "acp", None)
    declared = getattr(acp, "modes", None) or {}
    profiles = {
        mode_id: AcpModeProfile(
            id=mode_id,
            name=declaration.name,
            description=declaration.description,
            max_iterations=declaration.max_tool_iterations,
            overlay=dict(declaration.overlay),
        )
        for mode_id, declaration in declared.items()
    }
    return SessionModes(profiles, default=getattr(acp, "default_mode", None))


__all__ = ["AcpModeProfile", "SessionModes", "build_session_modes"]
