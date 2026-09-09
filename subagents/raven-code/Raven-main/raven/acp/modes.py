"""Session modes: the operating profiles a client may switch a session to.

The ACP spec's session-mode surface (``modes`` on a session response,
``session/set_mode`` to change it). Raven-Code uses it for effort: the same
agent, the same identity prompt and the same credential, run at a different
reasoning effort.

Why this and not ``session/set_config_option``, which the model picker uses:
that surface exists because ``session/set_model`` is *not* in the stable
schema, so a generic option list is the only stable channel for it. Session
modes are in the stable schema, and a named operating profile is exactly what
they are for -- an option list would re-spell a first-class concept as
configuration.

Two decisions visible from the wire:

* **A mode is session state, not transcript state.** Kept in memory keyed by
  session id and deliberately not persisted with the conversation: the effort
  a question deserves is the caller's judgement at the time of asking, not a
  property of the record. The host re-sends the mode on every route into a
  session for the same reason.
* **Nothing is refused and nothing is interrupted.** A switch during a turn
  leaves that turn on the profile it started with and takes effect on the
  session's next turn -- the loop reads its session policy once, at a turn's
  start (``AgentLoop.session_policy``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class AcpModeProfile:
    """One declared mode: what the client sees and what the engine is handed.

    ``reasoning_effort`` is ``None`` for a mode that moves nothing -- the
    default mode, whose sessions run on the connection's own configuration.
    """

    id: str
    name: str
    description: str
    reasoning_effort: str | None


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
        """The session's mode id; the default for a session never switched."""
        return self._current.get(session_id, self._default)

    def profile(self, session_id: str) -> AcpModeProfile | None:
        """The session's profile, or ``None`` when no modes are declared."""
        return self._profiles.get(self.current(session_id))

    def set(self, session_id: str, mode_id: str) -> None:
        """Record a session's mode. Raises ``KeyError`` for an unknown id."""
        if mode_id not in self._profiles:
            raise KeyError(mode_id)
        previous = self.current(session_id)
        self._current[session_id] = mode_id
        if previous != mode_id:
            logger.info("acp: session {} switched from mode {} to {}", session_id, previous, mode_id)

    def forget(self, session_id: str) -> None:
        """Drop a closed session's choice; a later session under the same id
        starts on the default like any other."""
        self._current.pop(session_id, None)

    def state(self, session_id: str) -> dict[str, Any] | None:
        """The spec's ``SessionModeState``, or ``None`` when none are declared.

        ``None`` rather than an empty catalogue: a session response carrying an
        empty ``availableModes`` is a mode picker that opens onto nothing, and
        the client cannot tell it from a picker whose entries failed to load.
        """
        if not self._profiles:
            return None
        return {
            "currentModeId": self.current(session_id),
            "availableModes": [
                {"id": profile.id, "name": profile.name, "description": profile.description}
                for profile in self._profiles.values()
            ],
        }


def build_session_modes(config: Any) -> SessionModes:
    """Resolve ``config.acp.modes`` once, at startup.

    Validated here rather than at the first ``session/set_mode``: by then a
    client has already been shown the mode as available, and a launch that
    fails is reported by the host cleanly while a failed switch is not.
    """
    acp = config.acp
    profiles: dict[str, AcpModeProfile] = {}
    for mode_id, declaration in acp.modes.items():
        if not mode_id.strip():
            raise ValueError("acp.modes: a mode id must be a non-empty string")
        effort = declaration.reasoning_effort
        if effort is not None and not effort.strip():
            raise ValueError(f"acp.modes.{mode_id}.reasoningEffort must be a non-empty string when given")
        profiles[mode_id] = AcpModeProfile(
            id=mode_id,
            name=declaration.name,
            description=declaration.description,
            reasoning_effort=effort,
        )
    if acp.default_mode is not None and acp.default_mode not in profiles:
        raise ValueError(
            f"acp.defaultMode {acp.default_mode!r} is not a declared mode; declared: {', '.join(profiles) or 'none'}"
        )
    modes = SessionModes(profiles, default=acp.default_mode)
    if profiles:
        logger.info("acp: session modes {} (default {})", ", ".join(modes.ids()), modes.default)
    return modes


__all__ = ["AcpModeProfile", "SessionModes", "build_session_modes"]
