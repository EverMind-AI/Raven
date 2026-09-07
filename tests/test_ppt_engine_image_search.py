"""`ppt_image_search`, and the round trips a deck's pictures used to cost.

Every other gathering tool on this route takes a list -- `ppt_fetch` takes `urls`,
`ppt_generate_image` takes `prompts` -- and this one took a single `query`, so an
author following the skill's own order (search the deck's pictures first, widely,
before the outline names what each page shows) paid one round trip per picture.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from raven_ppt.plugin.image_search import MAX_QUERIES, PptImageSearchTool


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    """Records the queries it was asked and how many were in flight at once."""

    live = 0
    most_at_once = 0
    asked: list[str] = []
    fails: set[str] = set()
    delay = 0.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, _url: str, *, json: dict[str, Any], **kwargs: Any) -> _Response:
        said = json["q"]
        type(self).asked.append(said)
        type(self).live += 1
        type(self).most_at_once = max(type(self).most_at_once, type(self).live)
        try:
            await asyncio.sleep(type(self).delay)
            if said in type(self).fails:
                raise RuntimeError("serper said 503")
            return _Response(
                {
                    "images": [
                        {
                            "title": f"{said} picture",
                            "imageUrl": f"https://pics.example/{said}.png",
                            "imageWidth": 1600,
                            "imageHeight": 900,
                            "domain": "example.com",
                            "link": f"https://example.com/{said}",
                        }
                    ]
                }
            )
        finally:
            type(self).live -= 1


@pytest.fixture(autouse=True)
def _client(monkeypatch) -> None:
    _Client.asked = []
    _Client.fails = set()
    _Client.live = 0
    _Client.most_at_once = 0
    _Client.delay = 0.0
    monkeypatch.setattr("raven_ppt.plugin.image_search.httpx.AsyncClient", _Client)


def _tool() -> PptImageSearchTool:
    return PptImageSearchTool(api_key="k")


async def test_a_decks_pictures_are_searched_in_one_call_and_come_back_grouped() -> None:
    reply = await _tool().execute(queries=["成都夜市", "night market lighting", "台北宁夏夜市"])

    assert _Client.asked == ["成都夜市", "night market lighting", "台北宁夏夜市"]
    for said in _Client.asked:
        assert f"Image results for: {said}" in reply
        assert f"https://pics.example/{said}.png" in reply
    assert reply.count("Image results for:") == 3, "one group per query"


async def test_the_queries_of_one_call_run_together() -> None:
    _Client.delay = 0.05

    started = asyncio.get_running_loop().time()
    await _tool().execute(queries=[f"q{n}" for n in range(4)])

    assert _Client.most_at_once > 1, "four searches ran one after another"
    assert asyncio.get_running_loop().time() - started < 0.15


async def test_no_more_than_the_concurrency_gate_is_in_flight() -> None:
    """A courtesy bound on one endpoint, the same one `ppt_fetch` holds."""
    from raven_ppt.plugin.image_search import _SEARCH_CONCURRENCY

    _Client.delay = 0.02
    await _tool().execute(queries=[f"q{n}" for n in range(MAX_QUERIES)])

    assert _Client.most_at_once <= _SEARCH_CONCURRENCY


async def test_one_query_that_fails_does_not_cost_the_others() -> None:
    _Client.fails = {"broken"}

    reply = await _tool().execute(queries=["fine", "broken", "also fine"])

    assert "This search failed (RuntimeError: serper said 503)" in reply
    assert "https://pics.example/fine.png" in reply
    assert "https://pics.example/also fine.png" in reply


async def test_a_single_query_still_takes_the_singular_form() -> None:
    """Kept beside `queries` rather than replaced: a follow-up on one page is one
    search, and wrapping it in a list is a worse tool for the more common call."""
    reply = await _tool().execute(query="成都夜市")

    assert _Client.asked == ["成都夜市"]
    assert reply.startswith("Image results for: 成都夜市")
    assert "-" * 60 not in reply, "one query is one group, with no separator"


async def test_neither_form_is_an_error_that_names_both() -> None:
    reply = await _tool().execute()

    assert reply.startswith("Error:") and "queries=[...]" in reply and "query=" in reply
    assert _Client.asked == []


async def test_more_queries_than_one_call_takes_is_refused_rather_than_truncated() -> None:
    """Truncating would search some of the deck's pictures and say nothing about the
    rest, which reads as "there were no results" for the ones that were dropped."""
    reply = await _tool().execute(queries=[f"q{n}" for n in range(MAX_QUERIES + 1)])

    assert reply.startswith("Error:") and str(MAX_QUERIES) in reply
    assert _Client.asked == []


async def test_the_schema_offers_the_batch_first() -> None:
    schema = _tool().parameters

    assert schema["properties"]["queries"]["maxItems"] == MAX_QUERIES
    assert schema["properties"]["queries"]["items"] == {"type": "string"}
    assert "required" not in schema, "either form is enough on its own"
    assert "queries=[...]" in _tool().description


async def test_a_missing_key_is_said_once_rather_than_per_query() -> None:
    reply = await PptImageSearchTool(api_key=None).execute(queries=["a", "b"])

    assert reply.count("Serper API key not configured") == 1
    assert _Client.asked == []


async def test_what_a_slide_cannot_use_is_dropped_before_it_is_offered(monkeypatch) -> None:
    """The floor the tool was written for, still applied per query in a batch."""

    class _Small(_Client):
        async def post(self, _url: str, *, json: dict[str, Any], **kwargs: Any) -> _Response:
            type(self).asked.append(json["q"])
            return _Response({"images": [{"imageUrl": "https://pics/x.png", "imageWidth": 320, "imageHeight": 180}]})

    monkeypatch.setattr("raven_ppt.plugin.image_search.httpx.AsyncClient", _Small)

    reply = await _tool().execute(queries=["tiny"])

    assert "No images at least 640px wide for: tiny" in reply
    assert json.dumps(reply)  # a plain string, not a structure the caller has to parse
