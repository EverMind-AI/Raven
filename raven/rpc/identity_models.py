"""Public identity registry request and response models."""

from typing import Literal

from pydantic import Field

from raven.agent.registry.identity import IdentityAlias, IdentityRecord
from raven.contracts.terminal import RuntimeInfo
from raven.rpc.terminal_models import TerminalParams


class AgentsRegisterParams(TerminalParams):
    name: str
    kind: str | None = None
    terminal: str | None = None
    role: str = ""
    task_ref: str = ""
    description: str | None = None
    aliases: list[IdentityAlias] | None = None


class AgentsShowParams(TerminalParams):
    name: str


class AgentsResolveParams(TerminalParams):
    mention: str


class IdentityResult(TerminalParams):
    meta: RuntimeInfo = Field(alias="_meta")


class AgentsRecordResult(IdentityResult):
    agent: IdentityRecord


class AgentsListResult(IdentityResult):
    agents: list[IdentityRecord]


class IdentityCandidate(TerminalParams):
    agent: IdentityRecord
    reason: Literal["exact", "alias"]


class AgentsResolveResult(IdentityResult):
    candidates: list[IdentityCandidate]
    unique: bool


IDENTITY_METHOD_MODELS = {
    "agents.register": (AgentsRegisterParams, AgentsRecordResult),
    "agents.list": (TerminalParams, AgentsListResult),
    "agents.show": (AgentsShowParams, AgentsRecordResult),
    "agents.resolve": (AgentsResolveParams, AgentsResolveResult),
}
