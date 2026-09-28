"""Current mechanisms projected by responsibility and channel, independently of grants."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Role = Literal["memory", "planning", "capability", "action"]
Channel = Literal["model_input", "model_decision", "tool_interaction", "execution_control"]


class Mechanism(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    roles: tuple[Role, ...]
    channels: tuple[Channel, ...]
    description: str
    components: tuple[tuple[str | int, ...], ...]
    sources: tuple[str, ...]
    targets: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()


def project(mechanisms, *, role: Role | None = None, channel: Channel | None = None):
    return tuple(
        item
        for item in mechanisms
        if (role is None or role in item.roles) and (channel is None or channel in item.channels)
    )


def validate(mechanisms, facts, sources, declaration):
    names = set()
    for item in mechanisms:
        if item.name in names:
            raise ValueError(f"duplicate mechanism: {item.name}")
        names.add(item.name)
        for path in item.components:
            if not path:
                raise ValueError(f"empty component reference: {item.name}")
            value = facts
            for key in path:
                value = value[key]
        for source in item.sources:
            if source not in sources:
                raise ValueError(f"unknown mechanism source: {source}")
        for target in item.targets:
            declaration.target(target)
