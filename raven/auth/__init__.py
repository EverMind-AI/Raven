"""Authentication & authorization primitives.

Three thin modules:

- ``allowlist``        — channel sender allowlist consolidation. Hosts
                          the canonical ``is_allowed(channel, sender_id,
                          allow_list)`` helper. ``channels/base.py`` now
                          delegates here instead of inlining the deny-by-
                          default check.
- ``capability_token`` — placeholder for the multi-agent capability
                          token design. Defines the dataclass + roundtrip
                          helpers; no enforcement code yet.
- ``managed_settings`` — placeholder for the company-deployment "this
                          config field is locked" mechanism. Returns a
                          plain ``ManagedSettings`` carrier; the actual
                          locking enforcement happens upstream once
                          there's a concrete operator need.

External callers should import from the sub-package paths directly.
"""

from raven.auth.allowlist import is_allowed
from raven.auth.capability_token import CapabilityToken, issue_token, verify_token
from raven.auth.managed_settings import ManagedSettings

__all__ = [
    "is_allowed",
    "CapabilityToken",
    "issue_token",
    "verify_token",
    "ManagedSettings",
]
