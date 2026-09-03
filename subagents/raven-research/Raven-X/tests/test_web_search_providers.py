"""Per-provider request and response contracts of ``WebSearchTool``.

Three backends share one tool. What differs is the request each expects and the
payload it answers with; everything downstream -- containment, cross-query
dedup, the shaping row, the rendering -- is one code path, so each provider is
normalised to the Serper shape before anything reads it.

Two properties matter more than the mapping itself:

* The Serper request stays byte-identical. It is the anchor's wire traffic, and
  a provider table that quietly reformats it moves every arm at once.
* A provider that serves no offset declares that to the saturation rule at
  construction (``paginates``) rather than issuing a page-2 request it cannot
  answer. The rule then reports ``stopped_degraded``, which is what keeps "the
  pool ran out" and "this endpoint cannot page" separable in the ledger.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from raven.agent.search_saturation import SearchSaturation
from raven.agent.tools.web import (
    DEFAULT_SEARCH_PROVIDER,
    SEARCH_PROVIDERS,
    WebSearchTool,
    selected_search_key,
    selected_search_provider,
)
from raven.config.schema import (
    WebProviderKey,
    WebProvidersConfig,
    WebSearchConfig,
    WebToolsConfig,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _RecordingClient:
    """Records how the tool called out, and answers with one canned payload."""

    def __init__(self, response):
        self._response = response
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._response

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._response


def _patched(payload):
    client = _RecordingClient(_FakeResponse(payload))
    return client, patch("raven.agent.tools.web.httpx.AsyncClient", lambda **kw: client)


@pytest.fixture(autouse=True)
def _no_ambient_search_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for spec in SEARCH_PROVIDERS.values():
        monkeypatch.delenv(spec.env_var, raising=False)


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_serper_request_body_is_unchanged() -> None:
    """The anchor's wire traffic. A literal, not a round-trip through the table."""
    client, patcher = _patched({"organic": []})
    with patcher:
        await WebSearchTool(api_key="sk-serper")._search("q1", 3)

    method, url, kwargs = client.calls[0]
    assert (method, url) == ("POST", "https://google.serper.dev/search")
    assert kwargs["json"] == {"q": "q1", "num": 3}
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-KEY": "sk-serper",
    }


@pytest.mark.asyncio
async def test_serper_page_two_still_carries_the_page_key() -> None:
    client, patcher = _patched({"organic": []})
    with patcher:
        await WebSearchTool(api_key="sk-serper")._search("q1", 3, page=2)

    assert client.calls[0][2]["json"] == {"q": "q1", "num": 3, "page": 2}


@pytest.mark.asyncio
async def test_serpapi_sends_the_key_as_a_parameter_and_pages_by_offset() -> None:
    client, patcher = _patched({"organic_results": []})
    tool = WebSearchTool(api_key="sk-serp", provider="serpapi")
    with patcher:
        await tool._search("q1", 5)
        await tool._search("q1", 5, page=3)

    method, url, first = client.calls[0]
    assert (method, url) == ("GET", "https://serpapi.com/search")
    assert first["params"] == {"engine": "google", "q": "q1", "num": 5, "api_key": "sk-serp"}
    # page 3 of 5-wide results begins at the 10th, and page 1 carries no offset
    # at all so an un-escalated turn sends nothing the rule did not ask for.
    assert client.calls[1][2]["params"]["start"] == 10


@pytest.mark.asyncio
async def test_anysearch_sends_a_bearer_token_and_never_a_page() -> None:
    client, patcher = _patched({"results": []})
    tool = WebSearchTool(api_key="sk-any", provider="anysearch")
    with patcher:
        await tool._search("q1", 4)

    method, url, kwargs = client.calls[0]
    assert (method, url) == ("POST", "https://api.anysearch.com/v1/search")
    assert kwargs["json"] == {"query": "q1", "max_results": 4}
    assert kwargs["headers"]["Authorization"] == "Bearer sk-any"


# ---------------------------------------------------------------------------
# Pagination capability, declared rather than discovered
# ---------------------------------------------------------------------------


def test_a_provider_without_an_offset_tells_the_saturation_rule_up_front() -> None:
    """Otherwise the rung is reachable and unanswerable: the rule escalates to
    page 2, the request comes back identical, dedup eats it, and the ledger
    records a dry search the endpoint never had a chance to serve."""
    sat = SearchSaturation()
    WebSearchTool(api_key="k", provider="anysearch", saturation=sat)
    assert sat.paginates is False

    for provider in ("serper", "serpapi"):
        paging = SearchSaturation()
        WebSearchTool(api_key="k", provider=provider, saturation=paging)
        assert paging.paginates is True, provider


def test_the_same_dry_streak_pages_on_one_provider_and_degrades_on_the_other() -> None:
    """``paginated`` against ``stopped_degraded`` on identical evidence.

    The two arms see the same searches come back dry; what differs is only
    whether their endpoint can serve the next page. Collapsing that into one
    action would make a backend limitation read as the model giving up.
    """
    paging = SearchSaturation(k=2, on_saturate="paginate")
    WebSearchTool(api_key="k", provider="serper", saturation=paging)

    degrading = SearchSaturation(k=2, on_saturate="paginate")
    WebSearchTool(api_key="k", provider="anysearch", saturation=degrading)

    for _ in range(2):
        paging.observe(())
        degrading.observe(())

    assert paging.counters()["sat_action"] == "paginated"
    assert not paging.stopped

    assert degrading.counters()["sat_action"] == "stopped_degraded"
    assert degrading.stopped, "a rung it cannot climb closes search rather than looping"


# ---------------------------------------------------------------------------
# Response normalisation -- every provider renders through the shared path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serpapi_results_reach_the_shared_rendering() -> None:
    _, patcher = _patched(
        {
            "organic_results": [
                {"title": "T1", "link": "https://a.example", "snippet": "S1"}
            ],
            "answer_box": {"answer": "42"},
            "knowledge_graph": {"title": "K", "description": "KD"},
        }
    )
    with patcher:
        rendered, urls, shaping = await WebSearchTool(api_key="k", provider="serpapi")._search("q1", 5)

    assert urls == ["https://a.example"]
    assert "1. T1\n   https://a.example" in rendered
    assert "Answer: 42" in rendered
    assert "Knowledge: K" in rendered
    # The instrument column means the same thing on every backend: how many rows
    # the endpoint served, before the width slice and before dedup.
    assert shaping["n_served"] == 1


@pytest.mark.asyncio
async def test_anysearch_accepts_both_shapes_and_alternate_spellings() -> None:
    plain = {"results": [{"title": "T1", "url": "https://a.example", "snippet": "S1"}]}
    # Its auth endpoint wraps payloads in {code, message, data}; the search
    # response is undocumented, so both have to parse.
    enveloped = {
        "code": 0,
        "data": {"results": [{"title": "T2", "link": "https://b.example", "content": "C2"}]},
    }
    for payload, expected in ((plain, "https://a.example"), (enveloped, "https://b.example")):
        _, patcher = _patched(payload)
        with patcher:
            rendered, urls, _ = await WebSearchTool(api_key="k", provider="anysearch")._search("q1", 5)
        assert urls == [expected]
        assert expected in rendered


@pytest.mark.asyncio
async def test_a_malformed_anysearch_payload_is_a_zero_hit_not_a_crash() -> None:
    _, patcher = _patched({"results": "not-a-list"})
    with patcher:
        rendered, urls, shaping = await WebSearchTool(api_key="k", provider="anysearch")._search("q1", 5)

    assert rendered == "No results for: q1"
    assert urls == []
    # The endpoint WAS reached and served nothing usable, which is a different
    # row from one where no request went out at all.
    assert shaping["n_served"] == 0


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


def test_each_provider_resolves_only_its_own_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "sk-serper")
    assert WebSearchTool().api_key == "sk-serper"
    assert WebSearchTool(provider="anysearch").api_key == ""
    assert WebSearchTool(provider="serpapi").api_key == ""

    monkeypatch.setenv("ANYSEARCH_API_KEY", "sk-any")
    assert WebSearchTool(provider="anysearch").api_key == "sk-any"


def test_selected_search_key_reads_the_vendor_matching_the_provider() -> None:
    web = WebToolsConfig(
        search=WebSearchConfig(provider="anysearch"),
        providers=WebProvidersConfig(
            serper=WebProviderKey(api_key="sk-serper"),
            anysearch=WebProviderKey(api_key="sk-any"),
        ),
    )
    assert selected_search_key(web) == ("anysearch", "sk-any")
    assert selected_search_provider(web.search) == "anysearch"


def test_the_dict_key_and_the_specs_vendor_agree() -> None:
    # They are written twice and the config path is derived from the second, so
    # a disagreement would send one provider's key lookup at another's field.
    for name, spec in SEARCH_PROVIDERS.items():
        assert spec.vendor == name
        assert spec.config_path == f"tools.web.providers.{name}.apiKey"


def test_an_unknown_provider_degrades_to_the_default() -> None:
    assert WebSearchTool(provider="bing").provider == DEFAULT_SEARCH_PROVIDER
    assert selected_search_provider(object()) == DEFAULT_SEARCH_PROVIDER


@pytest.mark.asyncio
async def test_the_unconfigured_error_names_the_selected_provider() -> None:
    """Named to the OPERATOR, on stderr - not to the model.

    The message is split on purpose (main's product-surface audit): the tool
    return value tells the model the capability is gone and not to retry, and
    must not carry our config path, which a return value gets narrated back to
    whoever is watching. The provider's name, config path and env var go to the
    log instead - still per-spec, so a selected provider's missing key is never
    reported as another provider's.
    """
    from loguru import logger

    lines: list[str] = []
    sink_id = logger.add(lines.append, level="ERROR")
    try:
        rendered, urls, _ = await WebSearchTool(provider="serpapi")._search("q", 5)
    finally:
        logger.remove(sink_id)

    assert rendered.startswith("Error: web search is unavailable")
    assert "apiKey" not in rendered
    log = "".join(lines)
    assert "SerpApi" in log
    assert "tools.web.providers.serpapi.apiKey" in log
    assert "SERPAPI_API_KEY" in log
    assert urls == []
