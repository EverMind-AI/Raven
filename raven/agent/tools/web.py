"""Web tools: web_search and web_fetch, each behind a selectable vendor.

One tool per capability, several backends behind each. What differs per vendor
is the request it expects and the payload it answers with; everything the model
sees -- the rendered result list, the fetch envelope -- is one code path, so
each provider is normalised to the Serper (search) or Jina (fetch) shape before
anything reads it. The Serper and Jina branches are byte-identical to what
these tools always sent; do not tidy them into the others.
"""

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from raven.config.schema import WEB_VENDOR_ENV_VARS
from raven.contracts.tool import Tool
from raven.security.network import validate_url_target


@dataclass(frozen=True)
class SearchProviderSpec:
    """One ``web_search`` backend: its name, its bare env var, its sign-up page."""

    vendor: str
    label: str
    env_var: str
    signup: str

    @property
    def config_path(self) -> str:
        return f"tools.web.providers.{self.vendor}.apiKey"


@dataclass(frozen=True)
class FetchProviderSpec:
    """One ``web_fetch`` backend.

    ``needs_key`` decides whether the backend can be offered at all: Jina reads
    pages unauthenticated at a lower rate limit, the others refuse without a
    key. ``extractor`` is what the fetch envelope reports as having served the
    page; the backends are not interchangeable, so it is a measurement, not a
    label.
    """

    vendor: str
    label: str
    env_var: str
    signup: str
    extractor: str
    needs_key: bool

    @property
    def config_path(self) -> str:
        return f"tools.web.providers.{self.vendor}.apiKey"


DEFAULT_SEARCH_PROVIDER = "serper"
DEFAULT_FETCH_PROVIDER = "jina"

SEARCH_PROVIDERS: dict[str, SearchProviderSpec] = {
    "serper": SearchProviderSpec("serper", "Serper", WEB_VENDOR_ENV_VARS["serper"], "https://serper.dev"),
    "anysearch": SearchProviderSpec(
        "anysearch", "AnySearch", WEB_VENDOR_ENV_VARS["anysearch"], "https://anysearch.com"
    ),
    "serpapi": SearchProviderSpec("serpapi", "SerpApi", WEB_VENDOR_ENV_VARS["serpapi"], "https://serpapi.com"),
    "tavily": SearchProviderSpec("tavily", "Tavily", WEB_VENDOR_ENV_VARS["tavily"], "https://tavily.com"),
    "exa": SearchProviderSpec("exa", "Exa", WEB_VENDOR_ENV_VARS["exa"], "https://exa.ai"),
    "brave": SearchProviderSpec("brave", "Brave Search", WEB_VENDOR_ENV_VARS["brave"], "https://brave.com/search/api"),
    "firecrawl": SearchProviderSpec(
        "firecrawl", "Firecrawl", WEB_VENDOR_ENV_VARS["firecrawl"], "https://firecrawl.dev"
    ),
    "serply": SearchProviderSpec("serply", "Serply", WEB_VENDOR_ENV_VARS["serply"], "https://serply.io"),
}

FETCH_PROVIDERS: dict[str, FetchProviderSpec] = {
    "jina": FetchProviderSpec(
        "jina", "Jina Reader", WEB_VENDOR_ENV_VARS["jina"], "https://jina.ai/reader", "jina-reader", False
    ),
    "anysearch": FetchProviderSpec(
        "anysearch", "AnySearch", WEB_VENDOR_ENV_VARS["anysearch"], "https://anysearch.com", "anysearch-extract", True
    ),
    "tavily": FetchProviderSpec(
        "tavily", "Tavily", WEB_VENDOR_ENV_VARS["tavily"], "https://tavily.com", "tavily-extract", True
    ),
    "exa": FetchProviderSpec("exa", "Exa", WEB_VENDOR_ENV_VARS["exa"], "https://exa.ai", "exa-contents", True),
    "firecrawl": FetchProviderSpec(
        "firecrawl", "Firecrawl", WEB_VENDOR_ENV_VARS["firecrawl"], "https://firecrawl.dev", "firecrawl-scrape", True
    ),
}


def resolve_vendor_key(
    vendor: str, keys: dict[str, str] | None, serper_key: str | None, jina_key: str | None
) -> str | None:
    """One web vendor's key from values a host has already resolved.

    The order is ``WebToolsConfig.vendor_key``'s, expressed over plain values
    for the hosts that thread the named table and the two pre-vendor scalars
    separately rather than carrying the config object. Named vendors rather
    than the default constants: each scalar is one vendor's own pre-vendor
    leaf, so it must not follow a change of default to another vendor.
    """
    if key := (keys or {}).get(vendor):
        return key
    if vendor == "serper":
        return serper_key
    if vendor == "jina":
        return jina_key
    return None


# The statuses that are the vendor's verdict on the key or the account rather
# than on the page or the query: a rejected key, an account out of credit, a
# key that may not make this request. Every call after one of these meets the
# same answer until the key or the account changes, so the tool stops asking
# instead of failing the same way per call. A search vendor's API is the
# endpoint, so all three speak of the account (Serper answers a rejected key
# with 403). A reader's 403 can be about the URL instead: Jina answers an
# anonymous request for a domain it has blocked with 403, and Firecrawl
# answers 403 for a site its policy does not scrape, so a reader pauses on
# 401 and 402 only.
SEARCH_REFUSAL_STATUSES = frozenset({401, 402, 403})
FETCH_REFUSAL_STATUSES = frozenset({401, 402})
_REFUSAL_MEANING = {
    401: "the key was rejected",
    402: "the account is out of credit (payment required)",
    403: "the key is not allowed to make this request",
}
# How long a refusal keeps the tool from asking again. A topped-up account or a
# rotated key takes effect on the vendor's side within minutes, and the tool
# must not stay dead for the process's life once the user has fixed it. A key
# that changes (the tools read theirs live) lifts the pause at once.
VENDOR_REFUSAL_PAUSE_S = 600.0


class _VendorRefusal:
    """What a vendor last said about the key, so later calls stop asking.

    Held per tool rather than per session: the key is shared by every session
    of the process, so a refusal one session met is the answer every other
    session would get. ``note`` records a refusal status and says whether it
    was one; a status from a request that carried no key never is, since there
    was no key to refuse (Jina reads pages unauthenticated, and its 403 for a
    domain it blocks names the domain, not a key). ``active`` is the status a
    call with ``key`` would meet again, or ``None`` once the key changed or the
    pause ran out, at which point one real request goes through and re-arms
    the pause if it is refused again. Both are handed the vendor and the key
    the call resolved once at its start, which are what its request carried:
    the vendor is read live too (``tools.web.<kind>.provider``), so a refusal
    is one vendor's verdict on one key and says nothing about another vendor
    that happens to be given the same key; and read again at ``note`` time, a
    key replaced during the request's flight would be paused before it had
    ever been sent.
    """

    def __init__(self, statuses: frozenset[int]) -> None:
        self.statuses = statuses
        self.status: int | None = None
        self.vendor: str | None = None
        self.key: str | None = None
        self.at: float = 0.0

    def note(self, status: int, vendor: str, key: str) -> bool:
        if status not in self.statuses or not key:
            return False
        self.status, self.vendor, self.key, self.at = status, vendor, key, time.monotonic()
        return True

    def active(self, vendor: str, key: str) -> int | None:
        if self.status is None:
            return None
        if (vendor, key) != (self.vendor, self.key) or time.monotonic() - self.at >= VENDOR_REFUSAL_PAUSE_S:
            self.status = None
            return None
        return self.status


def refusal_text(
    spec: "SearchProviderSpec | FetchProviderSpec", status: int, *, kind: str, sent: bool
) -> tuple[str, str]:
    """The ``error`` and ``detail`` a refused key produces, for the model and the user.

    ``error`` is the same string for the refusing call and for every paused
    call after it, so the loop's failure streak counts them as one cause and
    its stop-repeating nudge fires. ``detail`` carries what changed with
    ``sent`` -- whether this call reached the vendor -- and what the user has
    to do, since a model cannot fix a key or a bill on its own.
    """
    meaning = _REFUSAL_MEANING[status]
    error = f"{spec.label} refused the key (HTTP {status})"
    outcome = (
        f"{meaning}; no further request will be sent to {spec.label} until the key changes or "
        f"{VENDOR_REFUSAL_PAUSE_S / 60:g} minutes pass"
        if sent
        else f"{meaning} on the last request, so this call was not sent"
    )
    detail = (
        f"{outcome}. Tell the user: {spec.label} needs attention -- set a working key at "
        f"{spec.config_path} or select another vendor under tools.web.{kind}.provider (both are read from the "
        f"config file without a restart), or restart with {spec.env_var} set; sign-up at {spec.signup}. "
        "Do not retry this tool until they have."
    )
    return error, detail


class _ProviderPageError(RuntimeError):
    """A fetch backend answered, but not with a page."""


def _check_provider(provider: "str | Callable[[], str]", known: dict, tool: str) -> None:
    """Refuse an unknown vendor at construction, when it is a fixed one.

    A live reader is checked per read instead (see ``_resolve_provider``): the
    file can name anything, and a turn is not the place to raise over it.
    """
    if not callable(provider) and provider not in known:
        raise ValueError(f"unknown {tool} provider {provider!r}; one of {sorted(known)}")


def _resolve_provider(source: "Callable[[], str] | None", fallback: str, known: dict) -> str:
    """The vendor for this call: what the reader answers, else the built-with one.

    An unknown name is not an error here. The file is read while a turn runs,
    and a typo in it must not take the tool down mid-call -- the vendor the
    tool was registered with answers instead, which is what it answered before
    the reader existed.
    """
    if source is None:
        return fallback
    try:
        named = source()
    except Exception:  # noqa: BLE001 - an unreadable file keeps the tool working
        return fallback
    return named if named in known else fallback


class WebSearchTool(Tool):
    """Search the web through the selected vendor."""

    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    def __init__(
        self,
        api_key: "str | Callable[[], str] | None" = None,
        max_results: int = 5,
        proxy: str | None = None,
        provider: "str | Callable[[], str]" = DEFAULT_SEARCH_PROVIDER,
    ):
        _check_provider(provider, SEARCH_PROVIDERS, "web_search")
        # A callable is the live form (a reader over the config file), so a key
        # added there serves the next call; a plain string stays a snapshot.
        self._api_key_source: "Callable[[], str] | None" = api_key if callable(api_key) else None
        self._init_api_key: str | None = None if callable(api_key) else api_key
        self._refusal = _VendorRefusal(SEARCH_REFUSAL_STATUSES)
        self.max_results = max_results
        self.proxy = proxy
        # The vendor is live for the same reason the key is, and they have to
        # move together: ``api_key`` resolves against ``spec``, so a vendor
        # frozen here would send the new vendor's key to the old endpoint.
        self._provider_source: "Callable[[], str] | None" = provider if callable(provider) else None
        self._init_provider: str = DEFAULT_SEARCH_PROVIDER if callable(provider) else provider

    @property
    def provider(self) -> str:
        return _resolve_provider(self._provider_source, self._init_provider, SEARCH_PROVIDERS)

    @property
    def spec(self) -> SearchProviderSpec:
        return SEARCH_PROVIDERS[self.provider]

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        configured = self._api_key_source() if self._api_key_source is not None else self._init_api_key
        return configured or os.environ.get(self.spec.env_var, "")

    def _resolve(self) -> tuple[str, str]:
        """The (vendor, key) pair one call runs on, each source read once.

        Read again on the way -- for the request, for the envelope, for the
        refusal it records -- a call could pair a key with a vendor its request
        never reached, or pause a key it never sent.
        """
        vendor = self.provider
        configured = self._api_key_source() if self._api_key_source is not None else self._init_api_key
        return vendor, configured or os.environ.get(SEARCH_PROVIDERS[vendor].env_var, "")

    @classmethod
    def is_configured(cls, config_key: str | None, provider: str = DEFAULT_SEARCH_PROVIDER) -> bool:
        """Whether a search key resolves, from the config value or the
        environment.

        Asked of the tool rather than of the config because those are two
        sources and only the tool consults both: a deployment that exports the
        vendor's env var and configures nothing is configured, and a caller
        reading the config slot alone would say otherwise.
        """
        return bool(cls(api_key=config_key or None, provider=provider).api_key)

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        # Resolved once per call: the vendor and key this request carries are
        # what a refusal is recorded against, so a key replaced while the
        # request is in flight is tried by the next call rather than paused
        # unsent, and a vendor switched under the tool starts afresh.
        vendor, key = self._resolve()
        spec = SEARCH_PROVIDERS[vendor]
        if not key:
            # Reachable only if the key goes away after registration, since the
            # loops withhold this tool when there is none. Name the file actually
            # in force: hard-coding ~/.raven/config.json sent anyone running with
            # --config to edit a file the process never reads.
            from raven.config.loader import get_config_path

            return (
                f"Error: {spec.label} API key not configured. Set it in {get_config_path()} "
                f"under {spec.config_path} (or export {spec.env_var}), "
                "then restart the gateway."
            )

        if (refused := self._refusal.active(vendor, key)) is not None:
            error, detail = refusal_text(spec, refused, kind="search", sent=False)
            return f"Error: {error}. {detail}"
        try:
            n = min(max(count or self.max_results, 1), 10)
            logger.debug("WebSearch[{}]: {}", vendor, "proxy enabled" if self.proxy else "direct connection")
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await self._provider_request(client, query, n, vendor, key)
                r.raise_for_status()

            data = self._normalise_response(r.json(), vendor)
            results = data.get("organic", [])[:n]
            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query}\n"]
            if answer := data.get("answerBox"):
                snippet = answer.get("answer") or answer.get("snippet")
                if snippet:
                    lines.append(f"Answer: {snippet}\n")
            if knowledge := data.get("knowledgeGraph"):
                title = knowledge.get("title")
                description = knowledge.get("description")
                if title or description:
                    lines.append(f"Knowledge: {title or ''}")
                    if description:
                        lines.append(f"   {description}")
            for i, item in enumerate(results, 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('link', '')}")
                if desc := item.get("snippet"):
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except httpx.HTTPStatusError as e:
            # Status and vendor only, never the exception text: httpx puts the
            # full request URL in it, and SerpApi carries its key as a query
            # parameter, so the default message would hand the credential to
            # the model and the log.
            status = e.response.status_code
            logger.error("WebSearch error: {} answered HTTP {}", spec.label, status)
            if self._refusal.note(status, vendor, key):
                error, detail = refusal_text(spec, status, kind="search", sent=True)
                return f"Error: {error}. {detail}"
            return f"Error: {spec.label} answered HTTP {status}"
        except httpx.ProxyError as e:
            logger.error("WebSearch proxy error: {}", e)
            return f"Proxy error: {e}"
        except Exception as e:
            logger.error("WebSearch error: {}", e)
            return f"Error: {e}"

    async def probe(self, query: str = "raven ai agent") -> tuple[bool, str]:
        """One real search, to learn whether the key is live.

        Returns ``(ok, detail)``. No search vendor offers a free metadata
        endpoint the way chat providers offer ``/v1/models``, so this spends
        one query -- the smallest one the request shape allows. The detail
        carries the vendor and the status only, never exception text, for the
        reason ``execute`` gives: SerpApi puts the key in the request URL.
        """
        try:
            client = httpx.AsyncClient(proxy=self.proxy)
        except Exception as e:
            # httpx names the proxy URL in the message, and a proxy URL can
            # carry its own credentials, so only the class goes out.
            return False, f"the configured web proxy is not usable ({type(e).__name__})"
        vendor, key = self._resolve()
        spec = SEARCH_PROVIDERS[vendor]
        try:
            async with client:
                r = await self._provider_request(client, query, 1, vendor, key)
                r.raise_for_status()
            hits = len(self._normalise_response(r.json(), vendor).get("organic", []))
        except httpx.HTTPStatusError as e:
            return False, f"{spec.label} answered HTTP {e.response.status_code}"
        except httpx.HTTPError as e:
            return False, f"{spec.label} could not be reached ({type(e).__name__})"
        except ValueError as e:
            return False, f"{spec.label} answered without results: {e}"
        return True, f"{hits} result(s)"

    async def _provider_request(
        self, client: httpx.AsyncClient, query: str, n: int, vendor: str, key: str
    ) -> httpx.Response:
        """One search request, built the way ``vendor`` expects, carrying ``key``."""
        if vendor == "serper":
            return await client.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": n},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-API-KEY": key,
                },
                timeout=10.0,
            )
        if vendor == "serpapi":
            return await client.get(
                "https://serpapi.com/search",
                params={"engine": "google", "q": query, "num": n, "api_key": key},
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
        if vendor == "tavily":
            return await client.post(
                "https://api.tavily.com/search",
                json={"query": query, "max_results": n},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
                timeout=10.0,
            )
        if vendor == "exa":
            # Highlights, not ``text``: text is the whole page and the render
            # path shows whatever lands in the snippet slot. ``maxCharacters``
            # is the bound that holds on the live endpoint.
            return await client.post(
                "https://api.exa.ai/search",
                json={"query": query, "numResults": n, "contents": {"highlights": {"maxCharacters": 300}}},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "x-api-key": key,
                },
                timeout=10.0,
            )
        if vendor == "brave":
            return await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": n},
                headers={"Accept": "application/json", "X-Subscription-Token": key},
                timeout=10.0,
            )
        if vendor == "firecrawl":
            return await client.post(
                "https://api.firecrawl.dev/v1/search",
                json={"query": query, "limit": n},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
                timeout=10.0,
            )
        if vendor == "serply":
            return await client.get(
                "https://api.serply.io/v1/search",
                params={"q": query, "num": n},
                headers={"Accept": "application/json", "X-Api-Key": key},
                timeout=10.0,
            )
        return await client.post(
            "https://api.anysearch.com/v1/search",
            json={"query": query, "max_results": n},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            timeout=10.0,
        )

    def _normalise_response(self, data: Any, vendor: str) -> dict[str, Any]:
        """``vendor``'s payload in the Serper shape the render path reads.

        Only the keys the renderer reads are produced: ``organic`` rows with
        ``title`` / ``link`` / ``snippet``, plus ``answerBox`` and
        ``knowledgeGraph`` where the vendor has them.
        """
        if not isinstance(data, dict):
            return {}
        if vendor == "serper":
            return data
        if vendor == "serpapi":
            out: dict[str, Any] = {"organic": list(data.get("organic_results") or [])}
            if box := data.get("answer_box"):
                out["answerBox"] = box
            if kg := data.get("knowledge_graph"):
                out["knowledgeGraph"] = kg
            return out
        if vendor == "tavily":
            out = {"organic": _rows(data.get("results"), url="url", snippet="content")}
            if answer := data.get("answer"):
                out["answerBox"] = {"answer": answer}
            return out
        if vendor == "exa":
            organic = []
            for item in data.get("results") or []:
                if not isinstance(item, dict):
                    continue
                highlights = [h for h in (item.get("highlights") or []) if isinstance(h, str) and h]
                organic.append(
                    {
                        "title": str(item.get("title") or ""),
                        "link": str(item.get("url") or ""),
                        "snippet": " ".join(" ".join(h.split()) for h in highlights),
                    }
                )
            return {"organic": organic}
        if vendor == "brave":
            web = data.get("web") if isinstance(data.get("web"), dict) else {}
            return {"organic": _rows(web.get("results"), url="url", snippet="description")}
        if vendor == "firecrawl":
            # A refusal inside a 200 must read as an error, not as a dry search.
            if data.get("success") is False:
                raise ValueError(f"Firecrawl: {data.get('error') or 'search failed'}")
            return {"organic": _rows(data.get("data"), url="url", snippet="description")}
        if vendor == "serply":
            # Google SERP rows under ``results``; the snippet is ``description``.
            return {"organic": _rows(data.get("results"), url="link", snippet="description")}
        # AnySearch publishes the request shape but not the response: results may
        # sit at the top level or inside a ``{code, message, data}`` envelope, and
        # an item spells the URL ``url`` or ``link``, the text ``snippet`` or
        # ``content``.
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        raw = body.get("results") if isinstance(body, dict) else None
        organic = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            organic.append(
                {
                    "title": str(item.get("title") or ""),
                    "link": str(item.get("url") or item.get("link") or ""),
                    "snippet": item.get("snippet") or item.get("content") or "",
                }
            )
        return {"organic": organic}


# Below this a picture is already soft at half-page width on a 1280px canvas.
IMAGE_MIN_WIDTH = 640
# How many of one call's queries are in flight at once: a courtesy bound on one endpoint.
IMAGE_SEARCH_CONCURRENCY = 4
# How many queries one call takes; a twenty-page deck does not need more distinct pictures.
IMAGE_MAX_QUERIES = 12

#: The ``web_search`` vendors that also have an image surface. Exa and AnySearch
#: search pages only; a deployment on one of those searches pictures through the
#: default vendor's key when it holds one. Tavily answers without dimensions, so
#: its hits are offered as "size unknown" rather than dropped.
IMAGE_SEARCH_VENDORS: tuple[str, ...] = ("serper", "serpapi", "brave", "tavily", "firecrawl")


def image_search_vendor(selected: str, key_for: "Callable[[str], str | None] | None" = None) -> str:
    """The vendor ``image_search`` speaks to, given the one ``web_search`` selected.

    The selected vendor when it has an image surface -- and, when ``key_for`` is
    given, when a key resolves for it; Serper otherwise. A host running Tavily
    on Tavily's key searches pictures through Tavily; a host running Exa, or
    one whose Tavily has no key, searches them through the Serper key it holds.
    """
    if selected in IMAGE_SEARCH_VENDORS and (key_for is None or key_for(selected)):
        return selected
    return DEFAULT_SEARCH_PROVIDER


@dataclass(frozen=True)
class ImageHit:
    """One picture as every vendor is read into: where it is, how big, and whose."""

    title: str
    image_url: str
    width: int | None
    height: int | None
    source: str
    page: str


class ImageSearchTool(Tool):
    """Search pictures through the selected vendor's image surface.

    A built-in beside ``web_search`` rather than a recipe in a skill: the Design lane
    was told to call Serper's image endpoint from ``exec`` with a key "already
    configured" in a file whose path nothing named, and a live run probed for the
    key, found none, and generated every picture instead. A tool is registered,
    gated on its vendor's key the way ``web_search`` is, and withheld while that key
    is absent. The vendor follows ``tools.web.search.provider`` where that vendor has
    an image surface (see :data:`IMAGE_SEARCH_VENDORS`) and is Serper otherwise.
    """

    name = "image_search"
    description = (
        "Search the web for pictures: each result carries the direct image URL, its pixel size where the "
        "vendor reports one, and the page it came from; anything known to be too small to hold up on a "
        "screen is dropped rather than offered. For a real logo, product shot, photograph or published "
        "chart; image_generate is for pictures that do not exist yet. Pass every picture the task needs "
        "as queries=[...] in one call -- they run together and come back grouped by query. Download what "
        "you pick and look at it before placing it: a hit the right size can still be a thumbnail sheet "
        "or somebody else's slide."
    )
    parameters = {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": IMAGE_MAX_QUERIES,
                "description": "every picture the task needs, one query each; they are searched together",
            },
            "query": {"type": "string", "description": "one query, for a single search"},
            "count": {"type": "integer", "description": "Results per query (1-10)", "minimum": 1, "maximum": 10},
            "min_width": {
                "type": "integer",
                "minimum": 1,
                "description": f"drop anything known to be narrower than this in pixels (default {IMAGE_MIN_WIDTH})",
            },
        },
    }

    def __init__(
        self,
        api_key: "str | Callable[[], str] | None" = None,
        max_results: int = 5,
        proxy: str | None = None,
        provider: "str | Callable[[], str]" = DEFAULT_SEARCH_PROVIDER,
    ):
        _check_provider(provider, IMAGE_SEARCH_VENDORS, "image_search")
        # A callable is the live form (a reader over the config file), as for web_search.
        self._api_key_source: "Callable[[], str] | None" = api_key if callable(api_key) else None
        self._init_api_key: str | None = None if callable(api_key) else api_key
        self._refusal = _VendorRefusal(SEARCH_REFUSAL_STATUSES)
        self.max_results = max_results
        self.proxy = proxy
        self._provider_source: "Callable[[], str] | None" = provider if callable(provider) else None
        self._init_provider: str = DEFAULT_SEARCH_PROVIDER if callable(provider) else provider

    @property
    def provider(self) -> str:
        return _resolve_provider(self._provider_source, self._init_provider, IMAGE_SEARCH_VENDORS)

    @property
    def spec(self) -> SearchProviderSpec:
        return SEARCH_PROVIDERS[self.provider]

    @property
    def api_key(self) -> str:
        """The vendor's key, from the live reader or the boot value, else its environment variable."""
        configured = self._api_key_source() if self._api_key_source is not None else self._init_api_key
        return configured or os.environ.get(self.spec.env_var, "")

    def _resolve(self) -> tuple[str, str]:
        """The (vendor, key) pair one call runs on, each source read once; see ``WebSearchTool._resolve``."""
        vendor = self.provider
        configured = self._api_key_source() if self._api_key_source is not None else self._init_api_key
        return vendor, configured or os.environ.get(SEARCH_PROVIDERS[vendor].env_var, "")

    @classmethod
    def is_configured(cls, config_key: str | None, provider: str = DEFAULT_SEARCH_PROVIDER) -> bool:
        """Whether a key resolves for the vendor, from the config value or the environment."""
        return bool(cls(api_key=config_key or None, provider=provider).api_key)

    async def execute(
        self,
        queries: list[str] | None = None,
        query: str | None = None,
        count: int | None = None,
        min_width: int | None = None,
        **kwargs: Any,
    ) -> str:
        vendor, key = self._resolve()
        spec = SEARCH_PROVIDERS[vendor]
        if not key:
            from raven.config.loader import get_config_path

            return (
                f"Error: {spec.label} API key not configured. Set it in {get_config_path()} under "
                f"{spec.config_path} (or export {spec.env_var}), then restart the gateway."
            )
        wanted = [said.strip() for said in (queries or ([query] if query else [])) if said and said.strip()]
        if not wanted:
            return "Error: pass queries=[...] with the pictures the task needs, or query='...' for one."
        if len(wanted) > IMAGE_MAX_QUERIES:
            return f"Error: {len(wanted)} queries in one call; {IMAGE_MAX_QUERIES} is the most. Split them."
        per_query = min(max(count or self.max_results, 1), 10)
        floor = max(min_width or IMAGE_MIN_WIDTH, 1)
        gate = asyncio.Semaphore(IMAGE_SEARCH_CONCURRENCY)

        async def one(said: str) -> str:
            async with gate:
                if (refused := self._refusal.active(vendor, key)) is not None:
                    error, detail = refusal_text(spec, refused, kind="search", sent=False)
                    return f"Image results for: {said}\n\n{error}. {detail}"
                try:
                    return await self._search_images(said, per_query, floor, vendor, key)
                except httpx.HTTPStatusError as exc:
                    # Status only: httpx puts the request in the message, and SerpApi
                    # carries its key as a query parameter.
                    status = exc.response.status_code
                    if self._refusal.note(status, vendor, key):
                        error, detail = refusal_text(spec, status, kind="search", sent=True)
                        return f"Image results for: {said}\n\n{error}. {detail}"
                    return f"Image results for: {said}\n\n{spec.label} answered HTTP {status}."
                except Exception as exc:  # noqa: BLE001 -- one query's failure is not the batch's
                    return f"Image results for: {said}\n\nThis search failed ({type(exc).__name__})."

        found = await asyncio.gather(*(one(said) for said in wanted))
        return found[0] if len(found) == 1 else ("\n\n" + "-" * 60 + "\n\n").join(found)

    async def _search_images(self, query: str, count: int, min_width: int, vendor: str, key: str) -> str:
        """The vendor's image surface, filtered to what a screen can use.

        Dimensions and source page travel with every hit: the caller has two
        judgements to make -- whether it holds up on screen, and whether its
        origin can be cited -- and needs both. A vendor that reports no size
        leaves the first judgement to the caller, and the line says so.
        """
        async with httpx.AsyncClient(proxy=self.proxy) as client:
            response = await self._provider_request(client, query, max(count, 10), vendor, key)
            response.raise_for_status()
        hits = self.normalise_hits(response.json(), vendor)
        usable = [
            hit
            for hit in hits
            if hit.image_url
            and (hit.width is None or hit.width >= min_width)
            # 16:9 is the shape of a screen; far taller than wide cannot fill a region uncropped.
            and (hit.height is None or hit.height >= int(min_width * 9 / 16))
        ]
        if not usable:
            return (
                f"No images at least {min_width}px wide for: {query}\n"
                "Try a more specific query, or lower min_width if a smaller image is genuinely enough."
            )
        lines = [f"Image results for: {query}\n"]
        for index, hit in enumerate(usable[:count], 1):
            lines.append(f"{index}. {hit.title}")
            lines.append(f"   {hit.image_url}")
            size = f"{hit.width}x{hit.height}px" if hit.width and hit.height else "size unknown -- check before use"
            lines.append(f"   {size} - {hit.source or 'unknown source'}")
            if hit.page:
                lines.append(f"   from: {hit.page}")
        if len(usable) > count:
            lines.append(f"\n[{len(usable)} usable results, {count} shown.]")
        lines.append(
            "\nDownload one before use and look at it: it has to depict what was asked, from a source you can cite."
        )
        return "\n".join(lines)

    async def _provider_request(
        self, client: httpx.AsyncClient, query: str, n: int, vendor: str, key: str
    ) -> httpx.Response:
        """One image search, built the way ``vendor``'s image surface expects, carrying ``key``."""
        auth_json = {"Accept": "application/json", "Content-Type": "application/json"}
        if vendor == "serper":
            return await client.post(
                "https://google.serper.dev/images",
                json={"q": query, "num": n},
                headers={**auth_json, "X-API-KEY": key},
                timeout=15.0,
            )
        if vendor == "serpapi":
            return await client.get(
                "https://serpapi.com/search.json",
                params={"engine": "google_images", "q": query, "api_key": key},
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
        if vendor == "brave":
            return await client.get(
                "https://api.search.brave.com/res/v1/images/search",
                params={"q": query, "count": n},
                headers={"Accept": "application/json", "X-Subscription-Token": key},
                timeout=15.0,
            )
        if vendor == "tavily":
            return await client.post(
                "https://api.tavily.com/search",
                json={"query": query, "max_results": n, "include_images": True, "include_image_descriptions": True},
                headers={**auth_json, "Authorization": f"Bearer {key}"},
                timeout=15.0,
            )
        return await client.post(
            "https://api.firecrawl.dev/v2/search",
            json={"query": query, "limit": n, "sources": [{"type": "images"}]},
            headers={**auth_json, "Authorization": f"Bearer {key}"},
            timeout=15.0,
        )

    def normalise_hits(self, data: Any, vendor: str | None = None) -> list[ImageHit]:
        """A vendor payload as the one list the render path reads; ``vendor`` defaults to the tool's."""
        if not isinstance(data, dict):
            return []
        vendor = self.provider if vendor is None else vendor

        def number(value: Any) -> int | None:
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def domain(url: str) -> str:
            return urlparse(url).netloc if url else ""

        hits: list[ImageHit] = []
        if vendor == "serper":
            for item in data.get("images") or []:
                hits.append(
                    ImageHit(
                        title=str(item.get("title") or ""),
                        image_url=str(item.get("imageUrl") or ""),
                        width=number(item.get("imageWidth")),
                        height=number(item.get("imageHeight")),
                        source=str(item.get("domain") or item.get("source") or ""),
                        page=str(item.get("link") or ""),
                    )
                )
        elif vendor == "serpapi":
            for item in data.get("images_results") or []:
                hits.append(
                    ImageHit(
                        title=str(item.get("title") or ""),
                        image_url=str(item.get("original") or ""),
                        width=number(item.get("original_width")),
                        height=number(item.get("original_height")),
                        source=str(item.get("source") or domain(str(item.get("link") or ""))),
                        page=str(item.get("link") or ""),
                    )
                )
        elif vendor == "brave":
            for item in data.get("results") or []:
                properties = item.get("properties") or {}
                page = str(item.get("url") or "")
                hits.append(
                    ImageHit(
                        title=str(item.get("title") or ""),
                        image_url=str(properties.get("url") or ""),
                        width=number(properties.get("width")),
                        height=number(properties.get("height")),
                        source=str(item.get("source") or domain(page)),
                        page=page,
                    )
                )
        elif vendor == "tavily":
            # Tavily names the picture and describes it; it reports no size and no page.
            for item in data.get("images") or []:
                if isinstance(item, str):
                    item = {"url": item}
                url = str(item.get("url") or "")
                hits.append(
                    ImageHit(
                        title=str(item.get("description") or ""),
                        image_url=url,
                        width=None,
                        height=None,
                        source=domain(url),
                        page="",
                    )
                )
        else:
            for item in (data.get("data") or {}).get("images") or []:
                page = str(item.get("url") or "")
                hits.append(
                    ImageHit(
                        title=str(item.get("title") or ""),
                        image_url=str(item.get("imageUrl") or ""),
                        width=number(item.get("imageWidth")),
                        height=number(item.get("imageHeight")),
                        source=domain(page),
                        page=page,
                    )
                )
        return hits


def _rows(items: Any, *, url: str, snippet: str) -> list[dict[str, Any]]:
    return [
        {
            "title": str(item.get("title") or ""),
            "link": str(item.get(url) or ""),
            "snippet": item.get(snippet) or "",
        }
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict)
    ]


class WebFetchTool(Tool):
    """Fetch and extract readable content from a URL through the selected vendor."""

    name = "web_fetch"
    description = "Fetch URL and extract readable content."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100},
        },
        "required": ["url"],
    }

    def __init__(
        self,
        api_key: "str | Callable[[], str] | None" = None,
        max_chars: int = 50000,
        proxy: str | None = None,
        provider: "str | Callable[[], str]" = DEFAULT_FETCH_PROVIDER,
    ):
        _check_provider(provider, FETCH_PROVIDERS, "web_fetch")
        self._provider_source: "Callable[[], str] | None" = provider if callable(provider) else None
        # ``effective_provider`` used to run once, at registration. It runs per
        # call now because the vendor it judges can move: a keyed backend
        # selected without a key is still replaced by Jina, but a key added
        # later stops the substitution instead of outliving it.
        self._init_provider: str = DEFAULT_FETCH_PROVIDER if callable(provider) else provider
        self._api_key_source: "Callable[[], str] | None" = api_key if callable(api_key) else None
        self._init_api_key: str | None = None if callable(api_key) else api_key
        self.max_chars = max_chars
        self.proxy = proxy
        self._substitution_said = False
        self._refusal = _VendorRefusal(FETCH_REFUSAL_STATUSES)

    @property
    def provider(self) -> str:
        """The backend this call runs on, after the Jina substitution.

        Resolved per call rather than at registration: the vendor and the key
        behind it both move now, and a substitution decided once outlived the
        key that would have stopped it.
        """
        return self._resolve()[0]

    @property
    def spec(self) -> FetchProviderSpec:
        return FETCH_PROVIDERS[self.provider]

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._resolve()[1]

    def _resolve(self) -> tuple[str, str]:
        """The (vendor, key) pair one call runs on, each source read once.

        The selected vendor, the key behind it and the Jina substitution are
        one decision: a key read again for the substitution could differ from
        the key the request carries, and a vendor read again for the envelope
        could name a backend the request never reached.
        """
        selected = _resolve_provider(self._provider_source, self._init_provider, FETCH_PROVIDERS)
        configured = self._api_key_source() if self._api_key_source is not None else self._init_api_key
        vendor = self.effective_provider(selected, configured, warn=not self._substitution_said)
        if vendor != selected:
            self._substitution_said = True
        return vendor, configured or os.environ.get(FETCH_PROVIDERS[vendor].env_var, "")

    @classmethod
    def effective_provider(cls, provider: str, api_key: str | None, *, warn: bool = True) -> str:
        """The backend a call runs on: the selected one, or Jina when it cannot.

        ``web_fetch`` is always offered because Jina reads pages without a key.
        A keyed backend selected without a key would be offered and fail on
        every call, so it is replaced here, out loud, rather than run.

        ``warn`` is off for the repeat: this is asked per call now, and a line
        per call for as long as the config stays keyless is noise.
        """
        if provider not in FETCH_PROVIDERS:
            raise ValueError(f"unknown web_fetch provider {provider!r}; one of {sorted(FETCH_PROVIDERS)}")
        spec = FETCH_PROVIDERS[provider]
        if not spec.needs_key or api_key or os.environ.get(spec.env_var):
            return provider
        if not warn:
            return DEFAULT_FETCH_PROVIDER
        logger.warning(
            "web_fetch: {} selected but no key resolves ({} / {}); reading pages through Jina instead",
            spec.label,
            spec.config_path,
            spec.env_var,
        )
        return DEFAULT_FETCH_PROVIDER

    async def execute(self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:  # noqa: N803  (LLM tool schema uses camelCase)
        max_chars = maxChars or self.max_chars
        is_valid, error_msg = validate_url_target(url)
        if not is_valid:
            # The same rule as the handlers below: most of these reasons name the
            # hostname, and a reader whose every target is refused is one cause.
            return json.dumps({"error": "URL validation failed", "detail": error_msg, "url": url}, ensure_ascii=False)
        vendor, key = self._resolve()
        spec = FETCH_PROVIDERS[vendor]
        if (refused := self._refusal.active(vendor, key)) is not None:
            error, detail = refusal_text(spec, refused, kind="fetch", sent=False)
            return json.dumps({"error": error, "detail": detail, "paused": True, "url": url}, ensure_ascii=False)

        try:
            logger.debug("WebFetch[{}]: {}", vendor, "proxy enabled" if self.proxy else "direct connection")
            text, status, extras = await self._provider_fetch(url, vendor, key)

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            return json.dumps(
                {
                    "url": url,
                    "finalUrl": url,
                    "status": status,
                    "extractor": spec.extractor,
                    "extractMode": extractMode,
                    "truncated": truncated,
                    "length": len(text),
                    **extras,
                    "text": text,
                },
                ensure_ascii=False,
            )
        except httpx.HTTPStatusError as e:
            # Same rule as the search tool: the vendor and the status, not a
            # message that repeats the request URL.
            status = e.response.status_code
            logger.error("WebFetch error for {}: {} answered HTTP {}", url, spec.label, status)
            if self._refusal.note(status, vendor, key):
                error, detail = refusal_text(spec, status, kind="fetch", sent=True)
                return json.dumps({"error": error, "detail": detail, "paused": True, "url": url}, ensure_ascii=False)
            return json.dumps({"error": f"{spec.label} answered HTTP {status}", "url": url}, ensure_ascii=False)
        except httpx.ProxyError as e:
            # The same rule as the status half above, and it reaches past the log line:
            # ``failure_class`` keys the loop's streak on this envelope's ``error``
            # alone, so a host interpolated here splits one repeated cause into a class
            # per host and the stop-repeating nudge never fires. The exception's own
            # text moves to ``detail``, which keeps it in front of the model and inside
            # ``is_hard_tool_failure``'s transient-marker scan.
            logger.error("WebFetch proxy error for {}: {}", url, e)
            return json.dumps({"error": "Proxy error", "detail": str(e), "url": url}, ensure_ascii=False)
        except _ProviderPageError as e:
            # Not folded into the transport case below: this type exists to say the
            # vendor answered without a page, and its message is composed here from a
            # fixed set of phrases rather than taken from an exception, so it already is
            # the vocabulary the streak wants. Naming it by type would make every
            # vendor's refusal one class.
            logger.error("WebFetch error for {}: {}", url, e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)
        except Exception as e:
            # The type, not the text: an SSL or connection failure spells the host in
            # its message, and two hosts behind one broken reader are one cause.
            logger.error("WebFetch error for {}: {}", url, e)
            return json.dumps({"error": type(e).__name__, "detail": str(e), "url": url}, ensure_ascii=False)

    async def _provider_fetch(self, url: str, vendor: str, key: str) -> tuple[str, int, dict[str, Any]]:
        """One page, read the way ``vendor`` serves it, carrying ``key``.

        Returns the page text, the status to report, and any extra envelope
        fields the backend can fill in. Raises when the answer was not a page.
        """
        if vendor == "jina":
            headers = {"Accept": "text/plain"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                r = await client.get(f"https://r.jina.ai/{url}", headers=headers)
                r.raise_for_status()
            return r.text, r.status_code, {}

        json_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if vendor == "tavily":
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                r = await client.post(
                    "https://api.tavily.com/extract",
                    json={"urls": [url]},
                    headers={**json_headers, "Authorization": f"Bearer {key}"},
                )
                r.raise_for_status()
            data = r.json()
            results = data.get("results") if isinstance(data, dict) else None
            hit = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
            if hit is None:
                failed = data.get("failed_results") if isinstance(data, dict) else None
                reason = (
                    failed[0].get("error")
                    if isinstance(failed, list) and failed and isinstance(failed[0], dict)
                    else None
                )
                raise _ProviderPageError(f"Tavily: {reason or 'extract failed'}")
            text = str(hit.get("raw_content") or "")
            if not text:
                raise _ProviderPageError("Tavily returned no page content")
            return text, r.status_code, {}

        if vendor == "exa":
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                r = await client.post(
                    "https://api.exa.ai/contents",
                    json={"urls": [url], "text": True},
                    headers={**json_headers, "x-api-key": key},
                )
                r.raise_for_status()
            data = r.json()
            results = data.get("results") if isinstance(data, dict) else None
            hit = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
            text = str(hit.get("text") or "") if hit else ""
            if not text:
                raise _ProviderPageError("Exa returned no page content")
            return text, r.status_code, {"title": str(hit["title"])} if hit.get("title") else {}

        if vendor == "firecrawl":
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                r = await client.post(
                    "https://api.firecrawl.dev/v1/scrape",
                    json={"url": url, "formats": ["markdown"]},
                    headers={**json_headers, "Authorization": f"Bearer {key}"},
                )
                r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict) or not data.get("success"):
                message = (data.get("error") or "scrape failed") if isinstance(data, dict) else "malformed response"
                raise _ProviderPageError(f"Firecrawl: {message}")
            body = data.get("data") if isinstance(data.get("data"), dict) else {}
            text = str(body.get("markdown") or "")
            if not text:
                raise _ProviderPageError("Firecrawl returned no page content")
            metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            return text, r.status_code, {"title": str(metadata["title"])} if metadata.get("title") else {}

        # AnySearch: a 200 whose envelope reports the failure is the other shape
        # a refusal takes, so ``code`` is read as well as the HTTP status.
        headers = dict(json_headers)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
            r = await client.post("https://api.anysearch.com/v1/extract", json={"url": url}, headers=headers)
            r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise _ProviderPageError("AnySearch returned a non-object body")
        if data.get("code") not in (0, None):
            raise _ProviderPageError(f"AnySearch: {data.get('message') or 'extract failed'}")
        body = data.get("data") if isinstance(data.get("data"), dict) else {}
        text = str(body.get("content") or "")
        if not text:
            raise _ProviderPageError("AnySearch returned no page content")
        return text, r.status_code, {"title": str(body["title"])} if body.get("title") else {}
