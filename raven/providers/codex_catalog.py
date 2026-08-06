"""Which models a Codex account can actually use.

The registry's default and the models LiteLLM's table lists for this provider are
both guesses that the account does not honor: asked for `gpt-5-codex`, the backend
answers that the model is not supported with a ChatGPT account, and the slugs an
account does offer (`gpt-5.6-sol`, `gpt-5.6-terra`, ...) appear in no static list
we carry. The account is the only source that knows, so the picker asks it.

Hidden entries are dropped. The catalogue marks some (`codex-auto-review`, a
watermarked variant) `visibility: "hide"` -- they are reachable but not meant to
be offered.
"""

from __future__ import annotations

import time
from typing import Any

CATALOG_URL = "https://chatgpt.com/backend-api/codex/models"

#: Sent because the endpoint takes it; a live account answered with its models for
#: this value. What it does with an unknown one, or with none, is not established.
CLIENT_VERSION = "0.0.0"

_CACHE_TTL_SECONDS = 300

#: Shorter for a failure than for an answer: not being signed in yet, and being
#: offline, are states that change -- an account's entitlements are not.
_FAILURE_TTL_SECONDS = 30

#: (credential fingerprint, monotonic stamp, slugs)
_cache: tuple[object, float, tuple[str, ...]] | None = None
#: (credential fingerprint, monotonic stamp)
_failure: tuple[object, float] | None = None


def _credential_fingerprint() -> object:
    """Something that changes when the credential does, cheaply.

    The cache has to belong to the account it was fetched for. A signed-out
    failure, or one account's models, must not be served to the next account --
    and the sign-in that changes accounts happens in another process, so there is
    no call to invalidate on. The file the driver writes is the shared fact both
    processes see.
    """
    from raven.providers.chatgpt_token import auth_file

    try:
        stat = auth_file().stat()
    except OSError:
        return None

    return (stat.st_mtime_ns, stat.st_size)


def account_models(*, timeout: float = 5.0, strict: bool = False) -> tuple[str, ...]:
    """Slugs this account offers, newest first, or empty when it cannot be asked.

    Cached briefly: the picker rebuilds its list on every refresh, and a sign-in
    does not change what an account is entitled to from one keystroke to the next.
    Failures are empty rather than raised -- a provider list that cannot reach the
    network is still worth showing -- and are cached too, or every refresh made
    offline pays the full timeout again.

    ``strict`` raises instead, for the one caller whose whole job is to report why:
    a report that cannot tell "offline" from "this account has nothing" sends the
    user to fix the wrong thing. It also leaves no failure behind: the caller is
    reporting on this moment, and the picker should not inherit a verdict from a
    report it did not ask for.
    """
    global _cache, _failure

    now = time.monotonic()
    fingerprint = _credential_fingerprint()
    if _cache is not None and _cache[0] == fingerprint and now - _cache[1] < _CACHE_TTL_SECONDS:
        return _cache[2]
    if not strict and _failure is not None and _failure[0] == fingerprint and now - _failure[1] < _FAILURE_TTL_SECONDS:
        return ()

    try:
        slugs = _fetch(timeout=timeout)
    except Exception:
        if strict:
            raise
        _failure = (fingerprint, now)
        return ()

    _cache = (fingerprint, now, slugs)
    _failure = None

    return slugs


def reset_cache() -> None:
    """Forget both cached answers, for a caller that wants this instant's answer."""
    global _cache, _failure

    _cache = None
    _failure = None


def _fetch(*, timeout: float) -> tuple[str, ...]:
    import httpx

    from raven.providers.chatgpt_token import access_token_and_account

    access, account_id = access_token_and_account()
    headers = {"Authorization": f"Bearer {access}"}
    if account_id:
        headers["chatgpt-account-id"] = account_id

    response = httpx.get(
        CATALOG_URL,
        params={"client_version": CLIENT_VERSION},
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()

    return _visible_slugs(response.json())


def _visible_slugs(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()

    out: list[str] = []
    for entry in payload.get("models") or ():
        if not isinstance(entry, dict) or entry.get("visibility") == "hide":
            continue
        slug = entry.get("slug")
        if isinstance(slug, str) and slug:
            out.append(slug)

    return tuple(out)
