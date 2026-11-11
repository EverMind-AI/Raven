"""``ppt_image_search``: the deck's picture search on the host's selected vendor.

The fork grew this surface INSIDE the trunk built-in ``web_search`` as
``kind="images"``. Trunk carries no such parameter, so the search half boards
as a tool of its own. The page-level contract is unchanged: one call for every
picture the deck needs, the 640px floor, the 16:9 shortness floor, dimensions
and the source page on every hit. The vendor behind it is now the host's --
Serper, SerpApi, Brave, Firecrawl or Serply, with Serper as the fallback --
through :class:`raven.agent.tools.web.ImageSearchTool`. The one strictness this
subclass adds is ``require_dimensions``: a deck cannot judge a picture whose
size nobody reported, so such a hit is dropped rather than offered.
"""

from __future__ import annotations

from raven.agent.tools.web import IMAGE_MAX_QUERIES, ImageSearchTool

MAX_QUERIES = IMAGE_MAX_QUERIES


class PptImageSearchTool(ImageSearchTool):
    """Search pictures for a deck, on the vendor the host selected."""

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

    def __init__(
        self,
        api_key: str | None = None,
        max_results: int = 5,
        proxy: str | None = None,
        provider: str = "serper",
    ) -> None:
        super().__init__(
            api_key=api_key,
            max_results=max_results,
            proxy=proxy,
            provider=provider,
            require_dimensions=True,
        )
