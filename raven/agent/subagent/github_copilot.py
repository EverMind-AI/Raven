"""Why GitHub Copilot would not answer, read from the words it actually sends.

Measured 2026-09-24 on GitHub Copilot CLI 1.0.88. A missing GitHub token fails
the session with a bare ``Authentication required``, which the shared
credential reading already names as ``copilot login``. Everything the model
call itself refuses, Copilot writes into the assistant message and ends the
turn, so a reader that only looks at the turn status records a success.

The sentences below are the ones that came back, and only those. A 429 and a
provider that never answers both outlast the wait and carry no sentence, so
they stay the shared timeout. A plan that does not include a model was
rewritten by Copilot into the API-key sentence, so it is read as that.
"""

from __future__ import annotations

import re
from typing import Any

from raven.agent.subagent.probe_state import Remedy

_KEY = re.compile(r"authentication failed with provider.*\(http\s+40[13]\)", re.IGNORECASE | re.DOTALL)
_CREDIT = re.compile(r"\berror:\s*402\b|credit balance is too low", re.IGNORECASE)
_SUBSCRIPTION = re.compile(r"subscription has expired", re.IGNORECASE)
_MODEL = re.compile(r"model '[^']+' not found|\(http\s+404\)", re.IGNORECASE)
_UNREACHED = re.compile(
    r"could not connect to local model provider|tunnel error|error sending request for url",
    re.IGNORECASE,
)
_SERVER = re.compile(r"last error:\s*50[02]\b|502 bad gateway", re.IGNORECASE)


def applies(cfg: Any) -> bool:
    """True only for the shipped GitHub Copilot ACP preset."""
    return getattr(cfg, "preset", None) == "github_copilot" and getattr(cfg, "kind", None) == "acp"


def read(said: str) -> tuple[str, Remedy | None] | None:
    """The fix for one of Copilot's measured refusals, or ``None`` to leave it.

    ``None`` includes a bare ``Authentication required``: that one is a missing
    sign-in, and the shared reading names ``copilot login`` for it. Classifying
    it here would hide that command.
    """
    if _KEY.search(said):
        # Not `api_key`: that kind is the sheet's key field, and an ACP row has
        # none. The variable Copilot names is the action, so it stays in the
        # record instead of being folded under a control that is not there.
        return (
            "its model provider refused the API key it is set up with; "
            "change COPILOT_PROVIDER_API_KEY (or COPILOT_PROVIDER_API_KEY_COMMAND, "
            "or COPILOT_PROVIDER_BEARER_TOKEN) and connect again. "
            f"It said: {said}",
            None,
        )
    if _SUBSCRIPTION.search(said):
        # `plan`, not `billing`: billing tells the reader to add provider credit
        # or switch models, and neither renews the Copilot subscription.
        return (
            f"its subscription has expired; renew the plan and connect again. It said: {said}",
            Remedy("plan"),
        )
    if _CREDIT.search(said):
        return (
            "its model provider refused the call for want of credit; "
            "add credit with the provider and connect again. "
            f"It said: {said}",
            Remedy("billing"),
        )
    if _MODEL.search(said):
        return (
            "its model provider does not serve the model it is set to use; "
            "switch the model it uses and connect again. "
            f"It said: {said}",
            Remedy("model"),
        )
    if _UNREACHED.search(said):
        return (
            "it could not reach its model provider; "
            "check the network, the proxy and the address it is configured with. "
            f"It said: {said}",
            Remedy("network"),
        )
    if _SERVER.search(said):
        return (f"its model provider returned a server error. It said: {said}", None)
    return None
