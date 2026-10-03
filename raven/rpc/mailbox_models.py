"""Strict RPC shapes for reliable messages, handoff and host-authorized DAGs."""

from typing import Annotated, Literal

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


class MailboxDagRevision(MailboxEmptyParams):
    repo_id: Literal["workspace"]
    ref: str


class MailboxDagCheck(MailboxEmptyParams):
    check_id: str
    argv: list[str] = Field(min_length=1)
    cwd: str
    timeout_seconds: int = Field(ge=1)
    repairable_exit_codes: list[int] = Field(default_factory=list)
    nonrepairable_exit_codes: list[int] = Field(default_factory=list)


class MailboxDagNodePolicy(MailboxEmptyParams):
    logical_node_id: str
    depends_on: list[str] = Field(default_factory=list)
    output_paths: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    checks: list[MailboxDagCheck] = Field(default_factory=list)
    subjective_review: bool = False


class MailboxDagCreateParams(MailboxBindingParams):
    request_id: str
    history_root: str | None = None
    session_key: str
    graph: dict
    revision_selector: MailboxDagRevision
    node_policies: list[MailboxDagNodePolicy]
    max_auto_repairs: int = Field(default=3, ge=0)


class MailboxDagStartParams(MailboxBindingParams):
    root_id: str
    request_id: str
    expected_owner_epoch: int = Field(ge=0)


class MailboxDagRecoverParams(MailboxDagStartParams):
    expected_executor_id: str
    replace_owner: bool = False


class MailboxDagStatusParams(MailboxParams):
    root_id: str


class MailboxDagSubjectiveResolution(MailboxEmptyParams):
    action: Literal["subjective_approve"]
    snapshot_fingerprint: str
    decision_note: str


class MailboxDagResultKey(MailboxEmptyParams):
    recipient_agent_id: str
    message_id: str
    digest: str


class MailboxDagExecutionResolution(MailboxEmptyParams):
    action: Literal["reconcile_execution"]
    result_key: MailboxDagResultKey


class MailboxDagVerificationResolution(MailboxEmptyParams):
    action: Literal["reconcile_verification"]
    check_run_id: str


class MailboxDagAbandonResolution(MailboxEmptyParams):
    action: Literal["abandon"]
    decision_note: str


class MailboxDagReplanResolution(MailboxEmptyParams):
    action: Literal["replan"]
    successor_graph: dict
    successor_verification_plan: dict
    logical_node_mapping: dict[str, str]
    decision_note: str


class MailboxDagResolveParams(MailboxDagStartParams):
    attempt_id: str
    resolution: Annotated[
        MailboxDagSubjectiveResolution
        | MailboxDagExecutionResolution
        | MailboxDagVerificationResolution
        | MailboxDagAbandonResolution
        | MailboxDagReplanResolution,
        Field(discriminator="action"),
    ]


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
    "mailbox.dag.create": (MailboxDagCreateParams, MailboxDataResult),
    "mailbox.dag.start": (MailboxDagStartParams, MailboxDataResult),
    "mailbox.dag.recover": (MailboxDagRecoverParams, MailboxDataResult),
    "mailbox.dag.resolve": (MailboxDagResolveParams, MailboxDataResult),
    "mailbox.dag.status": (MailboxDagStatusParams, MailboxDataResult),
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
        "mailbox.dag.status",
    }
)
