"""Proxy-env manipulation for LAN vLLM endpoints.

Corporate HTTP(S)_PROXY intercepts LAN traffic and returns 502/503. Two
patterns are used across the codebase:

- ``bypass_proxy_for_url(url)``: append the URL's host to ``no_proxy`` so
  libraries that honour the env var skip the proxy for that host.
- ``strip_proxy_env_vars(env)``: return a copy of the env dict with all
  http_proxy / https_proxy / all_proxy / no_proxy stripped — used when we
  spawn a subprocess whose HTTP client doesn't reliably honour no_proxy.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

_PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def bypass_proxy_for_url(url: str) -> None:
    """Add the URL's host to ``no_proxy`` (idempotent)."""
    host = urlparse(url).hostname
    if not host:
        return
    existing = os.environ.get("no_proxy", "")
    if host in existing:
        return
    new = f"{existing},{host}".lstrip(",")
    os.environ["no_proxy"] = new
    os.environ["NO_PROXY"] = new


def strip_proxy_env_vars(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``env`` with proxy vars removed.

    Defaults to a copy of ``os.environ``; pass an explicit dict to strip from
    a pre-built subprocess env.
    """
    src = env if env is not None else os.environ
    return {k: v for k, v in src.items() if k.lower() not in _PROXY_KEYS}


__all__ = ["bypass_proxy_for_url", "strip_proxy_env_vars"]
