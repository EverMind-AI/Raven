"""Terminal lifecycle and addressed delivery subscription events."""

from typing import Literal

from pydantic import Field

from raven.contracts.terminal import TerminalRecord
from raven.rpc.terminal_models import TerminalParams


class TerminalCreatedEvent(TerminalParams):
    type: Literal["terminal.created"]
    payload: TerminalRecord


class TerminalClosedPayload(TerminalParams):
    handle: str


class TerminalClosedEvent(TerminalParams):
    type: Literal["terminal.closed"]
    payload: TerminalClosedPayload


class TerminalStatusPayload(TerminalClosedPayload):
    status: Literal["working", "idle", "permission", "unknown"]
    liveness: Literal["live", "exited", "unverifiable"]


class TerminalStatusEvent(TerminalParams):
    type: Literal["terminal.status"]
    payload: TerminalStatusPayload


class A2aSendPayload(TerminalClosedPayload):
    state: Literal["accepted", "queued", "blocked", "stalled", "delivered_to_host"]
    nonce: str | None


class A2aSendEvent(TerminalParams):
    type: Literal["a2a.send"]
    payload: A2aSendPayload


class A2aAckMatchedPayload(TerminalParams):
    handle: str | None = None
    nonce: str | None
    ack_for: str
    sender: str = Field(alias="from")
    to: str


class A2aAckMatchedEvent(TerminalParams):
    type: Literal["a2a.ack.matched"]
    payload: A2aAckMatchedPayload
