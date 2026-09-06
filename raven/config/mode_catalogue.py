"""The mode catalogue as configuration: which profiles a deployment declares,
and which of them a session starts on.

Inner by necessity rather than by taste. The agent loop needs the default tier
and the RPC surface serves the whole catalogue, and neither may reach through
``raven.acp`` to get it: the architecture contracts forbid an inner layer
knowing a surface at all, and name ``raven.rpc`` importing ``raven.acp``
specifically. What stays behind in :mod:`raven.acp.modes` is the part that is
genuinely the ACP surface's own -- which mode each live session is currently on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from raven.i18n import t as _t


@dataclass(frozen=True)
class ModeProfile:
    """One resolved mode: what the loop enforces and what the hooks read."""

    id: str
    name: str
    description: str
    max_iterations: int | None
    overlay: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModeCatalogue:
    """A deployment's declared profiles, resolved once, with its default named.

    ``default`` is already settled here rather than left to each reader: an
    unresolvable name degrades to the first declared profile, and an empty
    catalogue to ``""``.
    """

    profiles: dict[str, ModeProfile]
    default: str

    @property
    def enabled(self) -> bool:
        return bool(self.profiles)

    def ids(self) -> tuple[str, ...]:
        return tuple(self.profiles)

    def get(self, mode_id: str) -> ModeProfile | None:
        return self.profiles.get(mode_id)


def build_mode_catalogue(config: Any) -> ModeCatalogue:
    """Resolve ``config.acp.modes`` once, at startup."""
    acp = getattr(config, "acp", None)
    declared = getattr(acp, "modes", None) or {}
    # Raven's own rows are translated here rather than where they are declared,
    # so a language selected after that module was imported still applies. A
    # declared catalogue is left alone: its words are its author's, and running
    # them through the catalog would look up an id nobody registered.
    ours = bool(getattr(acp, "uses_builtin_modes", False))
    profiles = {
        mode_id: ModeProfile(
            id=mode_id,
            name=declaration.name,
            description=_t(declaration.description) if ours else declaration.description,
            max_iterations=declaration.max_tool_iterations,
            overlay=dict(declaration.overlay),
        )
        for mode_id, declaration in declared.items()
    }
    # `effective_default_mode` where the config model offers it (it separates the
    # built-in default from a custom catalogue's unset one); the plain field for
    # anything duck-typed into this function.
    named = getattr(acp, "effective_default_mode", None) or getattr(acp, "default_mode", None)
    return ModeCatalogue(profiles=profiles, default=named if named in profiles else next(iter(profiles), ""))


__all__ = ["ModeCatalogue", "ModeProfile", "build_mode_catalogue"]
