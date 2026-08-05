"""One way in to the ChatGPT (Codex) credential LiteLLM's driver owns.

LiteLLM ships the device flow, the refresh and the account-id derivation for this
provider, so raven stops carrying its own: what is left to decide is where the
file lives and how to read it without starting a login.

That last part is the reason this module exists rather than three call sites
reaching for ``Authenticator``. Its ``get_access_token`` starts a device flow when
it finds no token -- correct for ``provider login``, wrong for a status report or
a request that should fail with something the user can act on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def token_dir() -> Path:
    """Where the driver reads and writes this provider's credential.

    ``import_litellm`` points ``CHATGPT_TOKEN_DIR`` at raven's OAuth directory
    before LiteLLM is imported; reading the same variable here keeps one answer
    even when a user has set it themselves.
    """
    from raven.config.paths import get_oauth_dir

    configured = os.environ.get("CHATGPT_TOKEN_DIR")
    return Path(configured).expanduser() if configured else get_oauth_dir() / "chatgpt"


def auth_file() -> Path:
    return token_dir() / (os.environ.get("CHATGPT_AUTH_FILE") or "auth.json")


def stored_credentials() -> dict[str, Any] | None:
    """The credential on disk, or None when there is nothing usable there.

    A refresh token alone counts: the driver exchanges it for an access token, so
    an expired access token is not the same as being signed out.
    """
    try:
        data = json.loads(auth_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    return data if data.get("access_token") or data.get("refresh_token") else None


def access_token_and_account() -> tuple[str, str | None]:
    """A token to send, refreshed if it had to be, plus the account it belongs to.

    Raises when there is no stored credential, rather than letting the driver open
    a device flow underneath a request the user is waiting on.
    """
    from raven.providers.litellm_setup import import_litellm

    if stored_credentials() is None:
        raise RuntimeError("no ChatGPT credentials found -- run `raven provider login openai-codex`")

    import_litellm()
    from litellm.llms.chatgpt.authenticator import Authenticator

    authenticator = Authenticator()
    return authenticator.get_access_token(), authenticator.get_account_id()
