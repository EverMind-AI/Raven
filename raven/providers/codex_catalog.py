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

_cache: tuple[float, tuple[str, ...]] | None = None
_failed_at: float | None = None


def account_models(*, timeout: float = 5.0, strict: bool = False) -> tuple[str, ...]:
    """Slugs this account offers, newest first, or empty when it cannot be asked.

    Cached briefly: the picker rebuilds its list on every refresh, and a sign-in
    does not change what an account is entitled to from one keystroke to the next.
    Failures are empty rather than raised -- a provider list that cannot reach the
    network is still worth showing -- and are cached too, or every refresh made
    offline pays the full timeout again.

    ``strict`` raises instead, for the one caller whose whole job is to report why:
    a report that cannot tell "offline" from "this account has nothing" sends the
    user to fix the wrong thing.
    """
    global _cache, _failed_at

    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_SECONDS:
        return _cache[1]
    if _failed_at is not None and now - _failed_at < _FAILURE_TTL_SECONDS and not strict:
        return ()

    try:
        slugs = _fetch(timeout=timeout)
    except Exception:
        _failed_at = now
        if strict:
            raise
        return ()

    _cache = (now, slugs)
    _failed_at = None

    return slugs


def reset_cache() -> None:
    """Forget both cached answers (a fresh sign-in changes who is asking)."""
    global _cache, _failed_at

    _cache = None
    _failed_at = None


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
