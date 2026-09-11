"""Per-vendor request and response contracts of the host web tools.

Several backends share each tool. What differs is the request a vendor expects
and the payload it answers with; everything the model reads -- the rendered
result list, the fetch envelope -- is one code path, so each vendor is
normalised to the Serper (search) or Jina (fetch) shape before anything reads
it. These tests pin the request each vendor receives and the rendering its
payload produces, with the transport replaced by a recorder.

The vendor list itself has three homes -- the schema's ``Literal``, the spec
tables here, and the settings page's own arrays -- so the last section pins all
three together. The front end cannot import the Python one, and a list mirrored
by hand drifts silently: adding a vendor to the schema alone leaves the browser
offering one fewer.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from raven.agent.tools import web as web_mod
from raven.agent.tools.web import (
    DEFAULT_FETCH_PROVIDER,
    DEFAULT_SEARCH_PROVIDER,
    FETCH_PROVIDERS,
    SEARCH_PROVIDERS,
    WebFetchTool,
    WebSearchTool,
)
from raven.config.schema import (
    WEB_VENDOR_ENV_VARS,
    Config,
    WebFetchProvider,
    WebProvidersConfig,
    WebSearchProvider,
)

pytestmark = pytest.mark.asyncio


class _Recorder:
    """Stands in for ``httpx.AsyncClient``: records the call, answers a canned body."""

    def __init__(self, payload: Any, status: int = 200, text: str | None = None) -> None:
        self.payload = payload
        self.status = status
        self.text = text
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "_Recorder":
        return self

    async def __aenter__(self) -> "_Recorder":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        request = httpx.Request(method, url)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, request=request)
        return httpx.Response(self.status, json=self.payload, request=request)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("POST", url, kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("GET", url, kwargs)


@contextmanager
def _patched(monkeypatch: pytest.MonkeyPatch, payload: Any, **kw: Any):
    recorder = _Recorder(payload, **kw)
    monkeypatch.setattr(web_mod.httpx, "AsyncClient", recorder)
    yield recorder


@pytest.fixture(autouse=True)
def _no_ambient_vendor_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in WEB_VENDOR_ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# The tables agree with the schema


def test_the_spec_tables_match_the_schema_literals() -> None:
    from typing import get_args

    assert set(SEARCH_PROVIDERS) == set(get_args(WebSearchProvider))
    assert set(FETCH_PROVIDERS) == set(get_args(WebFetchProvider))
    for vendor, spec in {**SEARCH_PROVIDERS, **FETCH_PROVIDERS}.items():
        assert spec.env_var == WEB_VENDOR_ENV_VARS[vendor]
        assert spec.config_path == f"tools.web.providers.{vendor}.apiKey"


def test_an_unknown_vendor_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        WebSearchTool(provider="bing")
    with pytest.raises(ValueError):
        WebFetchTool(provider="bing")


# --------------------------------------------------------------------------- #
# Keys: the constructor value first, then the vendor's own env var


@pytest.mark.parametrize("vendor", sorted(SEARCH_PROVIDERS))
def test_each_search_vendor_falls_back_to_its_own_env_var(vendor: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not WebSearchTool(provider=vendor).api_key
    monkeypatch.setenv(WEB_VENDOR_ENV_VARS[vendor], "from-env")
    assert WebSearchTool(provider=vendor).api_key == "from-env"
    assert WebSearchTool(api_key="from-config", provider=vendor).api_key == "from-config"
    assert WebSearchTool.is_configured(None, provider=vendor)
    # Another vendor's variable does not satisfy this one.
    other = next(v for v in SEARCH_PROVIDERS if v != vendor)
    assert not WebSearchTool.is_configured(None, provider=other)


async def test_the_unconfigured_error_names_the_selected_vendors_slot(tmp_path) -> None:
    from raven.config import loader

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    loader.set_config_path(cfg)
    try:
        answer = await WebSearchTool(provider="tavily").execute("q")
    finally:
        loader.set_config_path(None)
    assert "Tavily" in answer
    assert "tools.web.providers.tavily.apiKey" in answer and "TAVILY_API_KEY" in answer
    assert str(cfg) in answer


# --------------------------------------------------------------------------- #
# Search: what each vendor is sent


_SEARCH_REQUESTS: dict[str, tuple[str, str, dict[str, Any], dict[str, str]]] = {
    "serper": ("POST", "https://google.serper.dev/search", {"json": {"q": "q1", "num": 4}}, {"X-API-KEY": "k"}),
    "serpapi": (
        "GET",
        "https://serpapi.com/search",
        {"params": {"engine": "google", "q": "q1", "num": 4, "api_key": "k"}},
        {},
    ),
    "anysearch": (
        "POST",
        "https://api.anysearch.com/v1/search",
        {"json": {"query": "q1", "max_results": 4}},
        {"Authorization": "Bearer k"},
    ),
    "tavily": (
        "POST",
        "https://api.tavily.com/search",
        {"json": {"query": "q1", "max_results": 4}},
        {"Authorization": "Bearer k"},
    ),
    "exa": (
        "POST",
        "https://api.exa.ai/search",
        {"json": {"query": "q1", "numResults": 4, "contents": {"highlights": {"maxCharacters": 300}}}},
        {"x-api-key": "k"},
    ),
    "brave": (
        "GET",
        "https://api.search.brave.com/res/v1/web/search",
        {"params": {"q": "q1", "count": 4}},
        {"X-Subscription-Token": "k"},
    ),
    "firecrawl": (
        "POST",
        "https://api.firecrawl.dev/v1/search",
        {"json": {"query": "q1", "limit": 4}},
        {"Authorization": "Bearer k"},
    ),
}


@pytest.mark.parametrize("vendor", sorted(_SEARCH_REQUESTS))
async def test_each_search_vendor_gets_the_request_it_documents(vendor: str, monkeypatch: pytest.MonkeyPatch) -> None:
    method, url, body, auth = _SEARCH_REQUESTS[vendor]
    with _patched(monkeypatch, {}) as client:
        await WebSearchTool(api_key="k", provider=vendor).execute("q1", count=4)

    got_method, got_url, kwargs = client.calls[0]
    assert (got_method, got_url) == (method, url)
    for field, expected in body.items():
        assert kwargs[field] == expected
    for header, value in auth.items():
        assert kwargs["headers"][header] == value


@pytest.mark.parametrize("vendor", sorted(SEARCH_PROVIDERS))
async def test_a_status_error_never_echoes_the_key(vendor: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx puts the whole request URL in a status error, and SerpApi carries
    its key as a query parameter, so the default text would hand the credential
    to the model and the log. Every vendor renders as vendor plus status."""
    with _patched(monkeypatch, {}, status=401):
        rendered = await WebSearchTool(api_key="SECRET-KEY-123", provider=vendor).execute("q1")

    assert "SECRET-KEY-123" not in rendered
    assert rendered == f"Error: {SEARCH_PROVIDERS[vendor].label} answered HTTP 401"


def test_every_search_vendor_is_covered_by_the_request_table() -> None:
    assert set(_SEARCH_REQUESTS) == set(SEARCH_PROVIDERS)


# --------------------------------------------------------------------------- #
# Search: what each vendor's payload renders as


_SEARCH_PAYLOADS: dict[str, Any] = {
    "serper": {"organic": [{"title": "T1", "link": "https://a.example", "snippet": "S1"}]},
    "serpapi": {"organic_results": [{"title": "T1", "link": "https://a.example", "snippet": "S1"}]},
    "anysearch": {"data": {"results": [{"title": "T1", "url": "https://a.example", "content": "S1"}]}},
    "tavily": {"results": [{"title": "T1", "url": "https://a.example", "content": "S1"}]},
    "exa": {"results": [{"title": "T1", "url": "https://a.example", "highlights": ["S1\n\nmore"], "text": "x" * 500}]},
    "brave": {"web": {"results": [{"title": "T1", "url": "https://a.example", "description": "S1"}]}},
    "firecrawl": {"success": True, "data": [{"title": "T1", "url": "https://a.example", "description": "S1"}]},
}


@pytest.mark.parametrize("vendor", sorted(_SEARCH_PAYLOADS))
async def test_each_search_vendor_renders_through_the_shared_path(vendor: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with _patched(monkeypatch, _SEARCH_PAYLOADS[vendor]):
        rendered = await WebSearchTool(api_key="k", provider=vendor).execute("q1")

    assert rendered.startswith("Results for: q1\n")
    assert "1. T1\n   https://a.example\n   S1" in rendered
    # Exa's whole-page ``text`` never reaches the rendering; only the highlight does.
    assert "xxxx" not in rendered


async def test_a_firecrawl_refusal_inside_a_200_reads_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with _patched(monkeypatch, {"success": False, "error": "quota exhausted"}):
        rendered = await WebSearchTool(api_key="k", provider="firecrawl").execute("q1")

    assert rendered.startswith("Error:") and "quota exhausted" in rendered
    assert "No results" not in rendered


async def test_an_empty_payload_is_a_dry_search_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with _patched(monkeypatch, {"results": []}):
        rendered = await WebSearchTool(api_key="k", provider="tavily").execute("q1")

    assert rendered == "No results for: q1"


# --------------------------------------------------------------------------- #
# Fetch: registration falls back to Jina when the selected reader cannot run


def test_a_keyed_reader_without_a_key_is_replaced_by_jina(monkeypatch: pytest.MonkeyPatch) -> None:
    assert WebFetchTool.effective_provider("firecrawl", None) == "jina"
    assert WebFetchTool.effective_provider("firecrawl", "k") == "firecrawl"
    monkeypatch.setenv("FIRECRAWL_API_KEY", "from-env")
    assert WebFetchTool.effective_provider("firecrawl", None) == "firecrawl"
    # Jina needs nothing, so it is never replaced.
    assert WebFetchTool.effective_provider("jina", None) == "jina"


# --------------------------------------------------------------------------- #
# Fetch: what each reader is sent, and what its page becomes


@pytest.fixture
def _open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSRF gate resolves the hostname, which a test host must not do; the
    gate has its own suite (``test_security_web_ssrf``)."""
    monkeypatch.setattr(web_mod, "validate_url_target", lambda url: (True, ""))


_FETCH_CASES: dict[str, tuple[str, str, dict[str, Any], Any]] = {
    "jina": ("GET", "https://r.jina.ai/https://a.example", {}, None),
    "tavily": (
        "POST",
        "https://api.tavily.com/extract",
        {"json": {"urls": ["https://a.example"]}},
        {"results": [{"url": "https://a.example", "raw_content": "PAGE"}]},
    ),
    "exa": (
        "POST",
        "https://api.exa.ai/contents",
        {"json": {"urls": ["https://a.example"], "text": True}},
        {"results": [{"url": "https://a.example", "title": "T", "text": "PAGE"}]},
    ),
    "firecrawl": (
        "POST",
        "https://api.firecrawl.dev/v1/scrape",
        {"json": {"url": "https://a.example", "formats": ["markdown"]}},
        {"success": True, "data": {"markdown": "PAGE", "metadata": {"title": "T"}}},
    ),
    "anysearch": (
        "POST",
        "https://api.anysearch.com/v1/extract",
        {"json": {"url": "https://a.example"}},
        {"code": 0, "data": {"content": "PAGE", "title": "T"}},
    ),
}


@pytest.mark.parametrize("vendor", sorted(_FETCH_CASES))
async def test_each_reader_serves_the_page_through_one_envelope(
    vendor: str, monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    method, url, body, payload = _FETCH_CASES[vendor]
    kw = {"payload": payload} if payload is not None else {"payload": None, "text": "PAGE"}
    with _patched(monkeypatch, **kw) as client:
        raw = await WebFetchTool(api_key="k", provider=vendor).execute("https://a.example")

    got_method, got_url, kwargs = client.calls[0]
    assert (got_method, got_url) == (method, url)
    for field, expected in body.items():
        assert kwargs[field] == expected
    if vendor != "jina":
        assert "Bearer k" in kwargs["headers"].values() or kwargs["headers"].get("x-api-key") == "k"

    envelope = json.loads(raw)
    assert envelope["text"] == "PAGE"
    assert envelope["extractor"] == FETCH_PROVIDERS[vendor].extractor
    assert envelope["length"] == 4 and envelope["truncated"] is False


@pytest.mark.parametrize("vendor", sorted(FETCH_PROVIDERS))
async def test_a_reader_status_error_names_the_vendor_and_status_only(
    vendor: str, monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    with _patched(monkeypatch, {}, status=402):
        raw = await WebFetchTool(api_key="SECRET-KEY-123", provider=vendor).execute("https://a.example")

    envelope = json.loads(raw)
    assert "SECRET-KEY-123" not in raw
    assert envelope["error"] == f"{FETCH_PROVIDERS[vendor].label} answered HTTP 402"


def test_every_reader_is_covered_by_the_fetch_table() -> None:
    assert set(_FETCH_CASES) == set(FETCH_PROVIDERS)


async def test_a_reader_answering_without_a_page_is_an_error_envelope(
    monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    with _patched(monkeypatch, {"results": [], "failed_results": [{"url": "https://a.example", "error": "blocked"}]}):
        raw = await WebFetchTool(api_key="k", provider="tavily").execute("https://a.example")

    envelope = json.loads(raw)
    assert "text" not in envelope
    assert "blocked" in envelope["error"]


async def test_an_anysearch_envelope_failure_is_read_as_one(monkeypatch: pytest.MonkeyPatch, _open_gate: None) -> None:
    with _patched(monkeypatch, {"code": 401, "message": "bad key"}):
        raw = await WebFetchTool(api_key="k", provider="anysearch").execute("https://a.example")

    assert "bad key" in json.loads(raw)["error"]


# --------------------------------------------------------------------------- #
# The settings page's own copy of the vendor list


_SETTINGS_PAGE = Path(__file__).resolve().parents[1] / "ui-web/src/features/settings/SettingsPage.tsx"


def _tsx_vendor_pick(tool: str) -> dict[str, Any]:
    """One ``WEB_VENDOR`` entry as the settings page declares it.

    Parsed from the source rather than exported through a generated artefact:
    the list is eight short strings, and a build step to carry them would be
    more machinery than the thing it carries. The asserts below fail loudly if
    the shape it reads stops being there, which is the state a silent regex
    would report as an empty list.
    """
    body = _SETTINGS_PAGE.read_text(encoding="utf-8")
    entry = re.search(rf"\b{tool}:\s*\{{(.*?)\}},", body, re.S)
    assert entry, f"{tool} has no WEB_VENDOR entry in {_SETTINGS_PAGE.name}"
    block = entry.group(1)
    vendors = re.search(r"vendors:\s*\[(.*?)\]", block, re.S)
    path = re.search(r"path:\s*'([^']+)'", block)
    fallback = re.search(r"fallback:\s*'([^']+)'", block)
    assert vendors and path and fallback, f"{tool}'s WEB_VENDOR entry lost a field"
    return {
        "vendors": re.findall(r"'([^']+)'", vendors.group(1)),
        "path": path.group(1),
        "fallback": fallback.group(1),
    }


def _tsx_labels() -> dict[str, str]:
    body = _SETTINGS_PAGE.read_text(encoding="utf-8")
    block = re.search(r"WEB_VENDOR_LABEL:\s*Record<string, string>\s*=\s*\{(.*?)\n\}", body, re.S)
    assert block, f"WEB_VENDOR_LABEL is gone from {_SETTINGS_PAGE.name}"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))


def test_the_settings_page_offers_exactly_the_schemas_vendors() -> None:
    """Same vendors, same order, same default. Order because the pick is a
    ``select``: a list the browser shows in another order than the wizard is
    two answers to one question."""
    from typing import get_args

    search = _tsx_vendor_pick("web_search")
    fetch = _tsx_vendor_pick("web_fetch")

    assert search["vendors"] == list(get_args(WebSearchProvider))
    assert fetch["vendors"] == list(get_args(WebFetchProvider))
    # Not vacuous: the parse found real lists, so an empty one cannot pass.
    assert len(search["vendors"]) == 7 and len(fetch["vendors"]) == 5
    assert search["fallback"] == DEFAULT_SEARCH_PROVIDER
    assert fetch["fallback"] == DEFAULT_FETCH_PROVIDER
    assert search["path"] == "tools.web.search.provider"
    assert fetch["path"] == "tools.web.fetch.provider"


def test_the_settings_page_names_every_vendor_the_way_the_spec_table_does() -> None:
    """The label is what a person picks by. Two spellings of one vendor across
    two surfaces reads as two vendors."""
    labels = _tsx_labels()
    spec_labels = {v: s.label for v, s in SEARCH_PROVIDERS.items()}
    for vendor, spec in FETCH_PROVIDERS.items():
        assert spec_labels.setdefault(vendor, spec.label) == spec.label, (
            f"{vendor} is labelled differently in the two spec tables"
        )

    assert labels == spec_labels
    assert set(labels) == set(WEB_VENDOR_ENV_VARS), "every vendor a key can be held for needs a label"


# --------------------------------------------------------------------------- #
# The credential table as a hand-edited file reaches it
# --------------------------------------------------------------------------- #


def test_a_misspelled_vendor_fails_validation() -> None:
    """What the section's docstring promises. The tree's default policy is to
    drop an unknown member, which would leave the vendor unkeyed and its tool
    withheld -- the "feature X did nothing" case the loader refuses to mask."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WebProvidersConfig.model_validate({"tavili": {"apiKey": "sk-typo"}})
    # Through a whole-config load, which is how a hand-edited file arrives.
    with pytest.raises(ValidationError):
        Config.model_validate({"tools": {"web": {"providers": {"tavili": {"apiKey": "sk-typo"}}}}})


def test_forbidding_extras_keeps_both_key_spellings() -> None:
    """``extra="forbid"`` beside an alias generator is the combination that
    would reject the config file's own snake_case spelling."""
    assert WebProvidersConfig.model_validate({"tavily": {"apiKey": "sk-a"}}).key_for("tavily") == "sk-a"
    assert WebProvidersConfig.model_validate({"tavily": {"api_key": "sk-b"}}).key_for("tavily") == "sk-b"
