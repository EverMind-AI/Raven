"""Data sources other than an uploaded file: a typed note, and a fetched page.

Both end up as markdown in the same blob store as every other document, so
nothing downstream -- parser table, chunker, indexer, preview -- learns a second
shape. What this module owns is getting them there: reading a page off the web,
and deciding what each is called in a list whose other rows are filenames.

The page fetch follows Cherry Studio's knowledge-base capture: Jina Reader,
which reads a page without a key, asked for JSON so the page's own title comes
back as a field rather than as a preamble to parse out of the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from raven.security.network import validate_url_target

JINA_READER_HOST = "https://r.jina.ai"
FETCH_TIMEOUT_SECONDS = 30.0

#: Long enough to tell two captures of the same site apart, short enough to sit
#: in a table cell. Cherry Studio caps its own snapshot titles at the same 80.
TITLE_MAX = 80

_UNSAFE = re.compile(r"[^\w\-. ]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


class SourceFetchError(RuntimeError):
    """A page could not be read. The message is shown to the reader as-is."""


@dataclass(frozen=True)
class FetchedPage:
    """One page as the reader served it."""

    title: str
    markdown: str


def safe_stem(text: str) -> str:
    """One line of text reduced to something that can be a filename.

    Path separators and the rest go before anything else: this names a file in
    the blob store, and the text came from a page title on the open web.
    """
    cleaned = _SPACES.sub(" ", _UNSAFE.sub(" ", text)).strip(" .-")
    return cleaned[:TITLE_MAX].strip()


def _first_heading_or_line(markdown: str) -> str:
    lines = [line.strip() for line in markdown.splitlines()]
    for line in lines:
        if re.match(r"^#{1,6}\s+", line):
            return re.sub(r"^#{1,6}\s+", "", line)
    for line in lines:
        if line:
            return line
    return ""


def _url_stem(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    segments = [s for s in parsed.path.split("/") if s]
    return "-".join(part for part in (parsed.hostname, segments[-1] if segments else "") if part)


def page_filename(markdown: str, url: str, page_title: str = "") -> str:
    """What a captured page is called in the list.

    The page's own title, then its first heading, then host-and-last-segment,
    then ``page`` -- Cherry Studio's ladder, and each rung is there because the
    one above it is empty often enough to matter: a title-less page, a page
    served as a bare body, a URL that is only a host.
    """
    for candidate in (page_title, _first_heading_or_line(markdown), _url_stem(url)):
        stem = safe_stem(candidate)
        if stem:
            return f"{stem}.md"
    return "page.md"


def note_filename(title: str, text: str) -> str:
    """What a typed note is called. Its title, or its first line if untitled."""
    for candidate in (title, _first_heading_or_line(text)):
        stem = safe_stem(candidate)
        if stem:
            return f"{stem}.md"
    return "note.md"


async def fetch_page(url: str, *, api_key: str = "") -> FetchedPage:
    """Read one page as markdown through Jina Reader.

    Through ``validate_url_target`` first, because the URL arrives from a
    browser: without it this endpoint is a request forger that reaches whatever
    the gateway can reach, including link-local metadata services, and answers
    with the body.

    The key is optional -- Jina reads anonymously at a lower rate limit -- so a
    deployment that has not configured one still gets this feature rather than
    an error telling it to go and sign up.
    """
    target = url.strip()
    ok, why = validate_url_target(target)
    if not ok:
        raise SourceFetchError(why or f"refused to fetch {target}")

    headers = {"Accept": "application/json", "X-Retain-Images": "none"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            # The target URL is appended raw: encoding it would change what
            # Reader treats as the path of the page to read.
            response = await client.get(f"{JINA_READER_HOST}/{target}", headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise SourceFetchError(f"the reader answered HTTP {exc.response.status_code} for {target}") from exc
    except httpx.HTTPError as exc:
        raise SourceFetchError(f"could not reach {target}: {exc}") from exc
    except ValueError as exc:
        raise SourceFetchError(f"the reader did not answer with a page for {target}") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    fields = data if isinstance(data, dict) else payload if isinstance(payload, dict) else {}
    markdown = str(fields.get("content") or fields.get("text") or "").strip()
    if not markdown:
        raise SourceFetchError(f"{target} had no readable text")
    return FetchedPage(title=str(fields.get("title") or "").strip(), markdown=markdown)
