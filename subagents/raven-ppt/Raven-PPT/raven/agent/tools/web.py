"""Web tools: web_search and web_fetch."""

import json
import os
from typing import Any

import httpx
from loguru import logger

from raven.agent.tools.base import Tool
from raven.security.network import validate_url_target

# Below this a picture is already soft at half-page width on a 1280px canvas.
_MIN_IMAGE_WIDTH = 640


class WebSearchTool(Tool):
    """Search the web using Serper."""

    name = "web_search"
    description = (
        "Search the web. Returns titles, URLs, and snippets. "
        'Set kind="images" to search pictures instead: results carry the direct image URL, its pixel '
        "dimensions and the page it came from, and anything too small to hold up on a screen is dropped "
        "rather than offered."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10},
            "kind": {
                "type": "string",
                "enum": ["web", "images"],
                "description": 'Search surface: "web" for pages (default), "images" for pictures',
            },
            "min_width": {
                "type": "integer",
                "minimum": 1,
                "description": "images only: drop anything narrower than this in pixels (default 640)",
            },
        },
        "required": ["query"],
    }

    def __init__(self, api_key: str | None = None, max_results: int = 5, proxy: str | None = None):
        self._init_api_key = api_key
        self.max_results = max_results
        self.proxy = proxy

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get("SERPER_API_KEY", "")

    async def _search_images(self, query: str, count: int, min_width: int) -> str:
        """Serper's image surface, filtered to what a slide can actually use.

        A picture on a slide needs pixels: an image narrower than roughly 640px is
        already soft at half-page width on a 1280px canvas, so those are dropped
        here rather than offered and rejected later once someone looks at the page.
        The dimensions and the source page travel with every hit, because the
        caller has two judgements to make and needs both -- whether it will hold up
        on screen, and whether its origin can be cited.
        """
        async with httpx.AsyncClient(proxy=self.proxy) as client:
            response = await client.post(
                "https://google.serper.dev/images",
                json={"q": query, "num": 20},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-API-KEY": self.api_key,
                },
                timeout=15.0,
            )
            response.raise_for_status()
        hits = response.json().get("images") or []
        usable = [
            hit
            for hit in hits
            if hit.get("imageUrl")
            and int(hit.get("imageWidth") or 0) >= min_width
            # 16:9 is the shape of a slide; something far taller than it is wide
            # cannot fill a region without being cropped past recognition.
            and int(hit.get("imageHeight") or 0) >= int(min_width * 9 / 16)
        ]
        if not usable:
            return (
                f"No images at least {min_width}px wide for: {query}\n"
                "Try a more specific query, or lower min_width if a smaller image is genuinely enough."
            )

        lines = [f"Image results for: {query}\n"]
        for index, hit in enumerate(usable[:count], 1):
            lines.append(f"{index}. {hit.get('title', '')}")
            lines.append(f"   {hit['imageUrl']}")
            source = hit.get("domain") or hit.get("source") or "unknown source"
            lines.append(f"   {hit.get('imageWidth')}x{hit.get('imageHeight')}px · {source}")
            if page := hit.get("link"):
                lines.append(f"   from: {page}")
        if len(usable) > count:
            lines.append(f"\n[{len(usable)} usable results, {count} shown.]")
        lines.append(
            "\nDownload one before use, then look at it: check that it depicts what the query asked for "
            "and that its origin can be cited."
        )
        return "\n".join(lines)

    async def execute(
        self,
        query: str,
        count: int | None = None,
        kind: str = "web",
        min_width: int | None = None,
        **kwargs: Any,
    ) -> str:
        if not self.api_key:
            return (
                "Error: Serper API key not configured. Set it in "
                "~/.raven/config.json under tools.web.search.apiKey "
                "(or export SERPER_API_KEY), then restart the gateway."
            )

        try:
            if kind == "images":
                return await self._search_images(
                    query,
                    min(max(count or self.max_results, 1), 10),
                    max(min_width or _MIN_IMAGE_WIDTH, 1),
                )
            n = min(max(count or self.max_results, 1), 10)
            logger.debug("WebSearch: {}", "proxy enabled" if self.proxy else "direct connection")
            async with httpx.AsyncClient(proxy=self.proxy) as client:
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
        except httpx.ProxyError as e:
            logger.error("WebSearch proxy error: {}", e)
            return f"Proxy error: {e}"
        except Exception as e:
            logger.error("WebSearch error: {}", e)
            return f"Error: {e}"


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
