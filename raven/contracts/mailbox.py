"""Declared wire and local ownership shapes for the trusted-local mailbox."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__tier__ = "contract"
__all__ = ["MailboxEnvelope", "MailboxCard", "MailboxInstanceRef", "MailboxClaim", "MailboxResult", "MailboxError"]


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, serialize_by_alias=True)


class _Identity(_Model):
    authority_id: str
    tenant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    agent_id: str
    instance_id: str | None


class _Scope(_Model):
    task_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)


class _Artifact(_Model):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0, le=16777216)
    media_type: str = Field(max_length=128)
    name: str = Field(max_length=128)


class _Payload(_Model):
    content_type: Literal["application/json"]
    schema_id: str = Field(alias="schema")
    data: dict[str, Any]


class _Digest(_Model):
    algorithm: Literal["sha256"]
    canonicalization: Literal["oa-c14n-1"]
    value: str = Field(pattern=r"^[0-9a-f]{64}$")


class MailboxEnvelope(_Model):
    protocol_version: Literal["1.0"]
    message_id: str
    trace_id: str
    in_reply_to: str | None
    kind: Literal["task.request", "task.result", "handoff.offer", "handoff.accept", "receipt", "event"]
    sender_identity: _Identity
    target_identity: _Identity
    scope: _Scope
    created_at: str
    ttl: int = Field(ge=1, le=604800)
    receipt_policy: Literal["none", "terminal", "received_and_terminal"]
    payload: _Payload
    artifacts: list[_Artifact] = Field(max_length=32)
    extensions: dict[str, Any]
    digest: _Digest
    signature: Any


class _Capability(_Model):
    name: str
    version: str
    evidence: Literal["declared", "observed", "verified"]


class _Limits(_Model):
    max_envelope_bytes: int = Field(ge=1, le=65536)
    max_in_flight: int = Field(ge=1)


class MailboxCard(_Model):
    card_version: Literal["1.0"]
    authority_id: str
    tenant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    agent_id: str
    display_name: str
    brand: str
    instance_id: str | None
    generation: int = Field(ge=0)
    protocol_versions: list[Literal["1.0"]]
    capabilities: list[_Capability]
    limits: _Limits


class MailboxInstanceRef(_Model):
    authority_id: str
    tenant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    agent_id: str
    instance_id: str
    generation: int = Field(ge=1)


class MailboxClaim(_Model):
    envelope: MailboxEnvelope
    instance_ref: MailboxInstanceRef
    claim_token: str
    generation: int = Field(ge=1)
    attempt: int = Field(ge=1)
    lease_until: int = Field(ge=0)


class MailboxResult(_Model):
    outcome: Literal["succeeded", "failed", "blocked"]
    summary: str
    evidence: list[str]


class MailboxError(ValueError):
    """A diagnostic protocol code with no claim credentials or payload contents."""

    def __init__(self, code: str, *, retryable: bool = False, message_id: str | None = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.message_id = message_id
