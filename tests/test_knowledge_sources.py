"""Data sources other than an uploaded file: what a note and a page are called,
and how a page is read."""

from __future__ import annotations

import httpx
import pytest

from raven.knowledge import _sources


def _reader(monkeypatch, handler) -> dict[str, list[httpx.Request]]:
    """Route the Jina Reader fetch through a MockTransport."""
    seen: dict[str, list[httpx.Request]] = {"requests": []}

    def recording(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request)
        return handler(request)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(recording))
        return real(*args, **kwargs)

    monkeypatch.setattr(_sources.httpx, "AsyncClient", factory)
    monkeypatch.setattr(_sources, "validate_url_target", lambda url: (True, ""))
    return seen


def test_a_page_is_named_after_its_title_before_anything_else() -> None:
    assert _sources.page_filename("# Heading\n\nbody", "https://ex.com/a/b", "Real Title") == "Real Title.md"


def test_a_titleless_page_falls_back_down_the_ladder() -> None:
    """Each rung is there because the one above it is empty often enough to
    matter: a page served with no title, one with no headings, a URL that is
    only a host."""
    assert _sources.page_filename("# Heading\n\nbody", "https://ex.com/a/b") == "Heading.md"
    assert _sources.page_filename("plain body", "https://ex.com/docs/guide") == "plain body.md"
    assert _sources.page_filename("", "https://ex.com/docs/guide") == "ex.com-guide.md"
    assert _sources.page_filename("", "https://ex.com") == "ex.com.md"


def test_a_title_cannot_walk_out_of_the_blob_store() -> None:
    """The title comes off the open web, and it is about to name a file."""
    assert "/" not in _sources.page_filename("", "", "../../etc/passwd")
    assert _sources.safe_stem("../../etc/passwd") == "etc passwd"


def test_a_long_title_is_cut_to_something_that_fits_a_table_cell() -> None:
    name = _sources.page_filename("", "", "x" * 200)

    assert len(name) == _sources.TITLE_MAX + len(".md")


def test_a_note_is_named_from_its_title_or_its_first_line() -> None:
    assert _sources.note_filename("Release plan", "body") == "Release plan.md"
    assert _sources.note_filename("", "Standup notes\n\nshipped it") == "Standup notes.md"
    assert _sources.note_filename("", "   ") == "note.md"


@pytest.mark.asyncio
async def test_reading_a_page_asks_the_reader_for_json_not_a_preamble(monkeypatch) -> None:
    """The title has to come back as a field. Asked for as text it arrives as a
    header block above the body, which then has to be parsed back off, and ends
    up in the chunks when it is not."""
    seen = _reader(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": {"title": "Raven", "content": "# Raven\n\nhello"}}),
    )

    page = await _sources.fetch_page("https://example.com/docs")

    assert (page.title, page.markdown) == ("Raven", "# Raven\n\nhello")
    request = seen["requests"][0]
    assert str(request.url) == "https://r.jina.ai/https://example.com/docs"
    assert request.headers["Accept"] == "application/json"
    # No key: Reader answers anonymously at a lower rate limit, and a
    # deployment that has not configured one still gets this feature.
    assert "Authorization" not in request.headers


@pytest.mark.asyncio
async def test_a_configured_key_is_sent_and_an_absent_one_is_not(monkeypatch) -> None:
    seen = _reader(monkeypatch, lambda request: httpx.Response(200, json={"data": {"content": "body"}}))

    await _sources.fetch_page("https://example.com", api_key="k-1")

    assert seen["requests"][0].headers["Authorization"] == "Bearer k-1"


@pytest.mark.asyncio
async def test_a_refused_url_never_reaches_the_network(monkeypatch) -> None:
    """Without this the endpoint is a request forger: it takes a URL from a
    browser and answers with the body of whatever the gateway can reach."""
    monkeypatch.setattr(_sources, "validate_url_target", lambda url: (False, "blocked host"))

    def _explode(request: httpx.Request) -> httpx.Response:
        pytest.fail("must not fetch a refused target")

    real = httpx.AsyncClient
    monkeypatch.setattr(
        _sources.httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(_explode)}),
    )

    with pytest.raises(_sources.SourceFetchError) as caught:
        await _sources.fetch_page("http://169.254.169.254/latest/meta-data/")

    assert "blocked host" in str(caught.value)


@pytest.mark.asyncio
async def test_an_http_error_names_the_status_rather_than_the_stack(monkeypatch) -> None:
    _reader(monkeypatch, lambda request: httpx.Response(404))

    with pytest.raises(_sources.SourceFetchError) as caught:
        await _sources.fetch_page("https://example.com/gone")

    assert "404" in str(caught.value)


@pytest.mark.asyncio
async def test_a_page_with_no_text_is_an_error_not_an_empty_document(monkeypatch) -> None:
    """An empty document indexes to nothing and sits in the list looking as if
    it worked."""
    _reader(monkeypatch, lambda request: httpx.Response(200, json={"data": {"title": "Nothing", "content": "  "}}))

    with pytest.raises(_sources.SourceFetchError) as caught:
        await _sources.fetch_page("https://example.com/empty")

    assert "no readable text" in str(caught.value)


@pytest.mark.asyncio
async def test_an_answer_that_is_not_a_page_is_reported_as_one(monkeypatch) -> None:
    _reader(monkeypatch, lambda request: httpx.Response(200, content=b"<html>not json</html>"))

    with pytest.raises(_sources.SourceFetchError):
        await _sources.fetch_page("https://example.com/html")


def test_a_page_with_nothing_to_name_it_by_still_gets_a_name() -> None:
    """An untitled page at a bare address is rare and not an error; a document
    with no filename at all would be."""
    assert _sources.page_filename("", "", "") == "page.md"
    assert _sources.page_filename("", "http://[", "") == "page.md"


@pytest.mark.asyncio
async def test_a_reader_that_cannot_be_reached_says_that(monkeypatch) -> None:
    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _reader(monkeypatch, _refuse)

    with pytest.raises(_sources.SourceFetchError) as caught:
        await _sources.fetch_page("https://example.com")

    assert "could not reach" in str(caught.value)
