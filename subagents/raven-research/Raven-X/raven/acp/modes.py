"""Session modes: the operating profiles a client may switch a session to.

The ACP spec's session-mode surface (``modes`` on a session response,
``session/set_mode`` to change it). Raven-X uses it for research effort: the
same agent, the same identity prompt and the same credential, run against a
different DR budget.

Why this and not ``session/set_config_option``, which the main repo's ACP agent
uses for its model picker: that surface exists there because ``session/set_model``
is *not* in the stable schema, so a generic option list is the only stable
channel for it. Session modes are in the stable schema, and a named operating
profile is exactly what they are for -- an option list would be re-spelling a
first-class concept as configuration.

Two decisions worth stating, because both are visible from the wire:

* **A mode is session state, not engine state.** It outlives the engine, which
  the ``max_loops`` cap may evict and rebuild at any time, and it is deliberately
  not persisted with the transcript: a stored session reopened later starts at
  the connection's default again, because the effort a question deserves is the
  caller's judgement at the time of asking, not a property of the conversation.
* **Nothing is refused and nothing is interrupted.** A switch during a turn
  leaves that turn on the profile it started with and takes effect on the
  session's next turn -- the same contract the main repo's model picker states.
  It needs no code here: :class:`raven.acp.loops.AcpLoops` compares each
  session's mode id against the one its resident engine was built under, and a
  turn only ever reaches that check at its own start.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class AcpModeProfile:
    """One resolved mode: the two engine inputs an effort profile moves.

    Resolved once at startup rather than per session, so an overlay that does
    not validate kills the process at launch instead of failing the
    ``session/set_mode`` that first reaches it -- by which point a person has
    already been told the switch was available.
    """

    id: str
    name: str
    description: str
    dr_flow: Any
    max_iterations: int


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` into a copy of ``base``: dicts recurse, else replace.

    Lists replace wholesale -- an overlay that touches a list owns it -- and an
    explicit null lands as null, which the schema reads as "back to the built-in
    default" for a nullable knob like ``maxIterations``. Copies rather than
    mutating: the baseline is dumped once and merged against per mode, so an
    in-place merge would compose every overlay onto the one before it.

    The only implementation. The launcher that writes these overlays used to
    carry its own and apply it at render time; composing per session moved the
    merge here, and leaving the other one in place would have been two
    definitions of what an overlay means.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_profiles(config: Any, ec_config: Any) -> dict[str, AcpModeProfile]:
    """Validate every declared mode into the profile its sessions run on.

    The DR overlay is merged over the config's own ``drFlow`` and revalidated,
    so a mode can only move keys the schema already has: a typo becomes a
    startup failure naming the mode, not a silently ignored knob.
    """
    from raven.config.raven import DRFlowConfig

    declared = getattr(config.acp, "modes", None) or {}
    if not declared:
        return {}
    baseline = ec_config.dr_flow.model_dump(mode="python", by_alias=True)
    fallback_iterations = config.agents.defaults.max_tool_iterations
    profiles: dict[str, AcpModeProfile] = {}
    for mode_id, declaration in declared.items():
        try:
            dr_flow = DRFlowConfig.model_validate(_deep_merge(baseline, declaration.dr_flow))
        except Exception as exc:
            raise ValueError(f"acp.modes.{mode_id}.drFlow does not validate: {exc}") from exc
        profiles[mode_id] = AcpModeProfile(
            id=mode_id,
            name=declaration.name,
            description=declaration.description,
            dr_flow=dr_flow,
            max_iterations=declaration.max_tool_iterations or fallback_iterations,
        )
    return profiles


class SessionModes:
    """Which profile each session runs on, and the catalogue clients pick from.

    ``baseline`` is what a session runs on when no modes are declared: the
    config's own ``drFlow`` and iteration cap, which is byte-identical to what
    ``build_loop`` passed before this surface existed. That is the whole
    degradation path -- a deployment that declares nothing behaves exactly as
    it did.
    """

    def __init__(
        self,
        profiles: dict[str, AcpModeProfile],
        *,
        default: str | None,
        baseline: AcpModeProfile,
    ) -> None:
        self._profiles = dict(profiles)
        self._baseline = baseline
        self._default = default if default in self._profiles else next(iter(self._profiles), "")
        self._current: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._profiles)

    @property
    def default(self) -> str:
        return self._default

    def current(self, session_id: str) -> str:
        """The session's mode id, and the tag ``AcpLoops`` rebuilds against.

        Answers for a session it has never seen, because it is called for every
        engine build -- including the one ``session/new`` triggers before any
        mode could have been set.
        """
        return self._current.get(session_id, self._default)

    def profile(self, session_id: str) -> AcpModeProfile:
        return self._profiles.get(self.current(session_id), self._baseline)

    def set(self, session_id: str, mode_id: str) -> None:
        """Record a session's mode. Raises ``KeyError`` for an unknown id."""
        if mode_id not in self._profiles:
            raise KeyError(mode_id)
        previous = self.current(session_id)
        self._current[session_id] = mode_id
        if previous != mode_id:
            logger.info("acp: session {} switched from mode {} to {}", session_id, previous, mode_id)

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

    def ids(self) -> tuple[str, ...]:
        return tuple(self._profiles)


def build_session_modes(config: Any, ec_config: Any) -> SessionModes:
    """The connection's mode table, resolved and validated once."""
    baseline = AcpModeProfile(
        id="",
        name="",
        description="",
        dr_flow=ec_config.dr_flow,
        max_iterations=config.agents.defaults.max_tool_iterations,
    )
    profiles = resolve_profiles(config, ec_config)
    modes = SessionModes(profiles, default=getattr(config.acp, "default_mode", None), baseline=baseline)
    if profiles:
        logger.info("acp: session modes {} (default {})", ", ".join(modes.ids()), modes.default)
    return modes


__all__ = ["AcpModeProfile", "SessionModes", "build_session_modes", "resolve_profiles"]
