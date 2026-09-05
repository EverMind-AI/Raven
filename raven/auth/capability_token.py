"""Capability tokens — scaffold for multi-agent coordination.

Scaffolding only: this module defines the ``CapabilityToken``
dataclass and a deterministic HMAC-based issue / verify pair so future
work has a stable seam. There are no callers yet — AgentLoop / subagent
spawning don't consult tokens. Once a concrete multi-agent flow needs
"this subagent may only invoke these tools" enforcement, the wire-up
goes through these primitives.

Design choices:

- **Tokens are JSON + HMAC**, not JWT — we don't need the JWT
  algorithm-agility surface, and JSON keeps the payload readable in
  logs / debug dumps.
- **Tokens are issuer-bound by id**, not by rotating signing keys —
  the secret is the workspace-local config value. Rotation policy
  is an operator concern, deferred until there's a deployment that
  needs it.
- **Verification fails closed** — any structural mismatch, signature
  mismatch, or expiry returns ``None``. Callers should treat ``None``
  as "no capability".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

_SIG_ALGO = hashlib.sha256


@dataclass
class CapabilityToken:
    """One token grants one agent identity a bundle of capabilities."""

    agent_id: str
    capabilities: list[str] = field(default_factory=list)
    issued_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: int | None = None  # None = never expires
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capabilities": list(self.capabilities),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CapabilityToken":
        return cls(
            agent_id=str(payload["agent_id"]),
            capabilities=list(payload.get("capabilities") or []),
            issued_at=int(payload.get("issued_at", 0)),
            expires_at=(int(payload["expires_at"]) if payload.get("expires_at") is not None else None),
            metadata=dict(payload.get("metadata") or {}),
        )

    def is_expired(self, now: int | None = None) -> bool:
        if self.expires_at is None:
            return False
        ref = int(now if now is not None else time.time())
        return ref >= self.expires_at


def issue_token(token: CapabilityToken, secret: str) -> str:
    """Serialize and HMAC-sign a token. Returns ``payload.signature``
    where both halves are URL-safe base64.
    """
    raw = json.dumps(token.to_payload(), sort_keys=True, separators=(",", ":"))
    payload_b64 = _b64(raw.encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), _SIG_ALGO)
    sig_b64 = _b64(sig.digest())
    return f"{payload_b64}.{sig_b64}"


def verify_token(token_str: str, secret: str) -> CapabilityToken | None:
    """Reverse of :func:`issue_token`. Returns ``None`` on any failure
    mode (malformed, bad signature, expired, decode error).
    """
    if not isinstance(token_str, str) or token_str.count(".") != 1:
        return None
    payload_b64, sig_b64 = token_str.split(".", 1)
    expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), _SIG_ALGO)
    if not hmac.compare_digest(_b64(expected.digest()), sig_b64):
        return None
    try:
        raw = _unb64(payload_b64)
        payload = json.loads(raw.decode("utf-8"))
        token = CapabilityToken.from_payload(payload)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if token.is_expired():
        return None
    return token


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + padding).encode("ascii"))


__all__ = ["CapabilityToken", "issue_token", "verify_token"]
