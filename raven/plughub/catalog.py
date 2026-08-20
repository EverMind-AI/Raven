"""Catalog source for the plugin market.

v1 ships a curated catalog inside the wheel (``catalog.json``); a hosted
hub can override it later via ``RAVEN_PLUGHUB_URL`` without touching the
callers — search/detail keep the same shapes either way. Entries are data,
never code: the riskiest thing a catalog entry can carry is a stdio command
line, which the GUI surfaces verbatim behind an explicit confirm.

An ``mcp`` contribution's ``auth`` block may carry an ``endpoints`` object
alongside ``mode``/``scopes_hint``. It is the authorization server's own
metadata (``issuer``, ``authorizationEndpoint``, ``tokenEndpoint``,
``registrationEndpoint``, ``scopes``, ``resource``, and optionally a
pre-registered public ``clientId`` with the ``redirectUri`` it is registered
under). An install copies it verbatim into the server's ``oauth`` config stanza,
where it saves the connect the discovery fetches; ``MCPOAuthConfig`` documents
each field and ``mcp_oauth._CatalogSeed`` what is done with it. Omitting the
block is the discovery path, unchanged, so an entry only needs it once someone
has read the service's well-known documents and copied them.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib.resources import files
from typing import Any

from loguru import logger

_HUB_ENV = "RAVEN_PLUGHUB_URL"
_HUB_TIMEOUT = 10.0
# The bundled catalog is 56 KB; anything an order of magnitude past that is not
# a catalog, and buffering it whole would be the gateway process paying for it.
_MAX_CATALOG_BYTES = 8 * 1024 * 1024


@lru_cache(maxsize=1)
def _bundled() -> dict:
    raw = files("raven.plughub").joinpath("catalog.json").read_text(encoding="utf-8")
    return json.loads(raw)


async def _load() -> dict:
    from raven.plughub.trust import hub_endpoint

    raw = os.environ.get(_HUB_ENV, "").strip()
    if not raw:
        return _bundled()
    # Outside the try on purpose: an unreachable hub degrades to the bundled
    # catalog, but a hub we are not allowed to trust must not silently become one
    # we read anyway. Raising also tells the operator their override was refused
    # instead of leaving them to wonder why the catalog never changed.
    hub = hub_endpoint(raw, raw, what=_HUB_ENV)
    import httpx

    try:
        # No redirects and a byte ceiling: the endpoint is the operator's own URL,
        # so it has no business pointing raven elsewhere, and a catalog that does
        # not fit the cap is not a catalog.
        async with httpx.AsyncClient(timeout=_HUB_TIMEOUT, follow_redirects=False) as client:
            async with client.stream("GET", f"{hub}/openapi/v1/plugins/catalog") as resp:
                resp.raise_for_status()
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > _MAX_CATALOG_BYTES:
                        raise ValueError(f"catalog exceeds {_MAX_CATALOG_BYTES} bytes")
            data = json.loads(bytes(buf))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data
            logger.warning("plughub: hub returned an unexpected catalog shape; using bundled catalog")
    except Exception as e:  # noqa: BLE001 — market must degrade, never break the page
        logger.warning("plughub: hub unreachable ({}); using bundled catalog", e)
    return _bundled()


def _text(value: Any, lang: str) -> str:
    """Resolve an i18n dict ({'zh':…, 'en':…}) or plain string."""
    if isinstance(value, dict):
        return str(value.get(lang) or value.get("en") or next(iter(value.values()), ""))
    return str(value or "")


def _matches(entry: dict, q: str, lang: str) -> bool:
    if not q:
        return True
    hay = " ".join(
        [
            entry.get("id", ""),
            _text(entry.get("name"), "zh"),
            _text(entry.get("name"), "en"),
            _text(entry.get("summary"), "zh"),
            _text(entry.get("summary"), "en"),
        ]
    ).lower()
    return q.lower() in hay


def _as_int(value: Any, default: int) -> int:
    """A hosted hub's field is whatever it sends; a card must still render."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _lite(entry: dict, lang: str) -> dict:
    """The card-sized projection of an entry."""
    contributes = entry.get("contributes") or []
    mcp = next((c for c in contributes if c.get("kind") == "mcp"), None)
    return {
        "id": entry.get("id"),
        "version": entry.get("version"),
        "name": _text(entry.get("name"), lang),
        "summary": _text(entry.get("summary"), lang),
        "category": entry.get("category") or "other",
        "verified": bool((entry.get("publisher") or {}).get("verified")),
        "publisher": (entry.get("publisher") or {}).get("name") or "",
        "risk_tier": _as_int(entry.get("risk_tier"), 1),
        "auth_mode": ((mcp or {}).get("auth") or {}).get("mode", "none"),
        "transport": (mcp or {}).get("connection", {}).get("type") if mcp else None,
        "tool_preview_count": len((mcp or {}).get("tools_preview") or []),
        "skill_count": sum(1 for c in contributes if c.get("kind") == "skill"),
        "kinds": sorted({c.get("kind") for c in contributes if c.get("kind")}),
    }


async def catalog_search(q: str = "", category: str = "", lang: str = "en") -> list[dict]:
    data = await _load()
    out = []
    for entry in data.get("entries", []):
        if category and entry.get("category") != category:
            continue
        if not _matches(entry, q, lang):
            continue
        out.append(_lite(entry, lang))
    return out


async def catalog_detail(entry_id: str) -> dict | None:
    """The full raw entry (the RPC layer localizes what it exposes)."""
    data = await _load()
    for entry in data.get("entries", []):
        if entry.get("id") == entry_id:
            return entry
    return None


async def catalog_suggest(q: str, lang: str = "en", limit: int = 5) -> list[dict]:
    """Card projections of the entries closest to ``q``, best first.

    For the "no such plugin" answer: a caller asked for ``asanna`` or for
    ``jira`` (which the catalog calls ``atlassian``), and a bare refusal makes
    them guess again. Substring hits come first and rank by how much of the
    entry they cover; the rest fall back to difflib similarity over the id and
    both languages of the name, so a near-miss spelling still surfaces.
    """
    from difflib import SequenceMatcher

    needle = (q or "").strip().lower()
    if not needle:
        return []
    data = await _load()
    scored: list[tuple[float, dict]] = []
    for entry in data.get("entries", []):
        names = [str(entry.get("id") or ""), _text(entry.get("name"), "en"), _text(entry.get("name"), "zh")]
        best = 0.0
        for name in [n.lower() for n in names if n]:
            if needle in name:
                # +1 keeps every substring hit ahead of every fuzzy one, and the
                # ratio inside that band prefers the tightest containment.
                best = max(best, 1.0 + len(needle) / len(name))
            else:
                best = max(best, SequenceMatcher(None, needle, name).ratio())
        if best >= 0.45:
            scored.append((best, entry))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("id") or "")))
    return [_lite(entry, lang) for _, entry in scored[:limit]]


async def catalog_categories() -> list[str]:
    data = await _load()
    seen: list[str] = []
    for entry in data.get("entries", []):
        cat = entry.get("category") or "other"
        if cat not in seen:
            seen.append(cat)
    return seen


__all__ = ["catalog_categories", "catalog_detail", "catalog_search", "catalog_suggest"]
