"""Web tools: web_search and web_fetch, each behind a selectable vendor.

One tool per capability, several backends behind each. What differs per vendor
is the request it expects and the payload it answers with; everything the model
sees -- the rendered result list, the fetch envelope -- is one code path, so
each provider is normalised to the Serper (search) or Jina (fetch) shape before
anything reads it. The Serper and Jina branches are byte-identical to what
these tools always sent; do not tidy them into the others.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


class _ProviderPageError(RuntimeError):
    """A fetch backend answered, but not with a page."""


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
        provider: str = DEFAULT_SEARCH_PROVIDER,
    ):
        if provider not in SEARCH_PROVIDERS:
            raise ValueError(f"unknown web_search provider {provider!r}; one of {sorted(SEARCH_PROVIDERS)}")
        # A callable is the live form (a reader over the config file), so a key
        # added there serves the next call; a plain string stays a snapshot.
        self._api_key_source = api_key if callable(api_key) else None
        self._init_api_key = None if callable(api_key) else api_key
        self.max_results = max_results
        self.proxy = proxy
        self.provider = provider

    @property
    def spec(self) -> SearchProviderSpec:
        return SEARCH_PROVIDERS[self.provider]

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        configured = self._api_key_source() if self._api_key_source is not None else self._init_api_key
        return configured or os.environ.get(self.spec.env_var, "")

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
        if not self.api_key:
            # Reachable only if the key goes away after registration, since the
            # loops withhold this tool when there is none. Name the file actually
            # in force: hard-coding ~/.raven/config.json sent anyone running with
            # --config to edit a file the process never reads.
            from raven.config.loader import get_config_path

            return (
                f"Error: {self.spec.label} API key not configured. Set it in {get_config_path()} "
                f"under {self.spec.config_path} (or export {self.spec.env_var}), "
                "then restart the gateway."
            )

        try:
            n = min(max(count or self.max_results, 1), 10)
            logger.debug("WebSearch[{}]: {}", self.provider, "proxy enabled" if self.proxy else "direct connection")
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await self._provider_request(client, query, n)
                r.raise_for_status()

            data = self._normalise_response(r.json())
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
            logger.error("WebSearch error: {} answered HTTP {}", self.spec.label, status)
            return f"Error: {self.spec.label} answered HTTP {status}"
        except httpx.ProxyError as e:
            logger.error("WebSearch proxy error: {}", e)
            return f"Proxy error: {e}"
        except Exception as e:
            logger.error("WebSearch error: {}", e)
            return f"Error: {e}"

    async def _provider_request(self, client: httpx.AsyncClient, query: str, n: int) -> httpx.Response:
        """One search request, built the way the selected provider expects."""
        if self.provider == "serper":
            return await client.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": n},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-API-KEY": self.api_key,
                },
                timeout=10.0,
            )
        if self.provider == "serpapi":
            return await client.get(
                "https://serpapi.com/search",
                params={"engine": "google", "q": query, "num": n, "api_key": self.api_key},
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
        if self.provider == "tavily":
            return await client.post(
                "https://api.tavily.com/search",
                json={"query": query, "max_results": n},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout=10.0,
            )
        if self.provider == "exa":
            # Highlights, not ``text``: text is the whole page and the render
            # path shows whatever lands in the snippet slot. ``maxCharacters``
            # is the bound that holds on the live endpoint.
            return await client.post(
                "https://api.exa.ai/search",
                json={"query": query, "numResults": n, "contents": {"highlights": {"maxCharacters": 300}}},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                },
                timeout=10.0,
            )
        if self.provider == "brave":
            return await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": n},
                headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                timeout=10.0,
            )
        if self.provider == "firecrawl":
            return await client.post(
                "https://api.firecrawl.dev/v1/search",
                json={"query": query, "limit": n},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout=10.0,
            )
        return await client.post(
            "https://api.anysearch.com/v1/search",
            json={"query": query, "max_results": n},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=10.0,
        )

    def _normalise_response(self, data: Any) -> dict[str, Any]:
        """A provider payload in the Serper shape the render path reads.

        Only the keys the renderer reads are produced: ``organic`` rows with
        ``title`` / ``link`` / ``snippet``, plus ``answerBox`` and
        ``knowledgeGraph`` where the vendor has them.
        """
        if not isinstance(data, dict):
            return {}
        if self.provider == "serper":
            return data
        if self.provider == "serpapi":
            out: dict[str, Any] = {"organic": list(data.get("organic_results") or [])}
            if box := data.get("answer_box"):
                out["answerBox"] = box
            if kg := data.get("knowledge_graph"):
                out["knowledgeGraph"] = kg
            return out
        if self.provider == "tavily":
            out = {"organic": _rows(data.get("results"), url="url", snippet="content")}
            if answer := data.get("answer"):
                out["answerBox"] = {"answer": answer}
            return out
        if self.provider == "exa":
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
        if self.provider == "brave":
            web = data.get("web") if isinstance(data.get("web"), dict) else {}
            return {"organic": _rows(web.get("results"), url="url", snippet="description")}
        if self.provider == "firecrawl":
            # A refusal inside a 200 must read as an error, not as a dry search.
            if data.get("success") is False:
                raise ValueError(f"Firecrawl: {data.get('error') or 'search failed'}")
            return {"organic": _rows(data.get("data"), url="url", snippet="description")}
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
        api_key: str | None = None,
        max_chars: int = 50000,
        proxy: str | None = None,
        provider: str = DEFAULT_FETCH_PROVIDER,
    ):
        if provider not in FETCH_PROVIDERS:
            raise ValueError(f"unknown web_fetch provider {provider!r}; one of {sorted(FETCH_PROVIDERS)}")
        self._init_api_key = api_key
        self.max_chars = max_chars
        self.proxy = proxy
        self.provider = provider

    @property
    def spec(self) -> FetchProviderSpec:
        return FETCH_PROVIDERS[self.provider]

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get(self.spec.env_var, "")

    @classmethod
    def effective_provider(cls, provider: str, api_key: str | None) -> str:
        """The backend to register: the selected one, or Jina when it cannot run.

        ``web_fetch`` is always offered because Jina reads pages without a key.
        A keyed backend selected without a key would be offered and fail on
        every call, so it is replaced here, out loud, rather than registered.
        """
        if provider not in FETCH_PROVIDERS:
            raise ValueError(f"unknown web_fetch provider {provider!r}; one of {sorted(FETCH_PROVIDERS)}")
        spec = FETCH_PROVIDERS[provider]
        if not spec.needs_key or api_key or os.environ.get(spec.env_var):
            return provider
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
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        try:
            logger.debug("WebFetch[{}]: {}", self.provider, "proxy enabled" if self.proxy else "direct connection")
            text, status, extras = await self._provider_fetch(url)

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            return json.dumps(
                {
                    "url": url,
                    "finalUrl": url,
                    "status": status,
                    "extractor": self.spec.extractor,
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
            logger.error("WebFetch error for {}: {} answered HTTP {}", url, self.spec.label, status)
            return json.dumps({"error": f"{self.spec.label} answered HTTP {status}", "url": url}, ensure_ascii=False)
        except httpx.ProxyError as e:
            logger.error("WebFetch proxy error for {}: {}", url, e)
            return json.dumps({"error": f"Proxy error: {e}", "url": url}, ensure_ascii=False)
        except Exception as e:
            logger.error("WebFetch error for {}: {}", url, e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

    async def _provider_fetch(self, url: str) -> tuple[str, int, dict[str, Any]]:
        """One page, read the way the selected backend serves it.

        Returns the page text, the status to report, and any extra envelope
        fields the backend can fill in. Raises when the answer was not a page.
        """
        if self.provider == "jina":
            headers = {"Accept": "text/plain"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                r = await client.get(f"https://r.jina.ai/{url}", headers=headers)
                r.raise_for_status()
            return r.text, r.status_code, {}

        json_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.provider == "tavily":
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                r = await client.post(
                    "https://api.tavily.com/extract",
                    json={"urls": [url]},
                    headers={**json_headers, "Authorization": f"Bearer {self.api_key}"},
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

        if self.provider == "exa":
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                r = await client.post(
                    "https://api.exa.ai/contents",
                    json={"urls": [url], "text": True},
                    headers={**json_headers, "x-api-key": self.api_key},
                )
                r.raise_for_status()
            data = r.json()
            results = data.get("results") if isinstance(data, dict) else None
            hit = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
            text = str(hit.get("text") or "") if hit else ""
            if not text:
                raise _ProviderPageError("Exa returned no page content")
            return text, r.status_code, {"title": str(hit["title"])} if hit.get("title") else {}

        if self.provider == "firecrawl":
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                r = await client.post(
                    "https://api.firecrawl.dev/v1/scrape",
                    json={"url": url, "formats": ["markdown"]},
                    headers={**json_headers, "Authorization": f"Bearer {self.api_key}"},
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
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
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
