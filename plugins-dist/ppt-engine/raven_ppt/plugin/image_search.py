"""``ppt_image_search``: picture search under the plugin's own name (D2).

The fork grew this surface INSIDE the trunk built-in ``web_search`` as
``kind="images"`` (fork ``raven/agent/tools/web.py``, the +304/-14 shared-code
drift). Trunk's web_search carries no such parameter, and a plugin tool that
shadowed the built-in's name to add one would be a seam this migration refused
to open -- so the search half boards as a tool of its own, ported line for
line from the fork's ``_search_images`` (fork web.py:25, 67-119). The model-
visible name changes; the behaviour -- Serper's image surface, the 640px
usability floor, the 16:9 shortness floor, dimensions and origin on every hit
-- does not. D2 in the ppt verdict records the name change for the partner;
``kind`` upstreaming stays an optional card.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from raven.contracts.tool import Tool

# Below this a picture is already soft at half-page width on a 1280px canvas.
_MIN_IMAGE_WIDTH = 640
# How many of a batch's queries are in flight at once. `ppt_fetch`'s number, for the
# same reason it has one: this is a courtesy bound on one endpoint, not a tuned figure.
_SEARCH_CONCURRENCY = 4
# How many queries one call takes. A deck's pictures are one pass over the outline's
# `needs`, and twenty pages do not ask for more than this many distinct things.
MAX_QUERIES = 12


class PptImageSearchTool(Tool):
    """Search pictures for a deck, dropping what a slide cannot use."""

    name = "ppt_image_search"
    description = (
        "Search the web for pictures: results carry the direct image URL, its pixel dimensions and the "
        "page it came from, and anything too small to hold up on a screen is dropped rather than "
        "offered. Use it for a real logo, product screen, published plot or other existing evidence; "
        "ppt_generate_image is for visuals that do not exist. Pass every picture the deck needs as "
        "queries=[...] in one call -- they run together and come back grouped by query, so a deck's "
        "whole picture search is one round rather than one per page. Download what you select with "
        "ppt_fetch, passing the words its page printed about it as the caption, so the ingest reads "
        "it into this deck's evidence."
    )
    parameters = {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MAX_QUERIES,
                "description": "every picture this deck needs, one query each; they are searched together",
            },
            "query": {"type": "string", "description": "one query, for a single follow-up search"},
            "count": {"type": "integer", "description": "Results per query (1-10)", "minimum": 1, "maximum": 10},
            "min_width": {
                "type": "integer",
                "minimum": 1,
                "description": "drop anything narrower than this in pixels (default 640)",
            },
        },
    }

    def __init__(self, api_key: str | None = None, max_results: int = 5, proxy: str | None = None):
        self._init_api_key = api_key
        self.max_results = max_results
        self.proxy = proxy

    @property
    def api_key(self) -> str:
        """Resolve the key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get("SERPER_API_KEY", "")

    async def execute(
        self,
        queries: list[str] | None = None,
        query: str | None = None,
        count: int | None = None,
        min_width: int | None = None,
        **kwargs: Any,
    ) -> str:
        """One search or a deck's worth, in one round.

        `query` is kept beside `queries` rather than replaced: a follow-up on one page
        is a single search, and making the caller wrap it in a list to ask for one
        picture is a worse tool for the more common of the two calls.
        """
        if not self.api_key:
            return (
                "Error: Serper API key not configured. Set it in "
                'plugins.config["ppt-engine"].imageSearch.apiKey (or export SERPER_API_KEY), '
                "then restart the agent."
            )
        wanted = [said.strip() for said in (queries or ([query] if query else [])) if said and said.strip()]
        if not wanted:
            return "Error: pass queries=[...] with the pictures this deck needs, or query='...' for one."
        if len(wanted) > MAX_QUERIES:
            return f"Error: {len(wanted)} queries in one call; {MAX_QUERIES} is the most. Split them."
        per_query = min(max(count or self.max_results, 1), 10)
        floor = max(min_width or _MIN_IMAGE_WIDTH, 1)
        gate = asyncio.Semaphore(_SEARCH_CONCURRENCY)

        async def one(said: str) -> str:
            async with gate:
                try:
                    return await self._search_images(said, per_query, floor)
                except Exception as exc:  # noqa: BLE001 -- one query's failure is not the batch's
                    return f"Image results for: {said}\n\nThis search failed ({type(exc).__name__}: {exc})."

        found = await asyncio.gather(*(one(said) for said in wanted))
        if len(found) == 1:
            return found[0]
        return ("\n\n" + "-" * 60 + "\n\n").join(found)

    async def _search_images(self, query: str, count: int, min_width: int) -> str:
        """Serper's image surface, filtered to what a slide can actually use.

        A picture on a slide needs pixels: an image narrower than roughly 640px is
        already soft at half-page width on a 1280px canvas, so those are dropped
        here rather than offered and rejected later once someone looks at the page.
        The dimensions and the source page travel with every hit, because the
        caller has two judgements to make and needs both -- whether it will hold up
        on screen, and whether its origin can be cited.
        """
        async with httpx.AsyncClient(proxy=self.proxy, trust_env=True) as client:
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
            lines.append(f"   {hit.get('imageWidth')}x{hit.get('imageHeight')}px - {source}")
            if page := hit.get("link"):
                lines.append(f"   from: {page}")
        if len(usable) > count:
            lines.append(f"\n[{len(usable)} usable results, {count} shown.]")
        lines.append(
            "\nDownload one before use, then look at it: check that it depicts what the query asked for "
            "and that its origin can be cited."
        )
        return "\n".join(lines)
