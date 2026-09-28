"""Raven-supported targets and factory references for the shared declaration."""

from dataclasses import replace
from typing import Annotated

from pydantic import Field, RootModel

from raven.plugins.manifest import _FACTORY_REF_RE

from ...harness.state import StateUse


class EntryPoint(RootModel[Annotated[str, Field(pattern=_FACTORY_REF_RE)]]):
    """A supplied or generated module:symbol reference; each binding checks the loaded kind.

    For HostWiring.hooks targets the symbol is a zero-argument factory that returns the participant
    instance whose selected methods run; it is never the method itself.
    """


PARTICIPANT_STATE = StateUse(
    resource="AgentParticipant instance",
    scope="turn",
    access="Own instance attributes; StepView is a read-only observation.",
    lifecycle="ParticipantHook creates one instance per turn; its attributes do not survive the turn.",
)

PLUGIN_STATE = StateUse(
    resource="Plugin instance and granted services",
    scope="generation",
    access="PluginContext and explicitly granted RuntimeHandles; no mutation of host assembly.",
    lifecycle="Constructed with the runtime generation; the host starts and releases applicable resources.",
)


def catalogue():
    """Return candidates; actual reachability and the manual determine grants."""
    from . import action, capability, memory, planning, prompts
    from .knowledge import complete

    targets = (*memory.TARGETS, *planning.TARGETS, *capability.TARGETS, *action.TARGETS, *prompts.TARGETS)
    return tuple(replace(target, knowledge=complete(target.knowledge)) for target in targets)
