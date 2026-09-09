"""The permission papers: what the gate may say about one tool call.

The gate (``raven.permissions``) decides before dispatch; these are the only
shapes it is allowed to answer with, and the only shapes an approval transport
is allowed to answer a human's click with. Contracts only -- nothing here
imports the gate, a broker, or a tool.

``Deny`` names its source because the refusals are acted on: a user rule is the
reader's own config to edit, a builtin refusal is not. ``NeedsApproval`` is a
declaration, not an interaction -- whether anyone can actually be asked belongs
to the turn, and an unattended turn upgrades it to ``Deny`` where the gate runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Tier(StrEnum):
    """One tool's standing: run it, ask a human first, or refuse it."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionMode(StrEnum):
    """How the ask tier is read. Only the ask tier: builtin rulings and user
    deny rules hold in every mode."""

    ASK = "ask"
    SMART = "smart"
    FULL = "full"


class DecisionSource(StrEnum):
    """Which rule answered, in the words the refusal text names."""

    BUILTIN_DENY = "builtin_deny"
    BUILTIN_PARSE_ERROR = "builtin_parse_error"
    USER_DENY = "user_deny"
    USER_ALLOW = "user_allow"
    MODE = "mode"
    JUDGE = "judge"
    JUDGE_ERROR = "judge_error"
    UNATTENDED = "unattended"
    APPROVAL = "approval"
    DEFAULT = "default"


@dataclass(frozen=True)
class Allow:
    source: DecisionSource = DecisionSource.DEFAULT


@dataclass(frozen=True)
class Deny:
    reason: str
    source: DecisionSource


@dataclass(frozen=True)
class NeedsApproval:
    """The call may run only on a human's click.

    ``digest`` keys the per-turn dedup so one denied call is not re-asked within
    the same turn; ``description`` is the line the human is shown. From the
    builtin rulings this is a description, not a verdict: a command family the
    surface declared names the prompt, and the gate decides whether one is
    shown at all. ``family`` is that family's name, recorded on the call's
    audit span whichever way the gate then decides; empty when no family
    matched.
    """

    reason: str
    description: str
    digest: str
    family: str = ""


Decision = Allow | Deny | NeedsApproval


class ApprovalChoice(StrEnum):
    """What the human's click meant. ``DENY_STOP`` is the one path that ends
    the turn; every other refusal continues it."""

    ALLOW = "allow"
    DENY = "deny"
    DENY_STOP = "deny_stop"


@dataclass(frozen=True)
class ApprovalOutcome:
    """One round-trip's result. ``feedback`` is the sentence a human attached to
    a refusal, verbatim; ``answered`` is false when nobody said anything."""

    choice: ApprovalChoice
    feedback: str = ""
    answered: bool = True

    @property
    def approved(self) -> bool:
        return self.choice is ApprovalChoice.ALLOW


__all__ = [
    "Allow",
    "ApprovalChoice",
    "ApprovalOutcome",
    "Decision",
    "DecisionSource",
    "Deny",
    "NeedsApproval",
    "PermissionMode",
    "Tier",
]
__tier__ = "contract"
