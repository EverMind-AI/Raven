"""One way in to the ChatGPT (Codex) credential LiteLLM's driver owns.

LiteLLM ships the device flow, the refresh and the account-id derivation for this
provider, so raven stops carrying its own: what is left to decide is where the
file lives and how to read it without starting a login.

That last part is the reason this module exists rather than three call sites
reaching for ``Authenticator``. Its ``get_access_token`` starts a device flow
whenever it cannot produce a token -- correct for ``provider login``, wrong for a
status report or a request the user is waiting on, where it prints a code to
stdout and polls for fifteen minutes.
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


def _read_auth_file() -> dict[str, Any] | None:
    try:
        data = json.loads(auth_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    return data if isinstance(data, dict) else None


def stored_credentials() -> dict[str, Any] | None:
    """The credential on disk, or None when there is nothing usable there.

    A refresh token alone counts: the driver exchanges it for an access token, so
    an expired access token is not the same as being signed out. Bookkeeping the
    driver leaves behind does not count -- an interrupted sign-in leaves a file
    with a timestamp in it and no way to authenticate.
    """
    data = _read_auth_file()

    return data if data and (data.get("access_token") or data.get("refresh_token")) else None


_EXPIRED = "ChatGPT credentials are no longer valid -- run `raven provider login openai-codex`"
_MISSING = "no ChatGPT credentials found -- run `raven provider login openai-codex`"


def clear_abandoned_device_code() -> bool:
    """Forget a device code nobody finished using. True when one was there.

    The driver stamps the auth file when it requests a code, and treats a stamp
    from the last five minutes as a flow already in progress: the next sign-in
    waits for that one to land instead of starting its own, printing nothing while
    it does. After an abandoned attempt -- the window closed, the code expired --
    that turns ``provider login`` into five silent minutes.
    """
    data = _read_auth_file()
    if not data or "device_code_requested_at" not in data:
        return False

    del data["device_code_requested_at"]
    if data:
        # Written through a temp file: this is the only copy of a credential, and
        # what is left of it after dropping one key is still the whole record.
        path = auth_file()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    else:
        auth_file().unlink(missing_ok=True)

    return True


def access_token_and_account() -> tuple[str, str | None]:
    """A token to send, refreshed if it had to be, plus the account it belongs to.

    Refreshing is allowed; logging in is not. Checking the file first only covers
    having no credential at all: a stored refresh token the server has since
    revoked gets a logged warning from the driver and then the same device flow,
    underneath whatever request asked for the token. So the two entry points to it
    are stubbed out for this call, leaving the stored token and a refresh of it as
    the only ways it can succeed.
    """
    from raven.providers.litellm_setup import import_litellm

    if stored_credentials() is None:
        raise RuntimeError(_MISSING)

    import_litellm()
    from litellm.llms.chatgpt.authenticator import Authenticator

    authenticator = Authenticator()

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(_EXPIRED)

    # Per-instance, so `provider login` -- which wants the device flow -- keeps it.
    authenticator._login_device_code = _refuse  # type: ignore[method-assign]
    authenticator._wait_for_access_token = _refuse  # type: ignore[method-assign]

    return authenticator.get_access_token(), authenticator.get_account_id()
