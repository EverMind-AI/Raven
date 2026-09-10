"""Web tools: web_search and web_fetch."""

import json
import os
from typing import Any
from urllib.parse import quote_plus

import httpx
from loguru import logger

from raven.agent.tools.base import Tool
from raven.security.network import validate_url_target


class WebSearchTool(Tool):
    """Search the web using Serper or Serply."""

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

    #: Provider name -> (environment variable standing in for its key, where a key is obtained).
    PROVIDERS = {
        "serper": ("SERPER_API_KEY", "https://serper.dev"),
        "serply": ("SERPLY_API_KEY", "https://serply.io"),
    }

    def __init__(
        self, api_key: str | None = None, max_results: int = 5, proxy: str | None = None, provider: str = "serper"
    ):
        self._init_api_key = api_key
        self.max_results = max_results
        self.proxy = proxy
        self.provider = provider

    @classmethod
    def env_var(cls, provider: str = "serper") -> str:
        """The environment variable accepted instead of ``provider``'s configured key."""
        return cls.PROVIDERS[provider][0]

    @classmethod
    def key_source(cls, provider: str = "serper") -> str:
        """Where a deployer obtains a key for ``provider``."""
        return cls.PROVIDERS[provider][1]

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get(self.env_var(self.provider), "")

    @classmethod
    def is_configured(cls, config_key: str | None, provider: str = "serper") -> bool:
        """Whether a search key resolves, from the config value or the
        environment.

        Asked of the tool rather than of the config because those are two
        sources and only the tool consults both: a deployment that exports
        ``SERPER_API_KEY`` and configures nothing is configured, and a caller
        reading ``tools.web.search.apiKey`` alone would say otherwise. Only the
        chosen provider's variable counts.
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
                f"Error: {self.provider.capitalize()} API key not configured. Set it in {get_config_path()} "
                f"under tools.web.search.apiKey (or export {self.env_var(self.provider)}), "
                "then restart the gateway."
            )

        try:
            n = min(max(count or self.max_results, 1), 10)
            logger.debug("WebSearch: {}", "proxy enabled" if self.proxy else "direct connection")
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                if self.provider == "serply":
                    data, results = await self._search_serply(client, query, n)
                else:
                    data, results = await self._search_serper(client, query, n)
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
        except httpx.ProxyError as e:
            logger.error("WebSearch proxy error: {}", e)
            return f"Proxy error: {e}"
        except Exception as e:
            logger.error("WebSearch error: {}", e)
            return f"Error: {e}"

    async def _search_serper(self, client: httpx.AsyncClient, query: str, n: int) -> tuple[dict, list[dict]]:
        r = await client.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": n},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-API-KEY": self.api_key,
            },
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        return data, data.get("organic", [])[:n]

    async def _search_serply(self, client: httpx.AsyncClient, query: str, n: int) -> tuple[dict, list[dict]]:
        # Serply has no answer box or knowledge graph, so only the organic
        # results come back, in the shape the Serper branch produces.
        r = await client.get(
            f"https://api.serply.io/v1/search/q={quote_plus(query)}&num={n}",
            headers={"Accept": "application/json", "X-Api-Key": self.api_key},
            timeout=10.0,
        )
        r.raise_for_status()
        results = [
            {"title": item.get("title", ""), "link": item.get("link", ""), "snippet": item.get("description", "")}
            for item in r.json().get("results", [])[:n]
        ]
        return {}, results


class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Jina Reader."""

    name = "web_fetch"
    description = "Fetch URL and extract readable content via Jina Reader."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100},
        },
        "required": ["url"],
    }

    def __init__(self, api_key: str | None = None, max_chars: int = 50000, proxy: str | None = None):
        self._init_api_key = api_key
        self.max_chars = max_chars
        self.proxy = proxy

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get("JINA_API_KEY", "")

    async def execute(self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:  # noqa: N803  (LLM tool schema uses camelCase)
        max_chars = maxChars or self.max_chars
        is_valid, error_msg = validate_url_target(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        try:
            logger.debug("WebFetch: {}", "proxy enabled" if self.proxy else "direct connection")
            headers = {"Accept": "text/plain"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                r = await client.get(f"https://r.jina.ai/{url}", headers=headers)
                r.raise_for_status()

            text = r.text

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            return json.dumps(
                {
                    "url": url,
                    "finalUrl": url,
                    "status": r.status_code,
                    "extractor": "jina-reader",
                    "extractMode": extractMode,
                    "truncated": truncated,
                    "length": len(text),
                    "text": text,
                },
                ensure_ascii=False,
            )
        except httpx.ProxyError as e:
            logger.error("WebFetch proxy error for {}: {}", url, e)
            return json.dumps({"error": f"Proxy error: {e}", "url": url}, ensure_ascii=False)
        except Exception as e:
            logger.error("WebFetch error for {}: {}", url, e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)
