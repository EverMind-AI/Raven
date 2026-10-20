"""Sources: which data a deployment may reach for, and what it may do with it.

The sources an agent reaches for are not interchangeable. One lets you read
its data and build on it. One asks for its name to travel with the result.
One hands it over under a licence. One does not let you have it at all
unless you say why. A deployment that treats them the same is a deployment
that discovers the difference in a notice from one of them.

So the terms are held in one place, and the question is asked before the
request rather than after it. The refusal names the rule that was hit and
what would change the answer, because the person who can change it is the
operator and not the model.

A source that is not in the registry is refused. That is the direction to be
wrong in: a source added to the code without a row here has not been looked
at, and looking at it is the whole point of the table.
"""

from raven.sources.optins import (
    delete_optin,
    has_opt_in,
    optins_path,
    read_optins,
    write_optin,
)
from raven.sources.policy import (
    SOURCE_POLICIES,
    SourcePolicy,
    SourcePolicyError,
    SourceRefusedError,
    consult,
    policy_for,
)
from raven.sources.tiers import TIER_MEANING, SourceTier
from raven.sources.transport import PolicyClient, refusal_text, send

__all__ = [
    "SOURCE_POLICIES",
    "TIER_MEANING",
    "PolicyClient",
    "SourcePolicy",
    "SourcePolicyError",
    "SourceRefusedError",
    "SourceTier",
    "consult",
    "delete_optin",
    "has_opt_in",
    "optins_path",
    "policy_for",
    "read_optins",
    "refusal_text",
    "send",
    "write_optin",
]
