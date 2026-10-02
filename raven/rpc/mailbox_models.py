"""Strict RPC shapes for scoped reliable messages and handoff."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from raven.contracts.mailbox import MailboxInstanceRef, MailboxResult


class MailboxParams(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    binding_id: str | None = None


class MailboxDataResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict


class MailboxEmptyParams(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class MailboxEnrollParams(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    agent_name: str
    ref: MailboxInstanceRef
    scope: dict[str, str]
    request_id: str
    capabilities: list[str] = Field(default_factory=lambda: ["poll", "artifact_read", "handoff"])


class MailboxBindingParams(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    binding_id: str


class MailboxSendParams(MailboxParams):
    envelope: dict


class MailboxPollParams(MailboxParams):
    request_id: str | None = None
    peek: bool = False
    limit: int = Field(default=1, ge=1, le=16)
    lease_seconds: int = Field(default=120, ge=10, le=3600)


class MailboxAckParams(MailboxParams):
    claim: dict
    result: MailboxResult | None = None
    action: Literal["finish", "retry", "reject"] = "finish"
    reason: str | None = None


class MailboxRenewParams(MailboxParams):
    claim: dict
    request_id: str
    lease_seconds: int = Field(default=120, ge=10, le=3600)


class MailboxStatusParams(MailboxParams):
    message_id: str


class MailboxMessagesParams(MailboxParams):
    limit: int = Field(default=50, ge=1, le=100)


class MailboxArtifactParams(MailboxParams):
    offer_message_id: str
    artifact_hash: str


class MailboxProposeParams(MailboxParams):
    offer_message_id: str
    accept_message_id: str


class MailboxTaskCreateParams(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    task_id: str
    workspace_id: str
    owner_agent_id: str
    request_id: str


class MailboxCommitParams(MailboxBindingParams):
    offer_message_id: str
    accept_message_id: str
    expected_owner_agent_id: str
    expected_assignment_epoch: int
    request_id: str


class MailboxNotifyParams(MailboxBindingParams):
    request_id: str
    message_ids: list[str]


class MailboxNotifyStatusParams(MailboxParams):
    request_id: str


class MailboxOverviewParams(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    task_id: str
    workspace_id: str
    terminal_handle: str | None = None


MAILBOX_METHOD_MODELS = {
    "mailbox.enroll": (MailboxEnrollParams, MailboxDataResult),
    "mailbox.revoke": (MailboxBindingParams, MailboxDataResult),
    "mailbox.bindings": (MailboxEmptyParams, MailboxDataResult),
    "mailbox.overview": (MailboxOverviewParams, MailboxDataResult),
    "mailbox.send": (MailboxSendParams, MailboxDataResult),
    "mailbox.poll": (MailboxPollParams, MailboxDataResult),
    "mailbox.ack": (MailboxAckParams, MailboxDataResult),
    "mailbox.renew": (MailboxRenewParams, MailboxDataResult),
    "mailbox.status": (MailboxStatusParams, MailboxDataResult),
    "mailbox.messages": (MailboxMessagesParams, MailboxDataResult),
    "mailbox.artifact.read": (MailboxArtifactParams, MailboxDataResult),
    "mailbox.handoff.propose": (MailboxProposeParams, MailboxDataResult),
    "mailbox.handoff.create": (MailboxTaskCreateParams, MailboxDataResult),
    "mailbox.handoff.commit": (MailboxCommitParams, MailboxDataResult),
    "mailbox.handoff.status": (MailboxParams, MailboxDataResult),
    "mailbox.notify": (MailboxNotifyParams, MailboxDataResult),
    "mailbox.notify.status": (MailboxNotifyStatusParams, MailboxDataResult),
}

RECEIVER_METHODS = frozenset(
    {
        "mailbox.send",
        "mailbox.poll",
        "mailbox.ack",
        "mailbox.renew",
        "mailbox.status",
        "mailbox.messages",
        "mailbox.artifact.read",
        "mailbox.handoff.propose",
        "mailbox.handoff.status",
        "mailbox.notify.status",
    }
)
