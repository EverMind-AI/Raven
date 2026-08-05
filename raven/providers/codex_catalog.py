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

#: The endpoint answers 200 with an empty catalogue when it does not recognize the
#: client version, so this is load-bearing rather than cosmetic.
CLIENT_VERSION = "0.0.0"

_CACHE_TTL_SECONDS = 300
_cache: tuple[float, tuple[str, ...]] | None = None


def account_models(*, timeout: float = 5.0) -> tuple[str, ...]:
    """Slugs this account offers, newest first, or empty when it cannot be asked.

    Cached briefly: the picker rebuilds its list on every refresh, and a sign-in
    does not change what an account is entitled to from one keystroke to the next.
    Failures are empty rather than raised -- a provider list that cannot reach the
    network is still worth showing.
    """
    global _cache

    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_SECONDS:
        return _cache[1]

    try:
        slugs = _fetch(timeout=timeout)
    except Exception:
        return ()

    _cache = (now, slugs)

    return slugs


def reset_cache() -> None:
    """Forget the cached catalogue (a fresh sign-in changes who is asking)."""
    global _cache

    _cache = None


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
