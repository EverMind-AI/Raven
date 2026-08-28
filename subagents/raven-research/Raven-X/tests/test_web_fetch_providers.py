"""Per-provider request and response contracts of ``WebFetchTool``.

Two backends read pages behind one tool. What differs is the request, how a
failure is reported, and what the response carries; everything downstream --
the SSRF gate, containment, the digest path, truncation and the ledger -- is one
code path below them.

Three properties matter more than the mapping:

* The Jina request stays byte-identical, and so does its payload. It is the
  anchor's wire traffic and the anchor's model-visible bytes; a field added to
  that payload would move every arm at once.
* A backend that reports failure inside a 200 envelope must not read as a page.
  ``fetch_result_ok`` gates the fetch floor, so a "successful" empty fetch would
  release a gate whose whole contract is that a page was opened.
* The fallback chain runs on a vendor that could not serve the page, never on a
  refusal. A containment refusal and a failed URL validation are rules, and
  retrying a rule on another vendor is an end-run around the gate.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from raven.agent.tools.web import (
    DEFAULT_FETCH_PROVIDER,
    FETCH_PROVIDERS,
    WebFetchTool,
    fetch_result_ok,
    selected_fetch_fallback,
    selected_fetch_key,
    selected_fetch_provider,
    web_provider_keys,
)
from raven.config.schema import (
    WebFetchConfig,
    WebProviderKey,
    WebProvidersConfig,
    WebToolsConfig,
)

PAGE = "para about the topic. " * 40


def _anysearch_ok(content: str = PAGE, title: str = "A Title") -> dict:
    return {
        "code": 0,
        "message": "success",
        "request_id": "r-1",
        "data": {"url": "https://x.example/", "title": title, "content": content},
    }


class _Recorder:
    """Client that records what it was asked for and answers per method.

    Keyed by method rather than by call order because the two backends do not
    take the same number of calls: the Jina branch re-reads a garbled page, and
    a retryable failure is sent up to three times before it gives up. A queue
    would hand the fallback's response to the selected provider's retry.
    """

    def __init__(self, get: object = None, post: object = None) -> None:
        self._by_method = {"GET": get, "POST": post}
        self.calls: list[dict] = []

    async def __aenter__(self) -> "_Recorder":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def _next(self, method: str, url: str, kwargs: dict) -> object:
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self._by_method[method]
        if isinstance(item, Exception):
            raise item
        assert item is not None, f"no {method} response queued"
        return item

    async def get(self, url, headers=None, **kwargs):
        return self._next("GET", url, {"headers": dict(headers or {})})

    async def post(self, url, json=None, headers=None, **kwargs):
        return self._next("POST", url, {"json": json, "headers": dict(headers or {})})


def _dead_key() -> httpx.HTTPStatusError:
    """The measured Jina failure: an out-of-credit key answers 402.

    Not a retryable status, so it fails on the first attempt - which is what
    makes it the right stand-in here as well as the realistic one.
    """
    request = httpx.Request("GET", "https://r.jina.ai/x")
    return httpx.HTTPStatusError(
        "402", request=request, response=httpx.Response(402, request=request)
    )


class _Resp:
    def __init__(self, *, text: str = "", payload: object = None, status: int = 200) -> None:
        self.text = text
        self._payload = payload
        self.status_code = status

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Patched:
    """Stub the HTTP client and the SSRF validator (fake hosts do not resolve)."""

    def __init__(self, client: _Recorder, valid: bool = True) -> None:
        self._client = patch("raven.agent.tools.web.httpx.AsyncClient", lambda **kw: client)

        async def _check(u, **_):
            return (valid, "" if valid else "blocked")

        self._validator = patch("raven.agent.tools.web.validate_url_target_async", _check)

    def __enter__(self) -> "_Patched":
        self._client.__enter__()
        self._validator.__enter__()
        return self

    def __exit__(self, *args: object) -> bool:
        self._validator.__exit__(*args)
        return self._client.__exit__(*args)


# --------------------------------------------------------------------------- #
# request construction                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_jina_request_is_unchanged() -> None:
    # Pinned against literals, not against the code that builds them: this is
    # the anchor's wire traffic, and every measured run so far sent exactly it.
    client = _Recorder(get=_Resp(text=PAGE))
    with _Patched(client):
        await WebFetchTool(api_key="k").execute(url="https://x.example/a#frag")

    assert client.calls[0]["method"] == "GET"
    assert client.calls[0]["url"] == "https://r.jina.ai/https://x.example/a%23frag"
    assert client.calls[0]["headers"] == {"Accept": "text/plain", "Authorization": "Bearer k"}


@pytest.mark.asyncio
async def test_jina_without_a_key_sends_no_authorization() -> None:
    client = _Recorder(get=_Resp(text=PAGE))
    with _Patched(client):
        await WebFetchTool().execute(url="https://x.example/")

    assert "Authorization" not in client.calls[0]["headers"]


@pytest.mark.asyncio
async def test_the_anysearch_request_is_a_post_to_extract() -> None:
    client = _Recorder(post=_Resp(payload=_anysearch_ok()))
    with _Patched(client):
        await WebFetchTool(provider="anysearch", api_key="k-any").execute(url="https://x.example/")

    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.anysearch.com/v1/extract"
    assert call["json"] == {"url": "https://x.example/"}
    assert call["headers"]["Authorization"] == "Bearer k-any"


# --------------------------------------------------------------------------- #
# response handling                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failing_envelope_in_a_200_is_not_a_page() -> None:
    # The other failure shape raises in ``_send_with_retry`` (raise_for_status,
    # and 422 is not retried). This one answers 200 and reports the failure in
    # the body, so nothing above notices unless the envelope is read.
    client = _Recorder(post=_Resp(payload={"code": -1, "message": "Unable to extract content from the URL."}))
    with _Patched(client):
        out = await WebFetchTool(provider="anysearch", api_key="k").execute(url="https://x.example/")

    assert fetch_result_ok(out) is False
    assert "Unable to extract content" in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_an_empty_body_in_a_healthy_envelope_is_not_a_page() -> None:
    client = _Recorder(post=_Resp(payload=_anysearch_ok(content="")))
    with _Patched(client):
        out = await WebFetchTool(provider="anysearch", api_key="k").execute(url="https://x.example/")

    assert fetch_result_ok(out) is False


@pytest.mark.asyncio
async def test_the_title_is_its_own_field_not_a_header_block() -> None:
    # Jina embeds ``Title:``/``URL Source:``/``Published Time:`` in the text.
    # Imitating half of that block would report a missing publication date as an
    # undated page, so the field AnySearch does carry is carried as a field.
    client = _Recorder(post=_Resp(payload=_anysearch_ok(title="Real Title")))
    with _Patched(client):
        out = json.loads(
            await WebFetchTool(provider="anysearch", api_key="k").execute(url="https://x.example/")
        )

    assert out["title"] == "Real Title"
    assert out["text"] == PAGE
    assert "Title:" not in out["text"]


@pytest.mark.asyncio
async def test_the_extractor_names_the_backend_that_served_the_page(monkeypatch, tmp_path) -> None:
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    client = _Recorder(post=_Resp(payload=_anysearch_ok()))
    with _Patched(client):
        out = json.loads(
            await WebFetchTool(provider="anysearch", api_key="k").execute(url="https://x.example/")
        )

    assert out["extractor"] == "anysearch-extract"
    assert json.loads(ledger.read_text().splitlines()[0])["extractor"] == "anysearch-extract"


@pytest.mark.asyncio
async def test_the_default_payload_gains_no_field() -> None:
    # The payload is what the model reads. Adding a key on the default path is a
    # distribution change, so the extra columns are only carried off it.
    client = _Recorder(get=_Resp(text=PAGE))
    with _Patched(client):
        default = json.loads(await WebFetchTool().execute(url="https://x.example/"))

    assert set(default) == {
        "url", "finalUrl", "status", "extractor", "extractMode", "truncated", "length", "text"
    }
    assert default["extractor"] == "jina-reader"


@pytest.mark.asyncio
async def test_a_non_default_backend_reports_the_size_it_was_handed() -> None:
    # ``truncated`` is measured against this tool's own cap, so a backend that
    # truncated first would report False on a page it cut.
    client = _Recorder(post=_Resp(payload=_anysearch_ok()))
    with _Patched(client):
        out = json.loads(
            await WebFetchTool(provider="anysearch", api_key="k", max_chars=50).execute(
                url="https://x.example/"
            )
        )

    assert out["truncated"] is True
    assert out["length"] == 50
    assert out["source_chars"] == len(PAGE)


# --------------------------------------------------------------------------- #
# fallback chain                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_dead_backend_falls_through_to_the_next(monkeypatch, tmp_path) -> None:
    # The measured failure this exists for: an out-of-credit Jina key answered
    # 402 where no key answers 200, and that run recorded pages_ok 0 of 33.
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    client = _Recorder(get=_dead_key(), post=_Resp(payload=_anysearch_ok()))
    with _Patched(client):
        out = json.loads(
            await WebFetchTool(
                provider="jina",
                fallback=["anysearch"],
                provider_keys={"anysearch": "k-any"},
            ).execute(url="https://x.example/")
        )

    assert out["extractor"] == "anysearch-extract"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    fell_back = [r for r in rows if r["op"] == "fetch_fallback"]
    assert len(fell_back) == 1
    assert fell_back[0]["provider"] == "jina" and fell_back[0]["next"] == "anysearch"


@pytest.mark.asyncio
async def test_a_fallback_entry_uses_its_own_vendors_key() -> None:
    # ``api_key`` names the SELECTED provider. Reusing it for the fallback would
    # send one vendor's credential to another vendor's endpoint.
    client = _Recorder(get=_dead_key(), post=_Resp(payload=_anysearch_ok()))
    with _Patched(client):
        await WebFetchTool(
            api_key="k-jina",
            fallback=["anysearch"],
            provider_keys={"anysearch": "k-any"},
        ).execute(url="https://x.example/")

    assert client.calls[-1]["headers"]["Authorization"] == "Bearer k-any"


@pytest.mark.asyncio
async def test_a_refused_url_never_reaches_the_fallback() -> None:
    client = _Recorder(post=_Resp(payload=_anysearch_ok()))
    with _Patched(client, valid=False):
        out = json.loads(
            await WebFetchTool(fallback=["anysearch"], provider_keys={"anysearch": "k"}).execute(
                url="https://x.example/"
            )
        )

    assert "URL validation failed" in out["error"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_an_empty_page_falls_through_to_the_next(monkeypatch, tmp_path) -> None:
    # A 200 with nothing in it: the shape the Jina branch cannot see, because it
    # returns whatever the reader sent. Left unchecked it reads as a page, so the
    # chain stopped at the first vendor and the ledger recorded ok with chars 0 -
    # a fetch floor released on no evidence.
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    client = _Recorder(get=_Resp(text="   \n"), post=_Resp(payload=_anysearch_ok()))
    with _Patched(client):
        out = json.loads(
            await WebFetchTool(
                fallback=["anysearch"], provider_keys={"anysearch": "k-any"}
            ).execute(url="https://x.example/")
        )

    assert out["extractor"] == "anysearch-extract"
    assert out["text"] == PAGE
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    fell_back = [r for r in rows if r["op"] == "fetch_fallback"]
    assert len(fell_back) == 1
    assert fell_back[0]["provider"] == "jina" and fell_back[0]["next"] == "anysearch"


@pytest.mark.asyncio
async def test_an_empty_page_with_no_next_backend_is_returned_as_it_was() -> None:
    # The same response on the default configuration, where the check is off.
    # What the model reads here is the anchor's, and turning this into an error
    # would move it - a separate round, never a fix folded into a chain change.
    client = _Recorder(get=_Resp(text=""))
    with _Patched(client):
        out = await WebFetchTool().execute(url="https://x.example/")

    assert fetch_result_ok(out) is True
    assert json.loads(out)["length"] == 0


@pytest.mark.asyncio
async def test_the_last_backends_failure_is_what_is_reported() -> None:
    client = _Recorder(get=_dead_key(), post=_dead_key())
    with _Patched(client):
        out = json.loads(
            await WebFetchTool(fallback=["anysearch"], provider_keys={"anysearch": "k"}).execute(
                url="https://x.example/"
            )
        )

    assert fetch_result_ok(json.dumps(out)) is False
    assert "402" in out["error"]


def test_the_chain_drops_the_selected_provider_and_the_unknown() -> None:
    # An entry naming the selected provider would re-issue the request that just
    # failed; one naming a provider that does not exist would raise inside the
    # path that exists to survive a failure.
    tool = WebFetchTool(provider="jina", fallback=["jina", "bing", "anysearch", "anysearch"])
    assert tool.fallback == ["anysearch"]


# --------------------------------------------------------------------------- #
# registration and config resolution                                            #
# --------------------------------------------------------------------------- #


def test_only_a_backend_that_needs_a_key_can_be_withheld(monkeypatch) -> None:
    # Jina reads pages unauthenticated, so a bare checkout must keep offering
    # web_fetch exactly as it always has. One predicate, because the main loop
    # and the sub-agent registry both ask it and must agree.
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    assert FETCH_PROVIDERS[DEFAULT_FETCH_PROVIDER].needs_key is False
    assert FETCH_PROVIDERS["anysearch"].needs_key is True

    assert WebFetchTool().registrable is True
    assert WebFetchTool(provider="anysearch").registrable is False
    assert WebFetchTool(provider="anysearch", api_key="k").registrable is True
    assert WebFetchTool(provider="anysearch", corpus_endpoint="http://x").registrable is True


def test_the_dict_key_and_the_specs_vendor_agree() -> None:
    for name, spec in FETCH_PROVIDERS.items():
        assert spec.vendor == name
        assert spec.config_path == f"tools.web.providers.{name}.apiKey"


def test_each_provider_resolves_only_its_own_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    monkeypatch.setenv("JINA_API_KEY", "sk-jina")
    assert WebFetchTool().api_key == "sk-jina"
    assert WebFetchTool(provider="anysearch").api_key == ""

    monkeypatch.setenv("ANYSEARCH_API_KEY", "sk-any")
    assert WebFetchTool(provider="anysearch").api_key == "sk-any"


def test_an_unknown_provider_degrades_to_the_default() -> None:
    assert WebFetchTool(provider="firecrawl").provider == DEFAULT_FETCH_PROVIDER
    assert selected_fetch_provider(object()) == DEFAULT_FETCH_PROVIDER


def test_the_description_only_changes_off_the_default() -> None:
    # It is part of the tool schema the model is shown, so the default build's
    # must render byte for byte what the class always declared.
    assert WebFetchTool().description == WebFetchTool.description
    assert "AnySearch" in WebFetchTool(provider="anysearch").description


def test_one_vendor_key_serves_both_web_tools() -> None:
    # The whole reason the credential is keyed by vendor: one AnySearch account
    # answers both /v1/search and /v1/extract.
    web = WebToolsConfig(
        fetch=WebFetchConfig(provider="anysearch", fallback=["jina"]),
        providers=WebProvidersConfig(
            anysearch=WebProviderKey(api_key="sk-any"),
            jina=WebProviderKey(api_key="sk-jina"),
        ),
    )
    assert selected_fetch_key(web) == ("anysearch", "sk-any")
    assert selected_fetch_fallback(web.fetch) == ["jina"]
    assert web_provider_keys(web) == {"anysearch": "sk-any", "jina": "sk-jina"}
