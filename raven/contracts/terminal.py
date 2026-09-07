"""Terminal identities, delivery results, and the A-O-compatible peer envelope."""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

__tier__ = "contract"

AGENT_PROMPT_BLOCKED = "agent_prompt_blocked"
AGENT_PROMPT_STALLED = "agent_prompt_stalled"
_PROCESS_RUNTIME_ID = str(uuid4())
_NAME = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_NONCE = r"a2a-[0-9a-f]{12}"
_HEADER = re.compile(
    rf"\A\[AO-A2A:v1\] from=({_NAME}) to=({_NAME}) scope=([^\s/]+/[^\s/]+) "
    rf"(?:terminal=([^\s]+) )?nonce=({_NONCE}) (?:ack_for=({_NONCE}) )?message:\n([\s\S]+)\Z"
)


class WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, validate_assignment=True)


class RuntimeInfo(WireModel):
    runtime_id: str = Field(default_factory=lambda: _PROCESS_RUNTIME_ID)


class TerminalRecord(WireModel):
    handle: str = Field(default_factory=lambda: f"term_{uuid4()}")
    incarnation_id: str = Field(default_factory=lambda: str(uuid4()))
    pty_id: str = ""
    tab_id: str = Field(default_factory=lambda: str(uuid4()))
    leaf_id: str = Field(default_factory=lambda: str(uuid4()))
    pane_key: str = ""
    worktree_id: str
    worktree_path: str
    execution_host_id: str = "local"
    title: str = ""
    status: Literal["idle", "working", "permission", "unknown"] = "unknown"
    liveness: Literal["live", "exited", "unverifiable"] = "unverifiable"
    connected: bool = False
    writable: bool = False
    orphaned: bool = False
    visible: bool = True
    owner: str = "human"
    last_output_at: int | None = None

    @model_validator(mode="after")
    def fill_identity(self) -> TerminalRecord:
        if not self.pty_id:
            object.__setattr__(self, "pty_id", f"{self.worktree_id}@@{secrets.token_hex(4)}")
        if not self.pane_key:
            object.__setattr__(self, "pane_key", f"{self.tab_id}:{self.leaf_id}")
        return self


class Envelope(BaseModel):
    sender: str = Field(pattern=rf"^{_NAME}$")
    recipient: str = Field(pattern=rf"^{_NAME}$")
    scope: str = Field(pattern=r"^[^\s/]+/[^\s/]+$")
    nonce: str = Field(default_factory=lambda: f"a2a-{secrets.token_hex(6)}", pattern=rf"^{_NONCE}$")
    ack_for: str | None = Field(default=None, pattern=rf"^{_NONCE}$")
    reply_terminal: str | None = None
    body: str = Field(min_length=1)

    @field_validator("reply_terminal")
    @classmethod
    def single_token(cls, value: str | None) -> str | None:
        if value is not None and (not value or re.search(r"\s", value)):
            raise ValueError("reply_terminal must be one non-empty token")
        return value

    def to_text(self) -> str:
        fields = ["[AO-A2A:v1]", f"from={self.sender}", f"to={self.recipient}", f"scope={self.scope}"]
        if self.reply_terminal is not None:
            fields.append(f"terminal={self.reply_terminal}")
        fields.append(f"nonce={self.nonce}")
        if self.ack_for is not None:
            fields.append(f"ack_for={self.ack_for}")
        return " ".join(fields) + " message:\n" + self.body

    @classmethod
    def from_text(cls, text: str) -> Envelope:
        match = _HEADER.fullmatch(text)
        if match is None:
            raise ValueError("invalid_a2a_envelope_input")
        sender, recipient, scope, terminal, nonce, ack_for, body = match.groups()
        return cls(
            sender=sender,
            recipient=recipient,
            scope=scope,
            reply_terminal=terminal,
            nonce=nonce,
            ack_for=ack_for,
            body=body,
        )


class HostScope(WireModel):
    host_ids: list[str] = Field(default_factory=lambda: ["local"])
    omitted_host_ids: list[str] = Field(default_factory=list)


class TerminalListResult(WireModel):
    terminals: list[TerminalRecord] = Field(default_factory=list)
    truncated: bool = False
    host_scope: HostScope = Field(default_factory=HostScope)
    topology_revisions: dict[str, int] = Field(default_factory=dict)
    visual_layouts: list[dict[str, Any]] | None = None


class SendResult(WireModel):
    handle: str
    accepted: bool
    bytes_written: int = 0
    state: Literal["accepted", "queued", "blocked", "stalled"]
    nonce: str | None = None
    deduplicated: bool = False
    content_ack: bool | None = None


class WaitResult(WireModel):
    handle: str
    satisfied: bool
    blocked_reason: str | None = None
    timed_out: bool = False


class TerminalError(Exception):
    def __init__(self, code: str, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def success_envelope(request_id: str | int | None, result: Any, runtime: RuntimeInfo | None = None) -> dict:
    return {
        "id": request_id,
        "ok": True,
        "result": result,
        "_meta": (runtime or RuntimeInfo()).model_dump(by_alias=True),
    }


def error_envelope(
    request_id: str | int | None, code: str, message: str, data: Any = None, runtime: RuntimeInfo | None = None
) -> dict:
    return {
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message, "data": data},
        "_meta": (runtime or RuntimeInfo()).model_dump(by_alias=True),
    }


def worktree_id(path: str | Path, *, repo_id: str | None = None) -> str:
    resolved = Path(path).resolve()
    if repo_id is None:
        git = shutil.which("git")
        if git is None:
            raise FileNotFoundError("git is required to identify a worktree")
        result = subprocess.run(
            [git, "-C", str(resolved), "rev-list", "--max-parents=0", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        repo_id = result.stdout.splitlines()[0]
    if not repo_id or "::" in repo_id:
        raise ValueError("invalid repository id")
    return f"{repo_id}::{resolved}"


__all__ = [
    "AGENT_PROMPT_BLOCKED",
    "AGENT_PROMPT_STALLED",
    "Envelope",
    "HostScope",
    "RuntimeInfo",
    "SendResult",
    "TerminalError",
    "TerminalListResult",
    "TerminalRecord",
    "WaitResult",
    "error_envelope",
    "success_envelope",
    "worktree_id",
]
