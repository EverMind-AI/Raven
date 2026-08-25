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
        async with httpx.AsyncClient(proxy=self.proxy, trust_env=False) as client:
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
            async with httpx.AsyncClient(proxy=self.proxy, trust_env=False) as client:
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
    description = (
        "Fetch URL and extract readable content via Jina Reader. "
        'Set extractMode="images" to list pictures from a page a report cited, with direct URLs, alt text, '
        "captions and document position. Run that citation sweep beside web_search(kind='images'); download "
        "selected candidates with ppt_fetch and inspect them before placement."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text", "images"], "default": "markdown"},
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

    _IMAGE_NOISE = (
        "logo",
        "icon",
        "avatar",
        "sprite",
        "spacer",
        "pixel",
        "badge",
        "button",
        "banner-ad",
        "favicon",
        "emoji",
        "placeholder",
        "loading",
        "/funders/",
        "/sponsors/",
        "/partners/",
        "/branding/",
    )
    _IMAGE_CHROME_PARENTS = ("header", "footer", "nav", "aside")

    async def _reader_get(self, url: str) -> httpx.Response:
        headers = {"Accept": "text/plain"}
        async with httpx.AsyncClient(proxy=self.proxy, follow_redirects=True, trust_env=False) as client:
            if self.api_key:
                response = await client.get(
                    f"https://r.jina.ai/{url}",
                    headers={**headers, "Authorization": f"Bearer {self.api_key}"},
                    timeout=30.0,
                )
                if response.status_code not in (401, 402, 403):
                    response.raise_for_status()
                    return response
            response = await client.get(f"https://r.jina.ai/{url}", headers=headers, timeout=30.0)
            response.raise_for_status()
            return response

    async def _extract_images_via_reader(self, url: str, reason: str) -> str:
        import re as _re

        response = await self._reader_get(url)
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for alt, src in _re.findall(r"!\[([^\]]*)\]\(([^)\s]+)", response.text):
            if src.startswith("data:") or src in seen or any(token in src.lower() for token in self._IMAGE_NOISE):
                continue
            seen.add(src)
            found.append((alt.strip(), src))
        if not found:
            return json.dumps(
                {"url": url, "images": [], "note": f"direct fetch refused ({reason}); reader found no images"},
                ensure_ascii=False,
            )
        lines = [f"Images on {url} ({len(found)} candidates, via reader)\n"]
        for index, (alt, src) in enumerate(found[:12], 1):
            lines.append(f"{index}. {src}")
            if alt:
                lines.append(f'   alt: "{alt}"')
        lines.append("\nDownload one with ppt_fetch, then inspect it before placement.")
        return "\n".join(lines)

    # Redirects are walked by hand rather than left to httpx. `execute` validates the
    # URL it was given, and automatic following then requested whatever `Location`
    # named -- so a public page could hand back an internal one. Every hop is put
    # through the same validator as the first.
    MAX_REDIRECTS = 5

    async def _get_checked_redirects(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        *,
        timeout: float,
    ) -> tuple[httpx.Response | None, str]:
        """The response, or a refusal naming the hop that failed validation."""
        current = url
        for _ in range(self.MAX_REDIRECTS + 1):
            response = await client.get(current, headers=headers, timeout=timeout)
            if not response.is_redirect:
                return response, ""
            target = str(response.url.join(response.headers.get("location") or ""))
            is_valid, error_msg = validate_url_target(target)
            if not is_valid:
                return None, f"refused a redirect to {target}: {error_msg}"
            current = target
        return None, f"more than {self.MAX_REDIRECTS} redirects from {url}"

    async def _extract_images(self, url: str) -> str:
        from urllib.parse import urljoin, urlparse

        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        async with httpx.AsyncClient(proxy=self.proxy, follow_redirects=False, trust_env=False) as client:
            try:
                response, refused = await self._get_checked_redirects(client, url, headers, timeout=20.0)
                if refused:
                    return json.dumps({"url": url, "error": refused}, ensure_ascii=False)
                response.raise_for_status()
            except httpx.HTTPError as direct_error:
                return await self._extract_images_via_reader(url, str(direct_error))
            if "html" not in (response.headers.get("content-type") or "").lower():
                return json.dumps(
                    {
                        "url": url,
                        "content_type": response.headers.get("content-type") or "unknown",
                        "note": "not an HTML page; download it with ppt_fetch so ingest can extract its figures",
                    },
                    ensure_ascii=False,
                )
            soup = BeautifulSoup(response.text, "html.parser")

        seen: set[str] = set()
        candidates: list[dict[str, Any]] = []

        def consider(src: str | None, *, alt: str = "", caption: str = "", where: str = "", rank: int = 3) -> None:
            if not src or src.startswith("data:"):
                return
            absolute = urljoin(str(response.url), src.strip())
            if not urlparse(absolute).scheme.startswith("http") or absolute in seen:
                return
            if any(token in absolute.lower() for token in self._IMAGE_NOISE):
                return
            seen.add(absolute)
            candidates.append(
                {"url": absolute, "alt": alt.strip()[:160], "caption": caption.strip()[:200], "where": where, "rank": rank}
            )

        if og := soup.find("meta", property="og:image"):
            consider(og.get("content"), where="og:image", rank=1)
        for figure in soup.find_all("figure"):
            image = figure.find("img")
            if image is None:
                continue
            caption = figure.find("figcaption")
            consider(
                image.get("src") or image.get("data-src"),
                alt=image.get("alt", ""),
                caption=caption.get_text(" ", strip=True) if caption else "",
                where="figure with caption" if caption else "figure",
                rank=0,
            )
        for image in soup.find_all("img"):
            if image.find_parent(self._IMAGE_CHROME_PARENTS):
                continue
            article = image.find_parent("article") is not None
            consider(
                image.get("src") or image.get("data-src"),
                alt=image.get("alt", ""),
                where="article image" if article else "page image",
                rank=2 if article else 4,
            )

        if not candidates:
            note = "no usable images found on this page"
            if any(token in url.lower() for token in ("arxiv.org/abs", "/abstract", "doi.org")):
                note += "; download the paper PDF with ppt_fetch and let ingest extract its figures"
            return json.dumps({"url": url, "images": [], "note": note}, ensure_ascii=False)
        candidates.sort(key=lambda item: item["rank"])
        lines = [f"Images on {url} ({len(candidates)} candidates, best first)\n"]
        for index, item in enumerate(candidates[:12], 1):
            lines.append(f"{index}. {item['url']}")
            detail = [item["where"]]
            if item["alt"]:
                detail.append(f'alt: "{item["alt"]}"')
            lines.append("   " + " · ".join(detail))
            if item["caption"]:
                lines.append(f'   caption: "{item["caption"]}"')
        lines.append("\nDownload one with ppt_fetch, then inspect it before placement.")
        return "\n".join(lines)

    async def execute(self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:  # noqa: N803  (LLM tool schema uses camelCase)
        max_chars = maxChars or self.max_chars
        is_valid, error_msg = validate_url_target(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        if extractMode == "images":
            try:
                return await self._extract_images(url)
            except Exception as exc:
                logger.error("WebFetch image extraction error: {!r}", exc)
                return json.dumps({"error": f"{type(exc).__name__}: {exc}", "url": url}, ensure_ascii=False)

        try:
            logger.debug("WebFetch: {}", "proxy enabled" if self.proxy else "direct connection")
            r = await self._reader_get(url)

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
